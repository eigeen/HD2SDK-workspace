"""Compare tool-generated variants against the manual reference variants.
Reports, per variant, what fraction of bytes / entries match."""
from __future__ import annotations

import os
import sys
from typing import List, Tuple

from .archive import StreamToc
from .constants import UnitID, MaterialID, TexID, TYPE_NAMES


def cmp_patch(ours: str, theirs: str):
    a = StreamToc.from_files(ours)
    b = StreamToc.from_files(theirs)
    a_by = a.by_type()
    b_by = b.by_type()

    total_entries = 0
    toc_match = 0
    gpu_match = 0
    stream_match = 0
    file_id_match = 0
    per_type_outcomes = {}

    for tid in a_by:
        per_type_outcomes[tid] = {"n": 0, "toc": 0, "gpu": 0, "stream": 0, "fid": 0}
        for ea, eb in zip(a_by.get(tid, []), b_by.get(tid, [])):
            total_entries += 1
            per_type_outcomes[tid]["n"] += 1
            if ea.file_id == eb.file_id:
                file_id_match += 1
                per_type_outcomes[tid]["fid"] += 1
            if ea.toc_data == eb.toc_data:
                toc_match += 1
                per_type_outcomes[tid]["toc"] += 1
            if ea.gpu_data == eb.gpu_data:
                gpu_match += 1
                per_type_outcomes[tid]["gpu"] += 1
            if ea.stream_data == eb.stream_data:
                stream_match += 1
                per_type_outcomes[tid]["stream"] += 1
    return total_entries, file_id_match, toc_match, gpu_match, stream_match, per_type_outcomes


def main(argv):
    our_root = argv[1]
    their_root = argv[2]
    patch_filename = "9ba626afa44a3aa3.patch_0"

    # Match folders.
    pairs: List[Tuple[str, str, str]] = []
    for d in sorted(os.listdir(our_root)):
        if not os.path.isdir(os.path.join(our_root, d)):
            continue
        # Tool output dirs are named "<hash>_<name>"; reference is "<name>".
        if "_" in d:
            name = d.split("_", 1)[1]
        else:
            name = d
        our_patch = os.path.join(our_root, d, patch_filename)
        their_patch = os.path.join(their_root, name, patch_filename)
        if os.path.exists(our_patch) and os.path.exists(their_patch):
            pairs.append((name, our_patch, their_patch))

    print(f"Comparing {len(pairs)} variant(s)\n")
    print(f"{'variant':40s}  {'entries':>7s}  {'FID':>4s}  {'TocData':>8s}  "
          f"{'GPU':>4s}  {'Stream':>6s}")
    agg = {"n": 0, "fid": 0, "toc": 0, "gpu": 0, "stream": 0}
    for name, ours, theirs in pairs:
        n, fid, toc, gpu, stream, _ = cmp_patch(ours, theirs)
        agg["n"] += n
        agg["fid"] += fid
        agg["toc"] += toc
        agg["gpu"] += gpu
        agg["stream"] += stream
        print(f"{name:40s}  {n:7d}  {fid:4d}  {toc:5d}/{n:<2d}  "
              f"{gpu:4d}  {stream:6d}")
    print()
    print(f"TOTALS: {agg['n']} entries  "
          f"FileID match: {agg['fid']}  TocData match: {agg['toc']}  "
          f"GPU match: {agg['gpu']}  Stream match: {agg['stream']}")


if __name__ == "__main__":
    main(sys.argv)
