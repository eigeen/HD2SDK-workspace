//! Extract Unit customization labels from TocData.
//!
//! Mirrors `mod_armor_migrator/unit_names.py`. Strategy:
//! 1. Scan the blob for ASCII strings matching
//!    `HelldiverCustomization(BodyType|Slot|Weight|PieceType)_<ident>`.
//! 2. For each of the four prefixes, the LAST occurrence wins (this matches
//!    the Python `matches[-1]` behavior).
//! 3. If any of the four are missing, fall back to a bonehash-based inference
//!    (currently unimplemented — Python depends on an external
//!    `hashlists/bonehash.txt` not bundled here).

const BODY: &str = "HelldiverCustomizationBodyType_";
const SLOT: &str = "HelldiverCustomizationSlot_";
const WEIGHT: &str = "HelldiverCustomizationWeight_";
const PIECE: &str = "HelldiverCustomizationPieceType_";

/// Customization metadata pulled from a Unit's TocData.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct UnitCustomizationName {
    pub body_type: String,
    pub slot: String,
    pub weight: String,
    pub piece_type: String,
}

impl UnitCustomizationName {
    /// `BodyType/Slot/Weight/PieceType` — NameFromMesh-style customization path.
    pub fn label(&self) -> String {
        format!(
            "{}/{}/{}/{}",
            self.body_type, self.slot, self.weight, self.piece_type
        )
    }

    /// Normalize `body_type` to one of `Stocky`, `Slim`, `Any`, else `Unknown`.
    pub fn body_variant(&self) -> &'static str {
        match self.body_type.as_str() {
            "Any" => "Any",
            "Stocky" => "Stocky",
            "Slim" => "Slim",
            _ => "Unknown",
        }
    }
}

pub fn extract_customization_name(toc_data: &[u8]) -> Option<UnitCustomizationName> {
    let values = customization_values(toc_data);
    if values.iter().all(Option::is_some) {
        let [body, slot, weight, piece] = values;
        return Some(UnitCustomizationName {
            body_type: body.unwrap_or_default(),
            slot: slot.unwrap_or_default(),
            weight: weight.unwrap_or_default(),
            piece_type: piece.unwrap_or_default(),
        });
    }
    // Bonehash fallback (Python: `_extract_bonehash_customization_name`)
    // requires `hashlists/bonehash.txt`. Not yet ported.
    None
}

pub fn body_variant(toc_data: &[u8]) -> &'static str {
    extract_customization_name(toc_data)
        .as_ref()
        .map(UnitCustomizationName::body_variant)
        .unwrap_or("Unknown")
}

/// Returns `[body, slot, weight, piece]`, each `Some(suffix)` if found.
pub(crate) fn customization_values(toc_data: &[u8]) -> [Option<String>; 4] {
    let matches = scan_customization_strings(toc_data);
    let prefixes = [BODY, SLOT, WEIGHT, PIECE];
    let mut out: [Option<String>; 4] = Default::default();
    for (i, prefix) in prefixes.iter().enumerate() {
        let last = matches
            .iter()
            .filter(|s| s.starts_with(prefix))
            .next_back()
            .map(|s| s[prefix.len()..].to_string());
        out[i] = last;
    }
    out
}

/// Find every `HelldiverCustomization(BodyType|Slot|Weight|PieceType)_<ident>`
/// occurrence in the byte stream. Pure scanning — no regex crate dep.
fn scan_customization_strings(data: &[u8]) -> Vec<String> {
    const HEAD: &[u8] = b"HelldiverCustomization";
    let mut out = Vec::new();
    let mut i = 0;
    while i + HEAD.len() < data.len() {
        if &data[i..i + HEAD.len()] != HEAD {
            i += 1;
            continue;
        }
        // Match one of the four kinds after HEAD.
        let after = &data[i + HEAD.len()..];
        let kind_len = match after {
            x if x.starts_with(b"BodyType_") => "BodyType_".len(),
            x if x.starts_with(b"Slot_") => "Slot_".len(),
            x if x.starts_with(b"Weight_") => "Weight_".len(),
            x if x.starts_with(b"PieceType_") => "PieceType_".len(),
            _ => {
                i += 1;
                continue;
            }
        };
        let start = i;
        let body_start = start + HEAD.len() + kind_len;
        let mut end = body_start;
        while end < data.len() && is_ident_byte(data[end]) {
            end += 1;
        }
        if end > body_start {
            if let Ok(s) = std::str::from_utf8(&data[start..end]) {
                out.push(s.to_string());
            }
        }
        i = end.max(i + 1);
    }
    out
}

#[inline]
fn is_ident_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_all_four_fields() {
        let mut blob = Vec::new();
        blob.extend_from_slice(b"\x00\x00garbage");
        blob.extend_from_slice(b"HelldiverCustomizationBodyType_Stocky\x00");
        blob.extend_from_slice(b"HelldiverCustomizationSlot_Torso\x00");
        blob.extend_from_slice(b"HelldiverCustomizationWeight_Medium\x00");
        blob.extend_from_slice(b"HelldiverCustomizationPieceType_Armor\x00");
        let got = extract_customization_name(&blob).expect("present");
        assert_eq!(got.body_type, "Stocky");
        assert_eq!(got.slot, "Torso");
        assert_eq!(got.weight, "Medium");
        assert_eq!(got.piece_type, "Armor");
        assert_eq!(got.body_variant(), "Stocky");
        assert_eq!(got.label(), "Stocky/Torso/Medium/Armor");
    }

    #[test]
    fn last_occurrence_wins() {
        let mut blob = Vec::new();
        blob.extend_from_slice(b"HelldiverCustomizationBodyType_First\x00");
        blob.extend_from_slice(b"HelldiverCustomizationSlot_Slot\x00");
        blob.extend_from_slice(b"HelldiverCustomizationWeight_W\x00");
        blob.extend_from_slice(b"HelldiverCustomizationPieceType_P\x00");
        blob.extend_from_slice(b"HelldiverCustomizationBodyType_Second\x00");
        let got = extract_customization_name(&blob).expect("present");
        assert_eq!(got.body_type, "Second");
    }

    #[test]
    fn missing_field_returns_none() {
        let blob = b"HelldiverCustomizationBodyType_Stocky\x00".to_vec();
        assert!(extract_customization_name(&blob).is_none());
    }
}
