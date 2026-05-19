//! Dialoguer-based interactive fill-in for missing CLI arguments.
//!
//! Strategy: fields the user already provided on the command line win
//! unchanged. Anything still empty after parse is prompted for here. When
//! `--non-interactive` is set we bail with a clear message instead of
//! prompting.

use crate::cli::args::{Cli, MigrationMode};
use crate::index::ArchiveIndex;
use dialoguer::{theme::ColorfulTheme, Confirm, FuzzySelect, Input, MultiSelect, Select};
use eyre::WrapErr;
use std::path::PathBuf;
use walkdir::WalkDir;

pub fn fill_in(cli: &mut Cli, index: &ArchiveIndex) -> crate::Result<()> {
    let theme = ColorfulTheme::default();

    if cli.patch.is_none() {
        cli.patch = Some(prompt_patch_path(&theme, cli.non_interactive)?);
    }

    if cli.mode == MigrationMode::Auto {
        cli.mode = if cli.remap_json.is_some() {
            MigrationMode::FromRemap
        } else if cli.data_dir.is_some() {
            MigrationMode::FromData
        } else {
            prompt_mode(&theme, cli.non_interactive)?
        };
    }

    match cli.mode {
        MigrationMode::FromData if cli.data_dir.is_none() => {
            cli.data_dir = Some(prompt_path(
                &theme,
                "Game data directory",
                None,
                cli.non_interactive,
                "--data-dir",
            )?);
        }
        MigrationMode::FromRemap if cli.remap_json.is_none() => {
            cli.remap_json = Some(prompt_path(
                &theme,
                "Path to remap.json",
                None,
                cli.non_interactive,
                "--remap-json",
            )?);
        }
        _ => {}
    }

    let categories: Vec<&str> = index.categories().collect();
    if !categories.iter().any(|c| *c == cli.category) {
        if cli.non_interactive {
            eyre::bail!(
                "--category {:?} not present in archive index (have: {:?})",
                cli.category,
                categories
            );
        }
        let default_idx = categories
            .iter()
            .position(|c| *c == "Armor")
            .unwrap_or(0);
        let choice = Select::with_theme(&theme)
            .with_prompt("Armor category")
            .items(&categories)
            .default(default_idx)
            .interact()
            .wrap_err("category prompt")?;
        cli.category = categories[choice].to_string();
    }

    // Targets: in FromData mode we list real archives from the index.
    // In FromRemap mode we let the user free-text (defaults to "all").
    if cli.target.is_empty() && cli.mode == MigrationMode::FromData {
        let entries = index
            .category(&cli.category)
            .ok_or_else(|| eyre::eyre!("unknown category {:?}", cli.category))?;
        if !cli.non_interactive && !entries.is_empty() {
            let names: Vec<String> = entries
                .iter()
                .map(|a| format!("{}  ({})", a.name, a.hash))
                .collect();
            let chosen = MultiSelect::with_theme(&theme)
                .with_prompt("Targets (space to toggle, Enter to confirm; empty = all)")
                .items(&names)
                .interact()
                .wrap_err("targets prompt")?;
            cli.target = chosen.into_iter().map(|i| entries[i].hash.clone()).collect();
        }
    }

    if cli.out_dir.is_none() {
        cli.out_dir = Some(prompt_path(
            &theme,
            "Output directory",
            Some("out"),
            cli.non_interactive,
            "--out-dir",
        )?);
    }

    Ok(())
}

fn prompt_patch_path(
    theme: &ColorfulTheme,
    non_interactive: bool,
) -> crate::Result<PathBuf> {
    if non_interactive {
        eyre::bail!("--patch is required in --non-interactive mode");
    }
    let candidates = discover_patch_files(std::env::current_dir()?);
    if !candidates.is_empty() {
        let labels: Vec<String> = candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect();
        let idx = FuzzySelect::with_theme(theme)
            .with_prompt("Select patch file")
            .items(&labels)
            .default(0)
            .interact()
            .wrap_err("patch prompt")?;
        return Ok(candidates[idx].clone());
    }
    let raw: String = Input::with_theme(theme)
        .with_prompt("Path to patch file (e.g. 9ba626afa44a3aa3.patch_0)")
        .interact_text()
        .wrap_err("patch path input")?;
    Ok(PathBuf::from(raw))
}

fn discover_patch_files(root: PathBuf) -> Vec<PathBuf> {
    WalkDir::new(root)
        .max_depth(2)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().into_owned();
            if name.contains(".patch_") && !name.ends_with(".gpu_resources") && !name.ends_with(".stream")
            {
                Some(e.path().to_path_buf())
            } else {
                None
            }
        })
        .take(40)
        .collect()
}

fn prompt_mode(
    theme: &ColorfulTheme,
    non_interactive: bool,
) -> crate::Result<MigrationMode> {
    if non_interactive {
        eyre::bail!(
            "either --data-dir (Mode A) or --remap-json (Mode B) is required in --non-interactive mode"
        );
    }
    let labels = [
        "From game data/ (Mode A — needs game install)",
        "From precomputed remap.json (Mode B — Slim/no game install)",
    ];
    let idx = Select::with_theme(theme)
        .with_prompt("Migration mode")
        .items(&labels)
        .default(0)
        .interact()
        .wrap_err("mode prompt")?;
    Ok(match idx {
        0 => MigrationMode::FromData,
        _ => MigrationMode::FromRemap,
    })
}

fn prompt_path(
    theme: &ColorfulTheme,
    label: &str,
    default: Option<&str>,
    non_interactive: bool,
    flag: &str,
) -> crate::Result<PathBuf> {
    if non_interactive {
        eyre::bail!("{} is required in --non-interactive mode", flag);
    }
    let mut prompt = Input::<String>::with_theme(theme).with_prompt(label);
    if let Some(d) = default {
        prompt = prompt.default(d.into());
    }
    let raw = prompt.interact_text().wrap_err_with(|| format!("{label} prompt"))?;
    Ok(PathBuf::from(raw))
}

/// Confirm proceeding once the plan has been resolved.
pub fn confirm_run(theme: &ColorfulTheme, non_interactive: bool, summary: &str) -> crate::Result<bool> {
    if non_interactive {
        return Ok(true);
    }
    let ok = Confirm::with_theme(theme)
        .with_prompt(format!("{summary}\nProceed?"))
        .default(true)
        .interact()
        .wrap_err("confirm")?;
    Ok(ok)
}
