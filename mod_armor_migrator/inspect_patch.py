"""Inspect a patch: list TypeIDs, FileIDs, and which armor archive they belong to.

Usage: python -m mod_armor_migrator.inspect_patch <patch_path> [<data_dir>]
"""
from __future__ import annotations

import json
import os
import sys

from .archive import StreamToc, list_file_ids
from .constants import TYPE_NAMES


def main(argv):
    patch_path = argv[1]
    data_dir = argv[2] if len(argv) > 2 else None

    here = os.path.dirname(os.path.abspath(__file__))
    index = json.load(open(os.path.join(here, "archivehashes.json"), encoding="utf-8"))

    p = StreamToc.from_files(patch_path)
    by_type = p.by_type()
    print(f"== {patch_path}")
    print(f"   entries: {len(p.entries)}, types: {len(by_type)}")
    for tid, entries in sorted(by_type.items()):
        print(f"   - {TYPE_NAMES.get(tid, hex(tid)):14s}  count={len(entries)}")

    if not data_dir:
        return

    # Match patch FileIDs to armor archives.
    patch_pairs = {(e.type_id, e.file_id) for e in p.entries}
    print("\nMatch against Armor archives in:", data_dir)
    matches = []
    for category in ("Armor", "Helmet", "Cape"):
        for h, name in index.get(category, {}).items():
            path = os.path.join(data_dir, h)
            if not os.path.exists(path):
                continue
            try:
                ids = list_file_ids(path)
            except Exception:
                continue
            arch_pairs = {(t, f) for t, fs in ids.items() for f in fs}
            hits = len(patch_pairs & arch_pairs)
            if hits:
                matches.append((hits, category, h, name))
    matches.sort(reverse=True)
    for hits, cat, h, name in matches[:10]:
        print(f"   {hits:4d}  [{cat:6s}] {h} {name}")


if __name__ == "__main__":
    main(sys.argv)
