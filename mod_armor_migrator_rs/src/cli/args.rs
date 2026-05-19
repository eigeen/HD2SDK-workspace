use clap::{ArgAction, Parser, ValueEnum};
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum MigrationMode {
    /// Pick automatically based on which of --data-dir / --remap-json is set.
    Auto,
    /// Mode A: derive remap from game data/ archives.
    FromData,
    /// Mode B: load precomputed remap.json.
    FromRemap,
}

#[derive(Debug, Parser)]
#[command(name = "mod_armor_migrator", version, about, long_about = None)]
pub struct Cli {
    /// Path to the source mod patch (e.g. 9ba626afa44a3aa3.patch_0).
    #[arg(long)]
    pub patch: Option<PathBuf>,

    /// Output directory for migrated variants.
    #[arg(long = "out-dir")]
    pub out_dir: Option<PathBuf>,

    /// Mode A: game data/ directory (mutually exclusive with --remap-json).
    #[arg(long = "data-dir")]
    pub data_dir: Option<PathBuf>,

    /// Override source archive hex hash; auto-detected if omitted (Mode A).
    #[arg(long)]
    pub source: Option<String>,

    /// Comma-separated target hashes (or names). Empty = all.
    #[arg(long, value_delimiter = ',')]
    pub target: Vec<String>,

    /// Archive category in archivehashes.json.
    #[arg(long, default_value = "Armor")]
    pub category: String,

    /// Override archivehashes.json path; defaults to the bundled copy.
    #[arg(long)]
    pub index: Option<PathBuf>,

    /// Mode B: path to precomputed remap.json (mutually exclusive with --data-dir).
    #[arg(long = "remap-json")]
    pub remap_json: Option<PathBuf>,

    /// Reference remap.json applied as an OVERRIDE on top of Mode A's computed remap.
    #[arg(long = "reference-remap-json")]
    pub reference_remap_json: Option<PathBuf>,

    /// Custom empty mesh patch to use as padding template; defaults to builtin.
    #[arg(long = "empty-mesh-from")]
    pub empty_mesh_from: Option<PathBuf>,

    /// Disable empty-mesh padding for target-only Unit slots.
    #[arg(long = "no-padding")]
    pub no_padding: bool,

    /// Use empty mesh template bytes verbatim (no sanitization).
    #[arg(long = "empty-mesh-verbatim")]
    pub empty_mesh_verbatim: bool,

    /// Output patch filename inside each target's directory.
    #[arg(long = "patch-suffix", default_value = "9ba626afa44a3aa3.patch_0")]
    pub patch_suffix: String,

    /// Emit incomplete remaps for testing (Mode A only).
    #[arg(long = "experimental-partial-remap")]
    pub experimental_partial_remap: bool,

    /// Increase logging verbosity: -v info, -vv debug, -vvv trace.
    #[arg(short, long, action = ArgAction::Count)]
    pub verbose: u8,

    /// Fail (don't prompt) when required args are missing.
    #[arg(long = "non-interactive")]
    pub non_interactive: bool,

    /// Force a particular migration mode (defaults to Auto).
    #[arg(long = "mode", value_enum, default_value_t = MigrationMode::Auto)]
    pub mode: MigrationMode,
}

impl Cli {
    pub fn padding_mode(&self) -> crate::padding::PaddingMode {
        if self.no_padding {
            crate::padding::PaddingMode::Disabled
        } else if self.empty_mesh_verbatim {
            crate::padding::PaddingMode::Verbatim
        } else {
            crate::padding::PaddingMode::Sanitized
        }
    }
}
