//! Top-level migration orchestration.
//!
//! Ports `mod_armor_migrator/migrator.py`'s `migrate_all`, `migrate_one`, and
//! `migrate_from_remap_json`. Parallelism: per-target via rayon `par_iter`.

pub mod mode_a;
pub mod mode_b;
pub mod report;

pub use report::MigrationReport;

use crate::archive::{StreamToc, TocEntry};
use crate::constants::{TEX_ID, UNIT_ID, type_name};
use crate::index::ArchiveIndex;
use crate::padding::{EmptyUnitTemplate, PaddingMode};
use crate::refs;
use eyre::WrapErr;
use rayon::prelude::*;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub trait ProgressSink: Sync {
    fn target_started(&self, name: &str);
    fn stage(&self, name: &str, stage: &str);
    fn target_finished(&self, name: &str);
}

pub struct MigrateAllOpts<'a> {
    pub patch_path: &'a Path,
    pub data_dir: &'a Path,
    pub out_dir: &'a Path,
    pub archive_index: &'a ArchiveIndex,
    pub source_hash: Option<&'a str>,
    pub target_hashes: Option<&'a [String]>,
    pub category: &'a str,
    pub patch_suffix: &'a str,
    pub empty_unit_template: Option<&'a EmptyUnitTemplate>,
    pub padding_mode: PaddingMode,
    pub reference_remap_json: Option<&'a Path>,
    pub experimental_partial_remap: bool,
    pub progress: Option<&'a dyn ProgressSink>,
}

pub struct MigrateFromRemapOpts<'a> {
    pub patch_path: &'a Path,
    pub remap_json: &'a Path,
    pub out_dir: &'a Path,
    pub target_names: Option<&'a [String]>,
    pub patch_suffix: &'a str,
    pub empty_unit_template: Option<&'a EmptyUnitTemplate>,
    pub padding_mode: PaddingMode,
    pub progress: Option<&'a dyn ProgressSink>,
}

pub fn migrate_all(opts: MigrateAllOpts) -> crate::Result<Vec<MigrationReport>> {
    mode_a::run(opts)
}

