# mod_armor_migrator

Standalone Python tool that takes a Helldivers 2 armor mod (a `patch_N` file
trio) and re-keys it to every other armor archive, producing one ready-to-drop
variant per target.

No Blender required. Runs against the game's `data/` directory.

## Install

Python 3.9+. Only optional dependency:

```
pip install lz4    # only needed if your data/ uses DSAR-compressed archives
```

If `lz4` isn't installed the tool will try to import the LZ4 module vendored
inside the parent Blender plugin (`../utils/lz4_311/block.py`). With a normal
non-Slim install you don't need LZ4 at all — armor archives are uncompressed.

## How it works

Each armor archive in `data/<hash>` stores Unit, Material, and Texture
resources. Unit entries are mesh-backed armor slots: their FileIDs are hashes,
and archive order is not a stable identity. Unit migration therefore uses an
explicit trusted remap from `--reference-remap-json` / `--remap-json` when
provided; otherwise it compares parsed Unit vertex distributions directly.

For non-Unit resources that are structurally parallel, the tool can still use
archive order as a fallback:

```
source.materials[0]  →  target.materials[0]
source.materials[1]  →  target.materials[1]
source.textures[i]   →  target.textures[i]
...
```

For each entry in the mod patch:
1. Its top-level `TocEntry.FileID` is rewritten to the target's matching
   FileID from the geometry remap (Unit) or safe fallback remap (non-Unit).
2. References baked inside the entry's `TocData` are rewritten too:
   - **Unit**: `UnkRef1`, `BonesRef`, `CompositeRef`, `UnkRef2`,
     `StateMachineRef`, plus `MaterialIDs[]` in the materials slot.
   - **Material**: `TexIDs[]`.

The raw GPU/Stream payloads (vertex buffers, DDS pixel data) are copied
through untouched — that *is* the mod's content.

If source and target have differing counts for a non-Unit TypeID, that type is
skipped for that target (with a warning); Unit count differences are handled by
geometry matching plus empty-mesh padding for target-only parts.

## Usage

```
python -m mod_armor_migrator \
    --patch  /path/to/your_mod/9ba626afa44a3aa3.patch_0 \
    --data-dir /path/to/Helldivers_2/data \
    --out-dir  ./out
```

Auto-detects the source armor from the patch's FileIDs; you can override:

```
python -m mod_armor_migrator \
    --patch /path/to/mod.patch_0 \
    --data-dir /path/to/data \
    --out-dir ./out \
    --source 1d6dc4216e7ce52d            # A-9 Helljumper
```

Restrict to a subset of targets:

```
python -m mod_armor_migrator ... \
    --target 1d6dc4216e7ce52d,f32d3723bfe55d2f
```

Re-key against Helmet / Cape categories instead of Armor:

```
python -m mod_armor_migrator ... --category Helmet
```

## Output layout

Per target armor:

```
out/
  <target_hash>_<Armor Name>/
    <target_hash>.patch_0
    <target_hash>.patch_0.gpu_resources
    <target_hash>.patch_0.stream
```

Drop the entire trio into your game's `data/` directory to install. Change
`--patch-suffix patch_3` (etc.) if you already have other patches in use.

## Two modes

### Mode A — derive remap from game archives (requires non-Slim install)

```
python -m mod_armor_migrator \
    --patch /path/to/mod.patch_0 \
    --data-dir /path/to/Helldivers_2/data \
    --out-dir ./out
```

### Mode B — use a precomputed remap.json (works on Slim installs)

