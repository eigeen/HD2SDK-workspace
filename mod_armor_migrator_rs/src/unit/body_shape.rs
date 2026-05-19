//! Body shape / variant tie-breaking for geometry matches.
//!
//! Public surface mirrors `mod_armor_migrator/unit_body_shape.py`. Internal
//! pair-scoring + assignment heuristics are NOT yet ported (~600 lines of
//! Python with depth-extent thresholds and named/unknown twin logic that
//! requires game data to validate).

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum BodyType {
    Stocky,
    Slim,
    Any,
    Unknown,
}

impl BodyType {
    pub fn as_str(&self) -> &'static str {
        match self {
            BodyType::Stocky => "Stocky",
            BodyType::Slim => "Slim",
            BodyType::Any => "Any",
            BodyType::Unknown => "Unknown",
        }
    }

    pub fn from_str_normalize(s: &str) -> Self {
        match s {
            "Stocky" => BodyType::Stocky,
            "Slim" => BodyType::Slim,
            "Any" => BodyType::Any,
            _ => BodyType::Unknown,
        }
    }
}

#[allow(dead_code)]
pub fn detect_body_type(toc_data: &[u8], _gpu_data: &[u8]) -> BodyType {
    let s = crate::unit::names::body_variant(toc_data);
    BodyType::from_str_normalize(s)
}

/// Stub for `apply_body_variant_pair_tiebreak`. Will refine geometry matches
/// using paired Stocky/Slim variants — currently a no-op pending port.
#[allow(dead_code)]
pub fn apply_body_variant_pair_tiebreak() {
    // TODO: port from unit_body_shape.py
}
