//! Mode B: load precomputed remap.json.
//!
//! Mirrors the input shape produced by Python's `extract_remap.py`:
//!
//! ```json
//! {
//!   "patch_filename": "9ba626afa44a3aa3.patch_0",
//!   "targets": {
//!     "Armor Name": {
//!       "file_ids":   {"src_decimal_or_hex": "tgt", ...},
//!       "slot_ids":   {"src": "tgt", ...},
//!       "extra_unit_file_ids": [...]
//!     }
//!   }
//! }
//! ```

use eyre::WrapErr;
use serde::Deserialize;
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct PrecomputedTable {
    #[serde(default = "default_patch_filename")]
    pub patch_filename: String,
    #[serde(default)]
    pub targets: HashMap<String, TargetRemap>,
}

fn default_patch_filename() -> String {
    "9ba626afa44a3aa3.patch_0".to_string()
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct TargetRemap {
    #[serde(default)]
    pub file_ids: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub slot_ids: HashMap<String, serde_json::Value>,
    #[serde(default)]
    pub extra_unit_file_ids: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Default)]
pub struct ParsedTarget {
    pub file_ids: HashMap<u64, u64>,
    pub slot_ids: HashMap<u32, u32>,
    pub extra_unit_file_ids: Vec<u64>,
}

impl TargetRemap {
    pub fn parse(&self) -> crate::Result<ParsedTarget> {
        let mut file_ids: HashMap<u64, u64> = HashMap::new();
        for (k, v) in &self.file_ids {
            let key = parse_u64_loose(k)?;
            let val = parse_u64_from_value(v)?;
            file_ids.insert(key, val);
        }
        let mut slot_ids: HashMap<u32, u32> = HashMap::new();
        for (k, v) in &self.slot_ids {
            let key = parse_u32_loose(k)?;
            let val = parse_u32_from_value(v)?;
            slot_ids.insert(key, val);
        }
        let mut extra_unit_file_ids: Vec<u64> = Vec::with_capacity(self.extra_unit_file_ids.len());
        for v in &self.extra_unit_file_ids {
            extra_unit_file_ids.push(parse_u64_from_value(v)?);
        }
        Ok(ParsedTarget {
            file_ids,
            slot_ids,
            extra_unit_file_ids,
        })
    }
}

pub fn load(path: &Path) -> crate::Result<PrecomputedTable> {
    let text = std::fs::read_to_string(path)
        .wrap_err_with(|| format!("read remap.json {}", path.display()))?;
    let v: PrecomputedTable = serde_json::from_str(&text)?;
    Ok(v)
}

fn parse_u64_loose(s: &str) -> crate::Result<u64> {
    parse_uint_str(s).map(|v| v as u64)
}

fn parse_u32_loose(s: &str) -> crate::Result<u32> {
    let v = parse_uint_str(s)?;
    if v > u128::from(u32::MAX) {
        eyre::bail!("u32 overflow parsing {s}");
    }
    Ok(v as u32)
}

fn parse_uint_str(s: &str) -> crate::Result<u128> {
    let s = s.trim();
    if let Some(rest) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        return u128::from_str_radix(rest, 16)
            .wrap_err_with(|| format!("invalid hex u128 {s}"));
    }
    s.parse::<u128>()
        .wrap_err_with(|| format!("invalid decimal u128 {s}"))
}

fn parse_u64_from_value(v: &serde_json::Value) -> crate::Result<u64> {
    if let Some(n) = v.as_u64() {
        return Ok(n);
    }
    if let Some(n) = v.as_i64() {
        return Ok(n as u64);
    }
    if let Some(s) = v.as_str() {
        return parse_u64_loose(s);
    }
    eyre::bail!("expected u64-compatible JSON value, got {v}")
}

fn parse_u32_from_value(v: &serde_json::Value) -> crate::Result<u32> {
    if let Some(n) = v.as_u64() {
        if n > u64::from(u32::MAX) {
            eyre::bail!("u32 overflow {n}");
        }
        return Ok(n as u32);
    }
    if let Some(s) = v.as_str() {
        return parse_u32_loose(s);
    }
    eyre::bail!("expected u32-compatible JSON value, got {v}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hex_and_decimal_keys() {
        let raw = r#"{
            "patch_filename": "9ba626afa44a3aa3.patch_0",
            "targets": {
                "FooArmor": {
                    "file_ids": {"123": 456, "0xff": "0x100"},
                    "slot_ids": {"3735928559": "0xCAFEBABE"},
                    "extra_unit_file_ids": [42, "100"]
                }
            }
        }"#;
        let table: PrecomputedTable = serde_json::from_str(raw).unwrap();
        let parsed = table.targets["FooArmor"].parse().unwrap();
        assert_eq!(parsed.file_ids[&123], 456);
        assert_eq!(parsed.file_ids[&0xff], 0x100);
        assert_eq!(parsed.slot_ids[&3735928559], 0xCAFE_BABE);
        assert_eq!(parsed.extra_unit_file_ids, vec![42u64, 100]);
    }
}
