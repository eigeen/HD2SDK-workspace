use color_eyre::eyre::Result;

fn main() -> Result<()> {
    color_eyre::install()?;
    mod_armor_migrator::cli::run()
}
