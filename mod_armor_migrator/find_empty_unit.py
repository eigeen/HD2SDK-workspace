"""Identify the empty-mesh unit in a target reference by diffing against
the source variant. The extra unit at the end is presumed to be the empty
mesh template.

Usage: python -m mod_armor_migrator.find_empty_unit <source_patch> <target_patch>
"""
from __future__ import annotations

import sys

from .archive import StreamToc
from .constants import UnitID


def main(argv):
    src = StreamToc.from_files(argv[1])
    tgt = StreamToc.from_files(argv[2])
    src_units = [e for e in src.entries if e.type_id == UnitID]
    tgt_units = [e for e in tgt.entries if e.type_id == UnitID]
    print(f"source units: {len(src_units)}, target units: {len(tgt_units)}")
    if len(tgt_units) <= len(src_units):
        print("target has no extra units")
        return

    extras = tgt_units[len(src_units):]
    print(f"\nExtra unit(s) in target ({len(extras)} unit(s)):")
    for i, u in enumerate(extras):
        print(f"  extra #{i}: FileID=0x{u.file_id:016x}")
        print(f"    toc_data size:    {len(u.toc_data)} bytes")
        print(f"    gpu_data size:    {len(u.gpu_data)} bytes")
        print(f"    stream_data size: {len(u.stream_data)} bytes")

    # Compare extras' sizes to typical units (= the matched ones).
    typical_gpu = [len(u.gpu_data) for u in tgt_units[:len(src_units)]]
    typical_toc = [len(u.toc_data) for u in tgt_units[:len(src_units)]]
    if typical_gpu:
        print(f"\n  typical (non-extra) GPU sizes: "
              f"min={min(typical_gpu)} max={max(typical_gpu)} "
              f"median={sorted(typical_gpu)[len(typical_gpu)//2]}")
        print(f"  typical (non-extra) TOC sizes: "
              f"min={min(typical_toc)} max={max(typical_toc)} "
              f"median={sorted(typical_toc)[len(typical_toc)//2]}")


if __name__ == "__main__":
    main(sys.argv)
