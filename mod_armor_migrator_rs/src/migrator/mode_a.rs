//! Mode A: derive remap by reading game `data/` archives.
//!
//! Auto-detects the source armor archive, then for each target armor:
//! 1. Builds a non-Unit ordinal remap via [`super::build_remap`].
//! 2. Builds a Unit-FileID remap via [`crate::unit::geometry`] (currently not
//!    implemented; see notes there).
//! 3. Rewrites every patch entry through [`crate::refs::rewrite`] and writes
//!    one variant per target to `out_dir/<safe_name>/<patch_suffix>`.
//!
//! When `reference_remap_json` is supplied, its `file_ids` / `slot_ids` win
//! over the computed remap (override semantics — matches Python).

use super::{MigrateAllOpts, MigrationReport};
use crate::archive::{StreamToc, TocEntry};
use crate::constants::UNIT_ID;
use crate::refs;
use eyre::WrapErr;
use rayon::prelude::*;
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

pub(super) fn run(opts: MigrateAllOpts) -> crate::Result<Vec<MigrationReport>> {
    std::fs::create_dir_all(opts.out_dir)
        .wrap_err_with(|| format!("create out_dir {}", opts.out_dir.display()))?;

    let archives = opts
        .archive_index
        .category(opts.category)
        .ok_or_else(|| eyre::eyre!("category {:?} not found in archive index", opts.category))?;
    let armor_list: Vec<(String, String)> = archives
        .iter()
        .map(|a| (a.hash.clone(), a.name.clone()))
        .collect();
    let by_hash: HashMap<String, String> = armor_list.iter().cloned().collect();

    tracing::info!(path = %opts.patch_path.display(), "loading patch");
    let patch = StreamToc::from_files(opts.patch_path)?;
    tracing::info!(entries = patch.entries.len(), "patch loaded");

    // Source: explicit hash or auto-detect.
    let (source_hash, source_name) = match opts.source_hash {
        Some(h) => {
            let name = by_hash
                .get(h)
                .cloned()
                .ok_or_else(|| eyre::eyre!("--source {h} not found in category {:?}", opts.category))?;
            (h.to_string(), name)
        }
        None => {
            let detected = super::detect_source_archive(&patch, opts.data_dir, &armor_list)
                .ok_or_else(|| {
                    eyre::eyre!(
                        "could not auto-detect source archive — pass --source <hash> explicitly"
                    )
                })?;
            tracing::info!(
                hash = %detected.0,
                name = %detected.1,
                hits = detected.2,
                "source archive auto-detected"
            );
            (detected.0, detected.1)
        }
    };

    let source_path = opts.data_dir.join(&source_hash);
    let source = StreamToc::from_files(&source_path)?;
    tracing::info!(entries = source.entries.len(), "source loaded");

    let reference_remaps = load_reference_remaps(opts.reference_remap_json)?;

    let targets: Vec<(String, String)> = match opts.target_hashes {
        Some(filter) => filter
            .iter()
            .filter_map(|h| by_hash.get(h).map(|n| (h.clone(), n.clone())))
            .collect(),
        None => armor_list
            .into_iter()
            .filter(|(h, _)| h != &source_hash)
            .collect(),
    };

    let source = Arc::new(source);
    let patch = Arc::new(patch);
    let reports: Mutex<Vec<MigrationReport>> = Mutex::new(Vec::new());

    targets.par_iter().for_each(|(thash, tname)| {
        if let Some(p) = opts.progress {
            p.target_started(tname);
            p.stage(tname, "loading target");
        }
        let res = migrate_one_target(
            &patch,
            &source,
            opts.data_dir,
            thash,
            tname,
            opts.out_dir,
            opts.patch_suffix,
            opts.empty_unit_template,
            opts.padding_mode,
            reference_remaps.get(tname),
            opts.experimental_partial_remap,
            opts.progress,
        );
        if let Some(p) = opts.progress {
            p.target_finished(tname);
        }
        match res {
            Ok(report) => reports.lock().expect("lock").push(report),
            Err(e) => tracing::error!(target = %tname, error = %e, "migration failed"),
        }
    });

    let mut out = reports.into_inner().expect("lock");
    out.sort_by(|a, b| a.target_name.cmp(&b.target_name));
    let _ = source_name;
    Ok(out)
}

