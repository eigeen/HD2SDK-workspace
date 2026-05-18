"""
Quick sanity tests that don't need real game data.

Run: python -m mod_armor_migrator.selftest
"""
from __future__ import annotations

import io
import os
import struct
import tempfile

from .archive import StreamToc, TocEntry
from .constants import UnitID, MaterialID, BoneID, TexID
from .migrator import RemapPlan, _find_unsafe_partial_types
from .unit_semantics import UnitSemanticKey, build_unit_semantic_remap
from . import refs


def _make_unit_blob(bones_ref: int, state_machine_ref: int, materials_offset: int,
                   material_ids: list) -> bytes:
    # Build a minimal Unit blob: 0x80 byte header + materials slot.
    buf = bytearray(materials_offset + 4 + 4 * len(material_ids) + 8 * len(material_ids))
    struct.pack_into("<Q", buf, 0x00, 0)                # UnkRef1
    struct.pack_into("<Q", buf, 0x08, bones_ref)
    struct.pack_into("<Q", buf, 0x10, 0)                # CompositeRef
    struct.pack_into("<Q", buf, 0x18, 0)                # UnkRef2
    struct.pack_into("<Q", buf, 0x20, state_machine_ref)
    struct.pack_into("<I", buf, 0x70, materials_offset)
    struct.pack_into("<I", buf, materials_offset, len(material_ids))
    for i, _ in enumerate(material_ids):
        struct.pack_into("<I", buf, materials_offset + 4 + 4 * i, 0xDEAD0000 + i)
    for i, mid in enumerate(material_ids):
        struct.pack_into("<Q", buf, materials_offset + 4 + 4 * len(material_ids) + 8 * i, mid)
    return bytes(buf)


def _make_material_blob(num_textures: int, tex_ids: list) -> bytes:
    size = 136 + 4 * num_textures + 8 * num_textures
    buf = bytearray(size)
    struct.pack_into("<I", buf, 0x40, num_textures)
    for i in range(num_textures):
        struct.pack_into("<I", buf, 136 + 4 * i, 0xCAFE0000 + i)
    for i, tid in enumerate(tex_ids):
        struct.pack_into("<Q", buf, 136 + 4 * num_textures + 8 * i, tid)
    return bytes(buf)


def _make_semantic_unit_blob(key: UnitSemanticKey) -> bytes:
    parts = (
        f"HelldiverCustomizationBodyType_{key.body_type}",
        f"HelldiverCustomizationSlot_{key.slot}",
        f"HelldiverCustomizationWeight_{key.weight}",
        f"HelldiverCustomizationPieceType_{key.piece_type}",
    )
    return b"\x00".join(part.encode("utf-8") for part in parts)


def test_refs_roundtrip():
    unit = _make_unit_blob(
        bones_ref=0x1111_1111_1111_1111,
        state_machine_ref=0x2222_2222_2222_2222,
        materials_offset=0x80,
        material_ids=[0x3333_0000_0000_0001, 0x3333_0000_0000_0002],
    )
    remap = {
        0x1111_1111_1111_1111: 0x9999_AAAA_BBBB_0001,
        0x2222_2222_2222_2222: 0x9999_AAAA_BBBB_0002,
        0x3333_0000_0000_0001: 0x9999_AAAA_BBBB_0003,
        0x3333_0000_0000_0002: 0x9999_AAAA_BBBB_0004,
    }
    new = refs.rewrite_unit(unit, remap)
    new_refs = refs.list_unit_refs(new)
    assert new_refs[1] == 0x9999_AAAA_BBBB_0001, new_refs
    assert new_refs[4] == 0x9999_AAAA_BBBB_0002, new_refs
    assert new_refs[5:7] == [0x9999_AAAA_BBBB_0003, 0x9999_AAAA_BBBB_0004], new_refs
    print("  refs.rewrite_unit OK")

    mat = _make_material_blob(2, [0xABCD_0000_0000_0001, 0xABCD_0000_0000_0002])
    mat_remap = {
        0xABCD_0000_0000_0001: 0x9999_AAAA_DEAD_0001,
        0xABCD_0000_0000_0002: 0x9999_AAAA_DEAD_0002,
    }
    new_mat = refs.rewrite_material(mat, mat_remap)
    new_tex = refs.list_material_refs(new_mat)
    assert new_tex == [0x9999_AAAA_DEAD_0001, 0x9999_AAAA_DEAD_0002], new_tex
    print("  refs.rewrite_material OK")


def test_streamtoc_roundtrip():
    toc = StreamToc()
    toc.entries = [
        TocEntry(file_id=0xAA, type_id=UnitID,
                 toc_data=_make_unit_blob(0xB1, 0xC1, 0x80, [0xD1, 0xD2]),
                 gpu_data=b"unit-gpu",
                 stream_data=b""),
        TocEntry(file_id=0xBB, type_id=MaterialID,
                 toc_data=_make_material_blob(1, [0xE1]),
                 gpu_data=b"",
                 stream_data=b""),
        TocEntry(file_id=0xCC, type_id=TexID,
                 toc_data=b"\x00" * 14 + b"DDStex",
                 gpu_data=b"raw-tex-pixels",
                 stream_data=b"streamed-mip-data"),
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "self_archive")
        toc.write(path)
        roundtrip = StreamToc.from_files(path)
    rt_by_type = roundtrip.by_type()
    assert UnitID in rt_by_type and MaterialID in rt_by_type and TexID in rt_by_type
    rt_unit = rt_by_type[UnitID][0]
    assert rt_unit.file_id == 0xAA
    assert rt_unit.gpu_data == b"unit-gpu"
    rt_tex = rt_by_type[TexID][0]
    assert rt_tex.stream_data == b"streamed-mip-data"
    print("  StreamToc round-trip OK")


def test_unsafe_non_unit_partial_remap_detection():
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=MaterialID),
        TocEntry(file_id=0x02, type_id=MaterialID),
    ]
    patch = StreamToc()
    patch.entries = [TocEntry(file_id=0x01, type_id=MaterialID)]

    plan = RemapPlan({}, [], {MaterialID: (2, 3)}, set(), [])
    unsafe = _find_unsafe_partial_types(patch, source, plan)
    assert unsafe == [MaterialID], unsafe
    print("  unsafe non-Unit partial remap detection OK")


def test_unit_semantic_remap_ignores_order():
    hip = UnitSemanticKey("Slim", "Hip", "Medium", "Undergarment")
    arm = UnitSemanticKey("Stocky", "RightArm", "Medium", "Undergarment")
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=UnitID, toc_data=_make_semantic_unit_blob(hip)),
        TocEntry(file_id=0x02, type_id=UnitID, toc_data=_make_semantic_unit_blob(arm)),
    ]
    target = StreamToc()
    target.entries = [
        TocEntry(file_id=0x22, type_id=UnitID, toc_data=_make_semantic_unit_blob(arm)),
        TocEntry(file_id=0x11, type_id=UnitID, toc_data=_make_semantic_unit_blob(hip)),
    ]
    patch = StreamToc()
    patch.entries = [
        TocEntry(file_id=0x01, type_id=UnitID, toc_data=b""),
        TocEntry(file_id=0x02, type_id=UnitID, toc_data=b""),
    ]

    result = build_unit_semantic_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x11, 0x02: 0x22}, result.remap
    print("  Unit semantic remap ignores order OK")


if __name__ == "__main__":
    print("self-test:")
    test_refs_roundtrip()
    test_streamtoc_roundtrip()
    test_unsafe_non_unit_partial_remap_detection()
    test_unit_semantic_remap_ignores_order()
    print("ALL PASS")