If you have *one* hand-made variant of the mod, plus several reference
variants for other armors (e.g. from another modder's release), you can
bootstrap a table and migrate to those targets without reading game archives:

```
# 1. Build the table from manually-made variants:
python -m mod_armor_migrator.extract_remap \
    --source-name "AF-52 Lockdown" \
    --reference-dir "/path/to/Mod with 24 variants" \
    --out remap.json

# 2. Apply to any patch (need not be one of the originals):
python -m mod_armor_migrator \
    --patch /path/to/your.patch_0 \
    --remap-json remap.json \
    --out-dir ./out
```

See `VALIDATION.md` for a measured comparison against a 24-variant ground-truth.

## Empty-mesh padding (extra target slots)

Some armors have parts (backpacks, mounts) that the source mod doesn't cover.
After FileID remap, those target slots still render the
*original armor's* parts. The tool auto-fills them with a single-point empty
mesh entry, hiding them.

- `extract_remap.py` records each target's extra Unit FileIDs in
  `remap.json["targets"][name]["extra_unit_file_ids"]`.
- Disable padding with `--no-padding`.

### Empty-mesh template

The project ships a default template at `_builtin_empty_mesh.py` — a
base64-encoded Blender/SDK export that is used when you don't pass
`--empty-mesh-from`. No setup needed.

The bundled example is treated as raw source material, not blindly trusted.
Before padding, the default path sanitizes the Unit blob:

- non-zero global Material FileIDs are rewritten to `0`
- StreamInfo / MeshSectionInfo index counts are rewritten to `0`
- top-level header references must already be absent (`0`)

`python -m mod_armor_migrator.inspect_template` audits the built-in template
and prints both raw and sanitized dependency/draw counts.

If you want to use your own (different vertex count, different bind pose,
etc.), author one in Blender, export through the SDK as a `<base>.patch_0`
trio with exactly one Unit entry, then:

```
python -m mod_armor_migrator ... \
    --empty-mesh-from /path/to/your_empty.patch_0 \
    --empty-mesh-verbatim
```

`--empty-mesh-verbatim` writes the template's `TocData` byte-for-byte — the
tool only changes the top-level `FileID` on the new entry. Without that
flag, the tool sanitizes the template first and then runs the slot-ID remap
pass over the template's TocData. Use verbatim mode only when you have
audited the template yourself and intentionally want those exact bytes.

To regenerate `_builtin_empty_mesh.py` from a new source mesh:

```python
import base64, textwrap, os
files = {'toc': '...path/to/<base>.patch_0',
         'gpu': '...path/to/<base>.patch_0.gpu_resources',
         'stream': '...path/to/<base>.patch_0.stream'}
with open('mod_armor_migrator/_builtin_empty_mesh.py', 'w') as out:
    out.write('import base64\n\n')
    for k, p in files.items():
        b64 = base64.b64encode(open(p,'rb').read()).decode('ascii')
        out.write(f'_B64_{k.upper()} = (\n')
        for ln in textwrap.wrap(b64, 76) or ['']:
            out.write(f'    "{ln}"\n')
        out.write(')\n\n')
    out.write('TOC_DATA = base64.b64decode(_B64_TOC)\n')
    out.write('GPU_DATA = base64.b64decode(_B64_GPU)\n')
    out.write('STREAM_DATA = base64.b64decode(_B64_STREAM)\n')
```

### How it ends up in the patch

```
your_empty.patch_0 ──── extract Unit #0 (smallest GPU) ──→ template
                                                              │
                                                              │ for each extra
                                                              │ target slot:
                                                              ▼
                                              new TocEntry
                                                file_id  = target slot FileID
                                                type_id  = UnitID
                                                toc_data = sanitized template.toc_data
                                                           (or raw bytes with
                                                            --empty-mesh-verbatim)
                                                gpu_data = template.gpu_data   (verbatim)
                                                stream_data = template.stream_data
```

The new entry is appended to the variant's patch and goes through the
standard `StreamToc.write()` pass, which lays out offsets / sizes / type
table headers correctly.

Validation: across the SuperEarth Stalker test set, 3 target armors needed
extras (CM-09 Bonesnapper +1, O-2 Heavy Operator +1, O-44 Bonded Pilot +2).
The tool's output matches the manual variants on entry count, FileID list,
and TypeID layout for all three.

## Caveats (measured)

- **Per-target weight rebake is not reproduced.** When Blender re-exports the
  same mesh against a different armor's skeleton it re-bakes
  `vertex_bone_indices` / `vertex_weights` against that target's bone layout,
  so the GPU bytes differ even though the mesh shape is identical. The tool
  copies the *source* GPU buffers verbatim; HD2's mod loader applies the
  geometry to whichever skeleton the target armor uses (this is the standard
  HD2 mod model — geometry travels, the skeleton stays target-native).
  Across HD2's largely standardised humanoid armor skeletons this is the
  normal path and produces correct results in-game.
- **Material slot IDs are uint32 murmur32 hashes of per-armor slot names.**
  Without access to source + target armor archives (Mode A), the tool needs a
  precomputed slot-id remap (Mode B).
- **Composite armors**: `CompositeRef` is rewritten but the CompositeMesh
  body itself is not rebuilt. Mods that ship their own Composite are
  migrated as plain entries.
- **Slim installs (no `9ba626afa44a3aa3`)**: BUNDLED-format archives in
  `bundles.nxa` are not read by Mode A. Use Mode B instead.

## Implementation files

| file | what |
|---|---|
| `archive.py` | LEGACY package read/write + DSAR decompress |
| `refs.py`    | Rewrite Unit/Material header references |
| `unit_geometry.py` | Parse Unit vertex positions and match armor slots by geometry |
| `migrator.py`| Source autodetect + per-target migration |
| `__main__.py`| CLI |
| `constants.py` | Type IDs and magic numbers |
| `hashing.py` | murmur64/32 (copied from parent plugin) |
| `archivehashes.json` | Archive → friendly-name index (copied) |
