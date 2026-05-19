//! `bundles.*.nxa` slim install reassembly.
//!
//! TODO: port `_reassemble_from_bundles` from `archive.py`.

use std::path::Path;

#[allow(dead_code)]
pub fn reassemble(_data_dir: &Path, _file_id_hex: &str) -> crate::Result<Vec<u8>> {
    eyre::bail!("reassembly::reassemble not yet implemented")
}
