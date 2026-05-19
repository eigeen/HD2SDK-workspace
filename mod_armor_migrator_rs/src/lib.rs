//! Migrate Helldivers 2 armor mod patches across all armor archives.
//!
//! Library entry points live under [`migrator`].

pub mod archive;
pub mod cli;
pub mod constants;
pub mod error;
pub mod hashing;
pub mod index;
pub mod migrator;
pub mod padding;
pub mod refs;
pub mod unit;

pub use error::{MigratorError, Result};
pub use index::ArchiveIndex;
pub use migrator::{
    MigrateAllOpts, MigrateFromRemapOpts, MigrationReport, ProgressSink, migrate_all,
    migrate_from_remap_json,
};
pub use padding::{EmptyUnitTemplate, PaddingMode, builtin_template, extract_template};
