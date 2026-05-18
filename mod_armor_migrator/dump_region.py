"""Hex-dump TocData around the MaterialsOffset of the first Unit in two patches
so we can read the actual layout."""
from __future__ import annotations

import struct
import sys

from .archive import StreamToc
from .constants import UnitID


def hexdump(buf, start, length, label=""):
    print(f"  {label} offset=0x{start:04x} len={length}")
    end = start + length
    for line in range(start, end, 16):
        chunk = buf[line:line + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asciip = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"    0x{line:04x}  {hexpart:<48}  {asciip}")


def main(argv):
    a = StreamToc.from_files(argv[1])
    b = StreamToc.from_files(argv[2])
    a_units = [e for e in a.entries if e.type_id == UnitID]
    b_units = [e for e in b.entries if e.type_id == UnitID]
    ea, eb = a_units[0], b_units[0]

    materials_offset_a, = struct.unpack_from("<I", ea.toc_data, 0x70)
    print(f"Unit#0 MaterialsOffset (A): 0x{materials_offset_a:x}")
    print("--- A ---")
    hexdump(ea.toc_data, materials_offset_a, 80, "materials slot A")
    print("--- B ---")
    hexdump(eb.toc_data, materials_offset_a, 80, "materials slot B")

    # Also dump the MeshInfo regions where diffs appeared (0x75b0..)
    print("\n--- A around 0x75a0 ---")
    hexdump(ea.toc_data, 0x75a0, 32, "")
    print("--- B around 0x75a0 ---")
    hexdump(eb.toc_data, 0x75a0, 32, "")


if __name__ == "__main__":
    main(sys.argv)
