"""For the first Unit pair, print exactly which byte offsets differ, the values
on each side, and the surrounding context — to discover where remapping needs
to happen."""
from __future__ import annotations

import struct
import sys

from .archive import StreamToc
from .constants import UnitID


def main(argv):
    a = StreamToc.from_files(argv[1])
    b = StreamToc.from_files(argv[2])
    a_units = [e for e in a.entries if e.type_id == UnitID]
    b_units = [e for e in b.entries if e.type_id == UnitID]

    for i in range(min(3, len(a_units))):
        ea, eb = a_units[i], b_units[i]
        print(f"=== Unit pair #{i} (sizes {len(ea.toc_data)} vs {len(eb.toc_data)}) ===")
        diffs = [j for j in range(min(len(ea.toc_data), len(eb.toc_data)))
                 if ea.toc_data[j] != eb.toc_data[j]]
        print(f"  {len(diffs)} bytes differ")
        # Cluster contiguous diffs into runs.
        runs = []
        if diffs:
            start = diffs[0]
            prev = start
            for d in diffs[1:]:
                if d == prev + 1:
                    prev = d
                else:
                    runs.append((start, prev + 1))
                    start = d
                    prev = d
            runs.append((start, prev + 1))
        print(f"  {len(runs)} run(s):")
        for r_start, r_end in runs:
            length = r_end - r_start
            chunk_a = ea.toc_data[r_start:r_end]
            chunk_b = eb.toc_data[r_start:r_end]
            print(f"    [0x{r_start:04x} .. 0x{r_end:04x}] ({length} bytes)")
            # Try to interpret as uint64 if 8-byte aligned & length multiple of 8.
            if r_start % 8 == 0 and length % 8 == 0:
                for k in range(length // 8):
                    pa, = struct.unpack_from("<Q", chunk_a, k * 8)
                    pb, = struct.unpack_from("<Q", chunk_b, k * 8)
                    print(f"        +{k*8:02d}  A=0x{pa:016x}  B=0x{pb:016x}")
            else:
                print(f"        A: {chunk_a.hex()}")
                print(f"        B: {chunk_b.hex()}")
        # Show MaterialsOffset to see where the materials slot lives.
        if len(ea.toc_data) > 0x74:
            mo_a, = struct.unpack_from("<I", ea.toc_data, 0x70)
            mo_b, = struct.unpack_from("<I", eb.toc_data, 0x70)
            print(f"  MaterialsOffset: A=0x{mo_a:x}  B=0x{mo_b:x}")


if __name__ == "__main__":
    main(sys.argv)
