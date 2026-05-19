//! Geometry-based Unit matching for armor migration.
//!
//! Public surface mirrors `mod_armor_migrator/unit_geometry.py`. The core
//! distance scoring + greedy first-fit matching engine (and the body-pair
//! tie-breaking it delegates to [`crate::unit::body_shape`]) is **not yet
//! ported** in this revision — it spans ~1700 lines of intricate Python
//! across both modules and ports require real game archives to validate.
//!
//! The intended call site is Mode A only (`migrator/mode_a.rs`). Mode B
//! consumers reading a precomputed `remap.json` bypass this module entirely.

use crate::archive::{StreamToc, TocEntry};
use crate::constants::UNIT_ID;
use crate::unit::names::{UnitCustomizationName, extract_customization_name};
use std::collections::{HashMap, HashSet};

pub type Point3 = (f64, f64, f64);

#[derive(Debug, Clone)]
pub struct GeometryMatchSettings {
    pub max_score: f64,
    pub min_margin: f64,
    pub sample_count: usize,
    pub quantiles: Vec<f64>,
}

impl Default for GeometryMatchSettings {
    fn default() -> Self {
        Self {
            max_score: 1.5,
            min_margin: 0.0,
            sample_count: 96,
            quantiles: vec![0.10, 0.25, 0.50, 0.75, 0.90],
        }
    }
}

#[derive(Debug, Clone)]
pub struct UnitGeometrySignature {
    pub file_id: u64,
    pub points: Vec<Point3>,
    pub sample_points: Vec<Point3>,
    pub vertex_count: usize,
    pub center: Point3,
    pub extents: Point3,
    pub diagonal: f64,
    pub axis_quantiles: Vec<f64>,
    pub radial_quantiles: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct UnitGeometryIssue {
    pub source_file_id: u64,
    pub reason: String,
    pub candidates: Vec<u64>,
}

#[derive(Debug, Clone, Default)]
pub struct UnitGeometryRemap {
    pub remap: HashMap<u64, u64>,
    pub expanded_remap: HashMap<u64, Vec<u64>>,
    pub match_levels: HashMap<u64, String>,
    pub scores: HashMap<u64, f64>,
    pub margins: HashMap<u64, f64>,
    pub rankings: HashMap<u64, Vec<(u64, f64)>>,
    pub missing: Vec<UnitGeometryIssue>,
    pub ambiguous: Vec<UnitGeometryIssue>,
    pub extra_unit_file_ids: Vec<u64>,
    pub claimed_target_file_ids: HashSet<u64>,
    pub empty_source_file_ids: HashSet<u64>,
}

impl UnitGeometryRemap {
    pub fn is_complete(&self) -> bool {
        self.missing.is_empty() && self.ambiguous.is_empty()
    }
}

/// Compute the FileID remap from source Units to target Units via geometry.
///
/// Currently returns an error — callers should run Mode B with a precomputed
/// `remap.json` until this is ported. The `_settings` arg shape is preserved so
/// future work doesn't change call sites.
pub fn build_unit_geometry_remap(
    _patch: &StreamToc,
    _source: &StreamToc,
    _target: &StreamToc,
    _settings: &GeometryMatchSettings,
) -> crate::Result<UnitGeometryRemap> {
    eyre::bail!(
        "geometry-based Unit matching (Mode A) is not yet implemented in the Rust port; \
         use --remap-json (Mode B) with a precomputed remap"
    )
}

/// Return Unit geometry signatures indexed by FileID for every parseable entry.
///
/// Reads the same fields the Python implementation does but currently emits
/// **empty** signatures — point parsing is left as TODO.
pub fn build_archive_signatures(
    toc: &StreamToc,
    _settings: &GeometryMatchSettings,
) -> HashMap<u64, UnitGeometrySignature> {
    toc.entries
        .iter()
        .filter(|e| e.type_id == UNIT_ID)
        .filter_map(|e| build_unit_signature(e, _settings).map(|s| (e.file_id, s)))
        .collect()
}

pub fn build_unit_signature(
    entry: &TocEntry,
    _settings: &GeometryMatchSettings,
) -> Option<UnitGeometrySignature> {
    // TODO: port _read_stream_layouts / _read_mesh_layouts / _read_transform_matrices /
    // _mesh_points / _downsample_points / _bounding_box_stats / _axis_quantiles /
    // _radial_quantiles from unit_geometry.py.
    if entry.toc_data.len() < 0x80 {
        return None;
    }
    Some(UnitGeometrySignature {
        file_id: entry.file_id,
        points: Vec::new(),
        sample_points: Vec::new(),
        vertex_count: 0,
        center: (0.0, 0.0, 0.0),
        extents: (0.0, 0.0, 0.0),
        diagonal: 0.0,
        axis_quantiles: Vec::new(),
        radial_quantiles: Vec::new(),
    })
}

pub fn format_unit_geometry_issues(result: &UnitGeometryRemap, limit: usize) -> String {
    let mut issues: Vec<&UnitGeometryIssue> = result
        .missing
        .iter()
        .chain(result.ambiguous.iter())
        .collect();
    let total = issues.len();
    issues.truncate(limit);
    let mut parts = Vec::new();
    for issue in &issues {
        let suffix = if issue.candidates.is_empty() {
            String::new()
        } else {
            format!(", candidates={:?}", issue.candidates)
        };
        parts.push(format!(
            "{}: {}{}",
            issue.source_file_id, issue.reason, suffix
        ));
    }
    if total > limit {
        parts.push(format!("... {} more", total - limit));
    }
    parts.join("; ")
}

/// Approximate symmetric cloud distance + quantile blend used by Python's
/// `score_signatures`. Currently unimplemented — returns NaN.
pub fn score_signatures(
    _source: &UnitGeometrySignature,
    _target: &UnitGeometrySignature,
) -> f64 {
    f64::NAN
}

#[allow(dead_code)]
fn unit_customization_name(entry: &TocEntry) -> Option<UnitCustomizationName> {
    extract_customization_name(&entry.toc_data)
}
