"""
Compare two mod patches that supposedly contain the same mod retargeted to
different armor archives. Verifies:

  - same TypeID counts
  - per-(TypeID, ordinal) entries: GPU & Stream blobs match byte-for-byte
  - per-(TypeID, ordinal) entries: TocData differs ONLY at known FileID
    reference fields (after applying an inferred ordinal-based remap)

This is the ground-truth check for the migrator's assumption that
"manual Blender variants" == "FileID-and-ref remap of source".

Usage:
    python -m mod_armor_migrator.diff_patches <patch_a> <patch_b>
"""
from __future__ import annotations

import struct
import sys
from typing import Dict, List, Tuple

from .archive import StreamToc
from .constants import TYPE_NAMES, UnitID, MaterialID
from . import refs


def build_ordinal_remap(a: StreamToc, b: StreamToc) -> Dict[int, int]:
    """Map a.entries[i].file_id -> b.entries[i].file_id grouped by type, in order."""
    a_by = a.by_type()
    b_by = b.by_type()
    remap: Dict[int, int] = {}
    for tid, entries in a_by.items():
        partner = b_by.get(tid, [])
        if len(partner) != len(entries):
            continue
        for ea, eb in zip(entries, partner):
            if ea.file_id != eb.file_id:
                remap[ea.file_id] = eb.file_id
    return remap


def diff_bytes_summary(name: str, a: bytes, b: bytes, limit: int = 8) -> str:
    if a == b:
        return f"    {name}: IDENTICAL ({len(a)} bytes)"
    if len(a) != len(b):
        return f"    {name}: SIZE MISMATCH ({len(a)} vs {len(b)})"
    diffs = []
    for i in range(len(a)):
        if a[i] != b[i]:
            diffs.append(i)
            if len(diffs) >= limit:
                break
    return f"    {name}: differs at {len(diffs)}+ offsets, first: {diffs}"


def find_diff_offsets(a: bytes, b: bytes) -> List[int]:
    return [i for i in range(min(len(a), len(b))) if a[i] != b[i]]


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    a = StreamToc.from_files(argv[1])
    b = StreamToc.from_files(argv[2])
    print(f"A: {argv[1]}  ({len(a.entries)} entries)")
    print(f"B: {argv[2]}  ({len(b.entries)} entries)")

    a_by = a.by_type()
    b_by = b.by_type()
    if sorted(a_by.keys()) != sorted(b_by.keys()):
        print("TypeID set differs:", set(a_by) ^ set(b_by))
        return 2
    for tid in sorted(a_by.keys()):
        if len(a_by[tid]) != len(b_by[tid]):
            print(f"count mismatch for {TYPE_NAMES.get(tid, hex(tid))}: "
                  f"{len(a_by[tid])} vs {len(b_by[tid])}")
            return 2

    remap = build_ordinal_remap(a, b)
    print(f"\nInferred ordinal remap: {len(remap)} FileID pairs differ")

    total = 0
    bad_gpu = 0
    bad_stream = 0
    bad_toc = 0
    bad_toc_post_remap = 0
    bad_examples: List[Tuple[int, int, int]] = []  # (tid, ordinal, extra_diff_count)

    for tid in sorted(a_by.keys()):
        ea_list = a_by[tid]
        eb_list = b_by[tid]
        for i, (ea, eb) in enumerate(zip(ea_list, eb_list)):
            total += 1
            if ea.gpu_data != eb.gpu_data:
                bad_gpu += 1
            if ea.stream_data != eb.stream_data:
                bad_stream += 1
            if ea.toc_data != eb.toc_data:
                bad_toc += 1
                # apply remap to A's toc_data, see if it matches B's
                rewritten = refs.rewrite(tid, ea.toc_data, remap)
                if rewritten != eb.toc_data:
                    bad_toc_post_remap += 1
                    extra = find_diff_offsets(rewritten, eb.toc_data)
                    if len(bad_examples) < 5:
                        bad_examples.append((tid, i, len(extra)))

    print(f"\nEntries compared: {total}")
    print(f"  GPU data mismatched:     {bad_gpu}")
    print(f"  Stream data mismatched:  {bad_stream}")
    print(f"  TocData differs:         {bad_toc}")
    print(f"  TocData STILL differs after applying ordinal remap + ref rewrite: "
          f"{bad_toc_post_remap}")

    if bad_examples:
        print("\n  examples of post-remap residual diffs (TypeID, ordinal, "
              "remaining diff bytes):")
        for tid, ord_, n in bad_examples:
            print(f"    {TYPE_NAMES.get(tid, hex(tid))} #{ord_} -> {n} bytes differ")

    if bad_gpu == 0 and bad_stream == 0 and bad_toc_post_remap == 0:
        print("\nVERDICT: ordinal-based FileID remap fully reproduces variant B from A.")
        print("         The migrator's assumption is validated for these patches.")
        return 0

    print("\nVERDICT: residual differences exist; see examples above.")
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
