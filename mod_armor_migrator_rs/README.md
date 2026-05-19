# mod_armor_migrator (Rust)

Rust port of the Python `mod_armor_migrator` tool. Takes a Helldivers 2 armor
mod patch (the `9ba626afa44a3aa3.patch_0` trio) and re-keys it to every other
armor archive, producing one ready-to-drop variant per target.

## Status

| Path | State |
|---|---|
| **Mode B** (precomputed `remap.json`) | ✅ full port — no game install required |
| **Mode A** (compute remap from game `data/`) | ⚠️ ordinal remap + reference override works; **Unit geometry matching is not yet ported** — Mode A without `--reference-remap-json` will fail with a clear error |
| LEGACY package read/write | ✅ |
| DSAR (LZ4 block) decompression | ✅ |
| Slim `bundles.*.nxa` reassembly | ❌ not yet implemented |
| Empty-mesh padding (sanitized + verbatim) | ✅ |
| Interactive CLI fill-in (no-args UX) | ✅ |
| Parallelism (rayon, per-target) | ✅ |

Mode A geometry-based Unit matching ports the public API surface but the inner
~1700 lines of distance scoring + body-pair tie-breaking heuristics still
require validation against real game archives. Use Mode B in the meantime.

## Build

```
cargo build --release
./target/release/mod_armor_migrator --help
```

## Usage

### Mode B (most common — no game install needed)

```
mod_armor_migrator \
  --patch  path/to/your_mod/9ba626afa44a3aa3.patch_0 \
  --remap-json path/to/remap.json \
  --out-dir out/
```

`remap.json` comes from running the upstream `extract_remap.py` against a
hand-made reference variant set. See the Python tool's README for that
workflow.

### Mode A (when you have the game `data/` directory)

```
mod_armor_migrator \
  --patch  path/to/your_mod/9ba626afa44a3aa3.patch_0 \
  --data-dir /path/to/Helldivers_2/data \
  --reference-remap-json path/to/manual_remap.json \
  --out-dir out/
```

Without `--reference-remap-json`, Mode A currently errors out — geometry
matching is the only path to derive the Unit FileID remap and is not yet
ported.

### Interactive mode

Run with no arguments to be prompted for the patch path, mode, paths, category,
targets, and output directory:

```
mod_armor_migrator
```

The `--non-interactive` flag turns missing required args into fatal errors
rather than prompts.

### Flags

Mirrors the Python `argparse` interface 1:1. See `--help` for the full list.

```
--patch                       Source mod patch file
--out-dir                     Output directory
--data-dir                    Game data/ directory (Mode A)
--remap-json                  Precomputed remap.json (Mode B)
--reference-remap-json        Override remap (Mode A only)
--source                      Override source armor hash
--target a,b,c                Limit to these target hashes/names
--category                    archivehashes.json category (default: Armor)
--index                       Override archivehashes.json path
--empty-mesh-from             Custom empty Unit patch (else builtin)
--no-padding                  Disable padding extras with empty meshes
--empty-mesh-verbatim         Keep template bytes as-is (no sanitization)
--patch-suffix                Output filename inside each target dir
--experimental-partial-remap  Allow incomplete Unit remap (Mode A)
--mode auto|from-data|from-remap   Force mode (default: auto)
--non-interactive             Fail rather than prompt for missing args
-v / -vv / -vvv               INFO / DEBUG / TRACE logging
```

## Embedded assets

Two assets are embedded into the binary at build time via `include_bytes!` /
`include_str!`:

- `assets/archivehashes.json` — armor hash → name index (copied verbatim from
  the Python package).
- `assets/empty_mesh/{toc,gpu,stream}.bin` — single-vertex empty mesh used as
  the default padding template.

`build.rs` asserts all three are present and non-empty (stream is allowed empty
for the default 1-vertex mesh) so a missed extraction fails the build loudly.

### Re-extracting `empty_mesh/*.bin` from the Python source

If you need to regenerate the empty mesh assets from the upstream
`_builtin_empty_mesh.py`:

```bash
python3 -c "
import sys, pathlib
sys.path.insert(0, '.')
from mod_armor_migrator._builtin_empty_mesh import TOC_DATA, GPU_DATA, STREAM_DATA
d = pathlib.Path('mod_armor_migrator_rs/assets/empty_mesh')
d.mkdir(parents=True, exist_ok=True)
(d/'toc.bin').write_bytes(TOC_DATA)
(d/'gpu.bin').write_bytes(GPU_DATA)
(d/'stream.bin').write_bytes(STREAM_DATA)
"
```

Run from the workspace root.

## Testing

`cargo test` runs ~22 unit tests covering:

- murmur64/32 vectors (regression-pinned against the TypeID constants).
- Archive LEGACY round-trip + minimum-size padding + 64B alignment.
- FileID / SlotID rewrite (Unit header refs, MaterialIDs, whole-blob u32 scan,
  Material TexIDs).
- Customization name extraction (regex-free byte scanner).
- Builtin empty mesh template loads + sanitized audit is non-drawing.
- DSAR header magic validation.
- `remap.json` hex/decimal key parsing.
- archivehashes.json schema parsing.

There are no end-to-end integration tests against real game data — that
requires a game install and is left to manual verification.

## Layout

```
src/
  archive/       LEGACY package + DSAR decompression
  cli/           clap args, dialoguer fill-in, tracing, indicatif progress
  migrator/      orchestration: Mode A (mode_a.rs) + Mode B (mode_b.rs)
  padding/       empty Unit template + sanitize + pad_patch
  unit/          names, semantic key match, geometry stub, body shape stub
  refs.rs        FileID + SlotID rewrite inside Unit/Material blobs
  hashing.rs     murmur64 + murmur32 (high-32 truncation)
  constants.rs   TypeID + magic constants + alignment helpers
  error.rs       eyre type aliases + typed error enum
  index.rs       archivehashes.json loader (builtin via include_str!)
```

## Differences from the Python tool

- All printing routed through `tracing`; no direct stdout/stderr from the
  library. CLI prints a colored summary at the end.
- Per-target migration runs in parallel via `rayon::par_iter`.
- Empty-mesh assets are embedded into the binary (no runtime base64 decode).
- Interactive mode (dialoguer) is added — Python is flags-only.
- `--mode` flag added (auto / from-data / from-remap) for explicit control.
- `--non-interactive` flag added (fails fast in CI / scripts).