#[allow(clippy::too_many_arguments)]
fn migrate_one_target(
    patch: &StreamToc,
    source: &StreamToc,
    data_dir: &Path,
    target_hash: &str,
    target_name: &str,
    out_root: &Path,
    patch_suffix: &str,
    empty_unit_template: Option<&crate::padding::EmptyUnitTemplate>,
    padding_mode: crate::padding::PaddingMode,
    reference_remap: Option<&ReferenceRemap>,
    experimental_partial_remap: bool,
    progress: Option<&dyn super::ProgressSink>,
) -> crate::Result<MigrationReport> {
    let target_path = data_dir.join(target_hash);
    if let Some(p) = progress {
        p.stage(target_name, "reading target archive");
    }
    let target = StreamToc::from_files(&target_path)?;

    if let Some(p) = progress {
        p.stage(target_name, "computing remap");
    }
    let plan = super::build_remap(source, &target);
    let mut remap: HashMap<u64, u64> = plan.remap.clone();
    let mut slot_remap: HashMap<u32, u32> = HashMap::new();
    let mut extra_unit_file_ids = plan.extra_unit_file_ids.clone();
    let mut skipped_file_ids = plan.skipped_file_ids.clone();

    if let Some(refmap) = reference_remap {
        // Reference remap is an OVERRIDE: it wins per-key; computed remap fills the gaps.
        for (k, v) in &refmap.file_ids {
            remap.insert(*k, *v);
        }
        slot_remap.extend(refmap.slot_ids.iter().map(|(k, v)| (*k, *v)));
        if !refmap.extra_unit_file_ids.is_empty() {
            extra_unit_file_ids = refmap.extra_unit_file_ids.clone();
        }
        for k in refmap.file_ids.keys() {
            skipped_file_ids.remove(k);
        }
        tracing::info!(
            target = %target_name,
            file_ids = refmap.file_ids.len(),
            slot_ids = refmap.slot_ids.len(),
            "applied reference remap override"
        );
    } else {
        // No reference remap → must derive Unit remap from geometry.
        let _ = experimental_partial_remap;
        let settings = crate::unit::geometry::GeometryMatchSettings::default();
        let unit_remap =
            crate::unit::geometry::build_unit_geometry_remap(patch, source, &target, &settings)
                .wrap_err_with(|| format!("Unit geometry remap for {target_name}"))?;
        remap.extend(unit_remap.remap);
        if !unit_remap.extra_unit_file_ids.is_empty() {
            extra_unit_file_ids = unit_remap.extra_unit_file_ids;
        }
    }

    if let Some(p) = progress {
        p.stage(target_name, "rewriting entries");
    }
    let mut new_patch = StreamToc::default();
    let mut written = 0usize;
    let mut skipped_entries = 0usize;
    let slot_ref = if slot_remap.is_empty() {
        None
    } else {
        Some(&slot_remap)
    };
    for e in &patch.entries {
        if skipped_file_ids.contains(&e.file_id) {
            tracing::warn!(
                target = %target_name,
                file_id = e.file_id,
                type_id = %crate::constants::type_name(e.type_id).unwrap_or("<unknown>"),
                "dropping entry (target lacks matching slot)"
            );
            skipped_entries += 1;
            continue;
        }
        let new_file_id = remap.get(&e.file_id).copied().unwrap_or(e.file_id);
        let toc_data = refs::rewrite(e.type_id, &e.toc_data, &remap, slot_ref);
        let mut new_entry = TocEntry::new(new_file_id, e.type_id);
        new_entry.toc_data = toc_data;
        new_entry.gpu_data = e.gpu_data.clone();
        new_entry.stream_data = e.stream_data.clone();
        new_patch.entries.push(new_entry);
        written += 1;
    }

    let padded = if !extra_unit_file_ids.is_empty() {
        if let Some(template) = empty_unit_template {
            if let Some(p) = progress {
                p.stage(target_name, "padding empty units");
            }
            let extras = crate::padding::pad_patch(
                &mut new_patch,
                &extra_unit_file_ids,
                template,
                padding_mode,
                slot_ref,
            );
            extras.len()
        } else {
            tracing::warn!(
                target = %target_name,
                count = extra_unit_file_ids.len(),
                "target has extra Unit slots but no empty-mesh template supplied"
            );
            0
        }
    } else {
        0
    };

    if let Some(p) = progress {
        p.stage(target_name, "writing output");
    }
    let out_dir = out_root.join(super::safe_filename(target_name));
    std::fs::create_dir_all(&out_dir)
        .wrap_err_with(|| format!("create dir {}", out_dir.display()))?;
    let out_path = out_dir.join(patch_suffix);
    new_patch.write_files(&out_path)?;

    tracing::info!(
        target = %target_name,
        entries = written,
        file_id_remapped = remap.len(),
        slot_id_remapped = slot_remap.len(),
        padded,
        "migrated"
    );

    let _ = UNIT_ID; // touch for future use
    Ok(MigrationReport {
        target_hash: target_hash.to_string(),
        target_name: target_name.to_string(),
        out_path: Some(out_path),
        file_id_remapped: remap.len(),
        slot_id_remapped: slot_remap.len(),
        padded_units: padded,
        skipped_entries,
        skipped_types: plan.skipped_types.clone(),
        type_counts: plan.type_counts.clone(),
        warnings: Vec::new(),
    })
}

// ---------- reference remap (override) -----------------------------------

#[derive(Debug, Clone, Default)]
struct ReferenceRemap {
    file_ids: HashMap<u64, u64>,
    slot_ids: HashMap<u32, u32>,
    extra_unit_file_ids: Vec<u64>,
}

fn load_reference_remaps(path: Option<&Path>) -> crate::Result<HashMap<String, ReferenceRemap>> {
    let Some(path) = path else {
        return Ok(HashMap::new());
    };
    let text = std::fs::read_to_string(path)
        .wrap_err_with(|| format!("read reference remap {}", path.display()))?;
    let v: super::mode_b::PrecomputedTable = serde_json::from_str(&text)?;
    let mut out = HashMap::new();
    for (name, t) in v.targets {
        let parsed = t.parse()?;
        out.insert(
            name,
            ReferenceRemap {
                file_ids: parsed.file_ids,
                slot_ids: parsed.slot_ids,
                extra_unit_file_ids: parsed.extra_unit_file_ids,
            },
        );
    }
    Ok(out)
}
