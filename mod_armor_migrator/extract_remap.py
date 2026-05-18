"""
Build a `remap.json` file from a SET of reference patches that all encode the
same mod, each retargeted to a different armor.

For each pair of (source, target) we derive:
  * FileID remap : pair entries by (TypeID, ordinal); record uint64 pairs
  * SlotID remap : after applying the FileID remap to the source's Unit blobs,
                   scan aligned uint32 words and record uint32 pairs where
                   they differ from the target

This lets you bootstrap a migration table from manually-made variants —
useful when you can't read the per-armor archives directly (Slim install).

Usage:
    python -m mod_armor_migrator.extract_remap \
        --source-name "AF-52 Lockdown" \
        --reference-dir /path/to/SuperEarth Stalker \
        --out remap.json
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections import defaultdict
from typing import Dict, Tuple

from .archive import StreamToc
from .constants import UnitID
from . import refs


def discover_uint32_pairs(unit_a: bytes, unit_b: bytes) -> Dict[int, int]:
    pairs: Dict[int, int] = {}
    if len(unit_a) != len(unit_b):
        return pairs
    for pos in range(0, len(unit_a) - 3, 4):
        a, = struct.unpack_from("<I", unit_a, pos)
        b, = struct.unpack_from("<I", unit_b, pos)
        if a != b:
            existing = pairs.get(a)
            if existing is not None and existing != b:
                # Conflict — drop this pair.
                pairs[a] = -1
            else:
                pairs[a] = b
    return {k: v for k, v in pairs.items() if v != -1}


def build_pair_remap(src: StreamToc, tgt: StreamToc) -> Tuple[Dict[int, int], Dict[int, int]]:
    """Return (file_id_remap_uint64, slot_id_remap_uint32) for one pair."""
    src_by = src.by_type()
    tgt_by = tgt.by_type()
    fid_remap: Dict[int, int] = {}
    for tid in src_by:
        for ea, eb in zip(src_by[tid], tgt_by.get(tid, [])):
            if ea.file_id != eb.file_id:
                fid_remap[ea.file_id] = eb.file_id

    slot_remap: Dict[int, int] = {}
    conflicts = defaultdict(set)
    for ea, eb in zip(src_by.get(UnitID, []), tgt_by.get(UnitID, [])):
        rewritten = refs.rewrite_unit(ea.toc_data, fid_remap)
        pair_pairs = discover_uint32_pairs(rewritten, eb.toc_data)
        for k, v in pair_pairs.items():
            conflicts[k].add(v)
    for k, vset in conflicts.items():
        if len(vset) == 1:
            slot_remap[k] = next(iter(vset))
    return fid_remap, slot_remap


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-name", required=True,
                   help="Subfolder name of the source variant (e.g. 'AF-52 Lockdown')")
    p.add_argument("--reference-dir", required=True,
                   help="Folder containing one subfolder per variant, "
                        "each holding a `<base>.patch_0` trio.")
    p.add_argument("--patch-filename", default="9ba626afa44a3aa3.patch_0",
                   help="Patch filename inside each variant subfolder.")
    p.add_argument("--out", required=True, help="Output remap.json path.")
    args = p.parse_args(argv)

    ref_root = args.reference_dir
    src_dir = os.path.join(ref_root, args.source_name)
    src_path = os.path.join(src_dir, args.patch_filename)
    if not os.path.exists(src_path):
        print(f"source patch not found: {src_path}", file=sys.stderr)
        return 2
    src = StreamToc.from_files(src_path)
    print(f"source: {args.source_name}  ({len(src.entries)} entries)")

    src_unit_count = sum(1 for e in src.entries if e.type_id == UnitID)
    targets: Dict[str, dict] = {}
    for name in sorted(os.listdir(ref_root)):
        sub = os.path.join(ref_root, name)
        if not os.path.isdir(sub):
            continue
        if name == args.source_name:
            continue
        tp = os.path.join(sub, args.patch_filename)
        if not os.path.exists(tp):
            continue
        tgt = StreamToc.from_files(tp)
        fid, slot = build_pair_remap(src, tgt)
        # Extra Unit FileIDs: anything in target's unit list past the
        # ordinal-matched range with source.
        tgt_unit_fids = [e.file_id for e in tgt.entries if e.type_id == UnitID]
        extras = tgt_unit_fids[src_unit_count:]
        targets[name] = {
            "file_ids": {str(k): str(v) for k, v in fid.items()},
            "slot_ids": {f"0x{k:08x}": f"0x{v:08x}" for k, v in slot.items()},
            "extra_unit_file_ids": [str(x) for x in extras],
        }
        extra_note = f" + {len(extras)} extras" if extras else ""
        print(f"  {name}: {len(fid)} FileIDs, {len(slot)} slot IDs{extra_note}")

    out = {
        "source_variant": args.source_name,
        "patch_filename": args.patch_filename,
        "targets": targets,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
