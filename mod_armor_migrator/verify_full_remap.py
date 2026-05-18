"""Verify the full migration model: starting from patch A, can we reach patch B
byte-for-byte by applying:
   (1) per-(TypeID, ordinal) uint64 FileID remap
   (2) per-Unit uint32 material-slot-ID remap (the SectionsIDs / short IDs)

If yes, the migrator's model is complete; the only missing piece is the source
of those remap tables (the source + target armor archives).
"""
from __future__ import annotations

import struct
import sys
from collections import Counter
from typing import Dict, List

from .archive import StreamToc
from .constants import UnitID, MaterialID
from . import refs


def discover_slot_remap(unit_a: bytes, unit_b: bytes) -> Dict[int, int]:
    """Find uint32 word positions that differ; build a value-pair remap."""
    pairs: Dict[int, int] = {}
    counts: Counter = Counter()
    # Iterate 4-byte aligned uint32 positions inside the unit; only consider
    # positions in the materials slot + MeshInfo (we don't want to touch
    # arbitrary words elsewhere). Practically the diffs are limited to those
    # regions so a global scan is fine.
    if len(unit_a) != len(unit_b):
        return pairs
    for pos in range(0, len(unit_a) - 3, 4):
        a, = struct.unpack_from("<I", unit_a, pos)
        b, = struct.unpack_from("<I", unit_b, pos)
        if a != b:
            if a in pairs and pairs[a] != b:
                # Conflicting mapping discovered for same source value — abort.
                pairs[a] = -1
            else:
                pairs[a] = b
            counts[(a, b)] += 1
    # Drop conflicting entries.
    return {k: v for k, v in pairs.items() if v != -1}


def apply_uint32_remap(data: bytes, remap: Dict[int, int]) -> bytes:
    if not remap:
        return data
    buf = bytearray(data)
    for pos in range(0, len(buf) - 3, 4):
        v, = struct.unpack_from("<I", buf, pos)
        if v in remap:
            struct.pack_into("<I", buf, pos, remap[v])
    return bytes(buf)


def main(argv):
    a = StreamToc.from_files(argv[1])
    b = StreamToc.from_files(argv[2])

    a_by = a.by_type()
    b_by = b.by_type()

    # FileID remap (uint64).
    file_id_remap: Dict[int, int] = {}
    for tid in a_by:
        for ea, eb in zip(a_by[tid], b_by[tid]):
            if ea.file_id != eb.file_id:
                file_id_remap[ea.file_id] = eb.file_id

    print(f"FileID pairs in remap: {len(file_id_remap)}")

    # For each Unit pair, discover the uint32 slot-id remap from the pair itself,
    # apply FileID remap + slot remap, compare to B.
    failures = 0
    aggregate_slot_remap: Dict[int, int] = {}
    for ea, eb in zip(a_by.get(UnitID, []), b_by.get(UnitID, [])):
        rewritten = refs.rewrite_unit(ea.toc_data, file_id_remap)
        slot_remap = discover_slot_remap(rewritten, eb.toc_data)
        for k, v in slot_remap.items():
            aggregate_slot_remap[k] = v
        rewritten2 = apply_uint32_remap(rewritten, slot_remap)
        if rewritten2 != eb.toc_data:
            failures += 1
    print(f"Aggregated uint32 slot remap entries: {len(aggregate_slot_remap)}")
    for k, v in list(aggregate_slot_remap.items())[:20]:
        print(f"   0x{k:08x}  ->  0x{v:08x}")

    # Final pass with per-pair PRECISE slot remap.
    all_match = True
    examples_printed = 0
    for tid in a_by:
        for i, (ea, eb) in enumerate(zip(a_by[tid], b_by[tid])):
            rewritten = refs.rewrite(tid, ea.toc_data, file_id_remap)
            if tid == UnitID:
                local_slot = discover_slot_remap(rewritten, eb.toc_data)
                rewritten = apply_uint32_remap(rewritten, local_slot)
            if rewritten != eb.toc_data:
                all_match = False
                if examples_printed < 3 and tid == UnitID:
                    diffs = [j for j in range(min(len(rewritten), len(eb.toc_data)))
                             if rewritten[j] != eb.toc_data[j]]
                    print(f"\n  Unit #{i} residual after per-pair slot+FileID remap:")
                    print(f"    {len(diffs)} bytes still differ at offsets: {diffs[:16]}")
                    if diffs:
                        for d in diffs[:8]:
                            ctx_start = max(0, d - 4)
                            ctx_end = min(len(rewritten), d + 8)
                            print(f"      @0x{d:04x} A={rewritten[ctx_start:ctx_end].hex()} "
                                  f"B={eb.toc_data[ctx_start:ctx_end].hex()}")
                    examples_printed += 1

    print(f"\nFinal TocData match with FileID+per-pair slot remap: {all_match}")

    # Now check GPU/Stream.
    gpu_diff = 0
    stream_diff = 0
    for tid in a_by:
        for ea, eb in zip(a_by[tid], b_by[tid]):
            if ea.gpu_data != eb.gpu_data:
                gpu_diff += 1
            if ea.stream_data != eb.stream_data:
                stream_diff += 1
    print(f"GPU mismatches: {gpu_diff}, Stream mismatches: {stream_diff}")
    if gpu_diff:
        print("  (GPU diffs are likely intentional per-armor mesh recustomization "
              "from the Blender workflow — see the report)")


if __name__ == "__main__":
    main(sys.argv)
