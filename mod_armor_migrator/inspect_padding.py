"""Reconnaissance for the padding feature.

Prints, for each variant:
  - Unit entry count
  - smallest Unit toc_data size (a candidate empty mesh)
  - whether any variant has MORE units than the source (=> would need padding)

Usage:
  python -m mod_armor_migrator.inspect_padding <reference_dir> [<source_name>]
"""
from __future__ import annotations

import os
import sys

from .archive import StreamToc
from .constants import UnitID
from .padding import find_empty_unit_candidates


def main(argv):
    ref_dir = argv[1]
    source_name = argv[2] if len(argv) > 2 else None
    patch_filename = "9ba626afa44a3aa3.patch_0"

    print(f"{'variant':40s}  {'#units':>6s}  {'smallest':>9s}  {'comment'}")
    src_count = None
    for name in sorted(os.listdir(ref_dir)):
        sub = os.path.join(ref_dir, name)
        if not os.path.isdir(sub):
            continue
        p_path = os.path.join(sub, patch_filename)
        if not os.path.exists(p_path):
            continue
        p = StreamToc.from_files(p_path)
        units = [e for e in p.entries if e.type_id == UnitID]
        smallest = min((len(u.toc_data) for u in units), default=0)
        if name == source_name:
            src_count = len(units)
            print(f"{name:40s}  {len(units):6d}  {smallest:9d}  [SOURCE]")
        else:
            tag = ""
            if src_count is not None and len(units) > src_count:
                tag = f"  ← has {len(units) - src_count} extra slot(s), would need padding"
            elif src_count is not None and len(units) < src_count:
                tag = f"  (source has {src_count - len(units)} more)"
            print(f"{name:40s}  {len(units):6d}  {smallest:9d}{tag}")


if __name__ == "__main__":
    main(sys.argv)