pub fn migrate_from_remap_json(opts: MigrateFromRemapOpts) -> crate::Result<Vec<MigrationReport>> {
    std::fs::create_dir_all(opts.out_dir)
        .wrap_err_with(|| format!("create out_dir {}", opts.out_dir.display()))?;
    let table = mode_b::load(opts.remap_json)?;
    tracing::info!(
        path = %opts.patch_path.display(),
        "loading patch"
    );
    let patch = StreamToc::from_files(opts.patch_path)?;
    tracing::info!(
        entries = patch.entries.len(),
        "patch loaded"
    );
    let base_hash = table
        .patch_filename
        .split('.')
        .next()
        .unwrap_or("")
        .to_string();

    let targets: Vec<(String, mode_b::ParsedTarget)> = table
        .targets
        .iter()
        .filter(|(name, _)| match opts.target_names {
            Some(filter) => filter.iter().any(|f| f == *name),
            None => true,
        })
        .map(|(name, t)| (name.clone(), t.parse().unwrap_or_default()))
        .collect();

    if let Some(p) = opts.progress {
        for (name, _) in &targets {
            p.target_started(name);
        }
    }

    let patch_arc = std::sync::Arc::new(patch);
    let reports: Mutex<Vec<MigrationReport>> = Mutex::new(Vec::new());
    targets
        .par_iter()
        .for_each_with(patch_arc.clone(), |patch_arc, (name, parsed)| {
            if let Some(p) = opts.progress {
                p.stage(name, "rewriting");
            }
            let res = migrate_one_precomputed(
                patch_arc,
                name,
                &base_hash,
                parsed,
                opts.out_dir,
                opts.patch_suffix,
                opts.empty_unit_template,
                opts.padding_mode,
            );
            match res {
                Ok(report) => {
                    if let Some(p) = opts.progress {
                        p.target_finished(name);
                    }
                    reports.lock().expect("lock").push(report);
                }
                Err(e) => {
                    if let Some(p) = opts.progress {
                        p.target_finished(name);
                    }
                    tracing::error!(
                        target = %name,
                        error = %e,
                        "migration failed"
                    );
                }
            }
        });
    let mut out = reports.into_inner().expect("lock");
    out.sort_by(|a, b| a.target_name.cmp(&b.target_name));
    Ok(out)
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn migrate_one_precomputed(
    patch: &StreamToc,
    target_name: &str,
    base_hash: &str,
    parsed: &mode_b::ParsedTarget,
    out_root: &Path,
    patch_suffix: &str,
    empty_unit_template: Option<&EmptyUnitTemplate>,
    padding_mode: PaddingMode,
) -> crate::Result<MigrationReport> {
    let mut new_patch = StreamToc::default();
    let mut written = 0usize;
    let mut file_id_remapped = 0usize;
    for e in &patch.entries {
        let new_file_id = parsed.file_ids.get(&e.file_id).copied().unwrap_or(e.file_id);
        if new_file_id != e.file_id {
            file_id_remapped += 1;
        }
        let toc_data = refs::rewrite(
            e.type_id,
            &e.toc_data,
            &parsed.file_ids,
            if parsed.slot_ids.is_empty() {
                None
            } else {
                Some(&parsed.slot_ids)
            },
        );
        let mut new_entry = TocEntry::new(new_file_id, e.type_id);
        new_entry.toc_data = toc_data;
        new_entry.gpu_data = e.gpu_data.clone();
        new_entry.stream_data = e.stream_data.clone();
        new_patch.entries.push(new_entry);
        written += 1;
    }

    let padded = if !parsed.extra_unit_file_ids.is_empty() {
        if let Some(template) = empty_unit_template {
            let slot_ref = if parsed.slot_ids.is_empty() {
                None
            } else {
                Some(&parsed.slot_ids)
            };
            let extras = crate::padding::pad_patch(
                &mut new_patch,
                &parsed.extra_unit_file_ids,
                template,
                padding_mode,
                slot_ref,
            );
            extras.len()
        } else {
            tracing::warn!(
                target = %target_name,
                count = parsed.extra_unit_file_ids.len(),
                "target has extra Unit slots but no empty-mesh template supplied"
            );
            0
        }
    } else {
        0
    };

    let out_dir = out_root.join(safe_filename(target_name));
    std::fs::create_dir_all(&out_dir)
        .wrap_err_with(|| format!("create dir {}", out_dir.display()))?;
    let out_path = out_dir.join(patch_suffix);
    new_patch.write_files(&out_path)?;

    tracing::info!(
        target = %target_name,
        entries = written,
        file_id_remapped,
        slot_id_remapped = parsed.slot_ids.len(),
        padded,
        "migrated"
    );

    Ok(MigrationReport {
        target_hash: base_hash.to_string(),
        target_name: target_name.to_string(),
        out_path: Some(out_path),
        file_id_remapped,
        slot_id_remapped: parsed.slot_ids.len(),
        padded_units: padded,
        skipped_entries: 0,
        skipped_types: Vec::new(),
        type_counts: HashMap::new(),
        warnings: Vec::new(),
    })
}

// ---------- shared helpers (also used by Mode A) ----------

pub(crate) fn safe_filename(name: &str) -> String {
    name.chars()
        .map(|c| if "<>:\"/\\|?*".contains(c) { '_' } else { c })
        .collect::<String>()
        .trim()
        .trim_end_matches('.')
        .to_string()
}

/// Build a FileID remap from source -> target archive entries.
///
/// Unit entries are intentionally excluded because their archive order is not
/// a stable slot identity. Automatic Unit mapping is done later by geometry.
pub(crate) fn build_remap(source: &StreamToc, target: &StreamToc) -> RemapPlan {
    let src_by_type = type_order(&source.entries);
    let tgt_by_type = type_order(&target.entries);
    let mut remap: HashMap<u64, u64> = HashMap::new();
    let mut skipped_types: Vec<u64> = Vec::new();
    let mut counts: HashMap<u64, (usize, usize)> = HashMap::new();
    let mut skipped_file_ids: HashSet<u64> = HashSet::new();

    let src_entries_by_type = entries_by_type(&source.entries);
    let tgt_entries_by_type = entries_by_type(&target.entries);

    for tid in &src_by_type {
        let src_entries: &[&TocEntry] = src_entries_by_type
            .get(tid)
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        let tgt_entries: &[&TocEntry] = tgt_entries_by_type
            .get(tid)
            .map(|v| v.as_slice())
            .unwrap_or(&[]);
        counts.insert(*tid, (src_entries.len(), tgt_entries.len()));
        if tgt_entries.is_empty() {
            skipped_types.push(*tid);
            for e in src_entries {
                skipped_file_ids.insert(e.file_id);
            }
            tracing::debug!(
                type_id = %type_name(*tid).unwrap_or("<unknown>"),
                "skip type: not present in target"
            );
            continue;
        }
        if src_entries.len() != tgt_entries.len() {
            tracing::warn!(
                type_id = %type_name(*tid).unwrap_or("<unknown>"),
                source = src_entries.len(),
                target = tgt_entries.len(),
                "type count mismatch (partial ordinal remap)"
            );
        }
        if *tid == UNIT_ID {
            if src_entries.len() > tgt_entries.len() {
                skipped_types.push(*tid);
                for e in src_entries.iter() {
                    skipped_file_ids.insert(e.file_id);
                }
            }
            tracing::debug!("skip Unit ordinal remap: Unit slots require geometry remap");
            continue;
        }
        if src_entries.len() > tgt_entries.len() {
            skipped_types.push(*tid);
            for e in src_entries[tgt_entries.len()..].iter() {
                skipped_file_ids.insert(e.file_id);
            }
        }
        for (src_e, tgt_e) in src_entries.iter().zip(tgt_entries.iter()) {
            if src_e.file_id != tgt_e.file_id {
                remap.insert(src_e.file_id, tgt_e.file_id);
            }
        }
        // Also discard tgt_by_type if it has types not in source — handled via
        // type_order iteration over both below.
        let _ = &tgt_by_type;
    }

    let src_units = src_entries_by_type
        .get(&UNIT_ID)
        .map(|v| v.as_slice())
        .unwrap_or(&[]);
    let tgt_units = tgt_entries_by_type
        .get(&UNIT_ID)
        .map(|v| v.as_slice())
        .unwrap_or(&[]);
    let extra_units: Vec<u64> = tgt_units
        .iter()
        .skip(src_units.len())
        .map(|e| e.file_id)
        .collect();

    RemapPlan {
        remap,
        skipped_types,
        type_counts: counts,
        skipped_file_ids,
        extra_unit_file_ids: extra_units,
    }
}

#[derive(Debug, Clone, Default)]
pub(crate) struct RemapPlan {
    pub remap: HashMap<u64, u64>,
    pub skipped_types: Vec<u64>,
    pub type_counts: HashMap<u64, (usize, usize)>,
    pub skipped_file_ids: HashSet<u64>,
    pub extra_unit_file_ids: Vec<u64>,
}

fn type_order(entries: &[TocEntry]) -> Vec<u64> {
    let mut order = Vec::new();
    for e in entries {
        if !order.contains(&e.type_id) {
            order.push(e.type_id);
        }
    }
    order
}

fn entries_by_type(entries: &[TocEntry]) -> HashMap<u64, Vec<&TocEntry>> {
    let mut out: HashMap<u64, Vec<&TocEntry>> = HashMap::new();
    for e in entries {
        out.entry(e.type_id).or_default().push(e);
    }
    out
}

/// Probe source archives for the one whose FileIDs overlap the patch most.
pub(crate) fn detect_source_archive(
    patch: &StreamToc,
    data_dir: &Path,
    archives: &[(String, String)], // (hash, name)
) -> Option<(String, String, usize)> {
    let patch_flat: HashSet<(u64, u64)> = patch
        .entries
        .iter()
        .map(|e| (e.type_id, e.file_id))
        .collect();
    if patch_flat.is_empty() {
        return None;
    }
    let mut best: Option<(String, String, usize)> = None;
    for (hash, name) in archives {
        let path = data_dir.join(hash);
        if !path.exists() {
            continue;
        }
        let Ok(ids) = crate::archive::list_file_ids(&path) else {
            continue;
        };
        let arch_flat: HashSet<(u64, u64)> = ids
            .iter()
            .flat_map(|(tid, fids)| fids.iter().map(move |fid| (*tid, *fid)))
            .collect();
        let hits = patch_flat.intersection(&arch_flat).count();
        if hits > 0 && best.as_ref().map(|b| hits > b.2).unwrap_or(true) {
            best = Some((hash.clone(), name.clone(), hits));
        }
    }
    best
}

/// Texture-ID logger helper used in verbose mode (Mode A).
#[allow(dead_code)]
pub(crate) fn list_referenced_source_texture_ids(
    patch: &StreamToc,
    source: &StreamToc,
) -> Vec<u64> {
    let source_textures: HashSet<u64> = source
        .entries
        .iter()
        .filter(|e| e.type_id == TEX_ID)
        .map(|e| e.file_id)
        .collect();
    let mut out: Vec<u64> = Vec::new();
    for entry in patch
        .entries
        .iter()
        .filter(|e| e.type_id == crate::constants::MATERIAL_ID)
    {
        for tex_id in refs::list_material_refs(&entry.toc_data) {
            if source_textures.contains(&tex_id) && !out.contains(&tex_id) {
                out.push(tex_id);
            }
        }
    }
    out
}

#[allow(dead_code)]
pub(crate) fn out_path(out_root: &Path, target_name: &str, patch_suffix: &str) -> PathBuf {
    out_root.join(safe_filename(target_name)).join(patch_suffix)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_filename_strips_forbidden_chars() {
        assert_eq!(safe_filename(r#"A:Foo/Bar*"<>?|"#), "A_Foo_Bar______");
        assert_eq!(safe_filename("trailing dots..."), "trailing dots");
    }
}
