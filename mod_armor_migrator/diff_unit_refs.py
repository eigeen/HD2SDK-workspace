"""For each Unit entry pair, print the 5 header uint64 refs side-by-side
and highlight which ones differ. Helps identify what's not in our patch-only
remap table."""
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
    for i, (ea, eb) in enumerate(zip(a_units, b_units)):
        refs_a = struct.unpack_from("<QQQQQ", ea.toc_data, 0)
        refs_b = struct.unpack_from("<QQQQQ", eb.toc_data, 0)
        names = ["UnkRef1", "BonesRef", "CompositeRef", "UnkRef2", "StateMachineRef"]
        print(f"Unit #{i} (A FileID={ea.file_id:016x}, B FileID={eb.file_id:016x})")
        for n, ra, rb in zip(names, refs_a, refs_b):
            mark = "  " if ra == rb else "≠ "
            print(f"  {mark}{n:18s}  A=0x{ra:016x}  B=0x{rb:016x}")
        if i >= 3:
            break


if __name__ == "__main__":
    main(sys.argv)
