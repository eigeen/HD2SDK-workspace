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
from .migrator import ArmorEntry, migrate_one
from .unit_geometry import GeometryMatchSettings, build_unit_geometry_remap
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


def _make_geometry_entry(file_id: int, points: list, marker: bytes = b"") -> TocEntry:
    toc_data, gpu_data = _make_geometry_unit_blob(points, marker)
    return TocEntry(file_id=file_id, type_id=UnitID, toc_data=toc_data, gpu_data=gpu_data)


def _make_geometry_unit_blob(points: list, marker: bytes = b"") -> tuple:
    stream_off = 0x80
    stream_base = 0x90
    mesh_off = 0x260
    mesh_base = 0x26C
    section_rel = 132
    toc_size = mesh_base + section_rel + 24 + len(marker)
    toc = bytearray(toc_size)
    struct.pack_into("<I", toc, 0x2C, 10800438)
    struct.pack_into("<I", toc, 0x5C, stream_off)
    struct.pack_into("<I", toc, 0x64, mesh_off)
    _write_stream_info(toc, stream_off, stream_base, len(points))
    _write_mesh_info(toc, mesh_off, mesh_base, section_rel, len(points))
    if marker:
        toc[-len(marker):] = marker
    gpu = b"".join(struct.pack("<3f", *point) for point in points)
    return bytes(toc), gpu


def _write_stream_info(toc: bytearray, stream_off: int, stream_base: int, vertex_count: int) -> None:
    struct.pack_into("<I", toc, stream_off, 1)
    struct.pack_into("<I", toc, stream_off + 4, stream_base - stream_off)
    struct.pack_into("<IIIQ", toc, stream_base + 8, 0, 2, 0, 0)
    struct.pack_into("<Q", toc, stream_base + 328, 1)
    struct.pack_into("<II", toc, stream_base + 352, vertex_count, 12)
    struct.pack_into("<II", toc, stream_base + 392, 0, 0)
    struct.pack_into("<IIII", toc, stream_base + 416, 0, vertex_count * 12, 0, 0)


def _write_mesh_info(
    toc: bytearray,
    mesh_off: int,
    mesh_base: int,
    section_rel: int,
    vertex_count: int,
) -> None:
    struct.pack_into("<I", toc, mesh_off, 1)
    struct.pack_into("<I", toc, mesh_off + 4, mesh_base - mesh_off)
    struct.pack_into("<i", toc, mesh_base + 56, 0)
    struct.pack_into("<I", toc, mesh_base + 104, 1)
    struct.pack_into("<I", toc, mesh_base + 108, 128)
    struct.pack_into("<II", toc, mesh_base + 120, 1, section_rel)
    section_at = mesh_base + section_rel
    struct.pack_into("<IIIIII", toc, section_at, 0, 0, vertex_count, 0, 0, 0)


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


def test_non_unit_count_mismatch_does_not_block():
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=MaterialID),
        TocEntry(file_id=0x02, type_id=MaterialID),
    ]
    target = StreamToc()
    target.entries = [
        TocEntry(file_id=0x11, type_id=MaterialID),
        TocEntry(file_id=0x12, type_id=MaterialID),
        TocEntry(file_id=0x13, type_id=MaterialID),
    ]
    patch = StreamToc()
    patch.entries = [
        TocEntry(file_id=0x01, type_id=MaterialID, toc_data=b"\x00" * 16),
    ]
    with tempfile.TemporaryDirectory() as out_dir:
        report = migrate_one(
            patch,
            source,
            target,
            ArmorEntry("target", "Target"),
            out_dir,
        )
    assert report.written_entries == 1, report
    print("  non-Unit count mismatch warning-only migration OK")


def test_unit_geometry_remap_ignores_names_and_order():
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=UnitID),
        TocEntry(file_id=0x02, type_id=UnitID),
    ]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x22, _line_points(0, 0, 0, 8, 0, 0)),
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1)),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), b"wrong_name_arm"),
        _make_geometry_entry(0x02, _line_points(0, 0, 0, 8, 0, 0), b"wrong_name_hip"),
    ]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x11, 0x02: 0x22}, result.remap
    print("  Unit geometry remap ignores names/order OK")


def test_unit_geometry_uses_patch_mod_geometry():
    source = StreamToc()
    source.entries = [_make_geometry_entry(0x01, _line_points(20, 0, 0, 30, 0, 0))]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _line_points(20, 0, 0, 30, 0, 0)),
        _make_geometry_entry(0x22, _box_points(0, 0, 0, 1, 1, 1)),
    ]
    patch = StreamToc()
    patch.entries = [_make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1))]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x22}, result.remap
    print("  Unit geometry uses patch mod geometry OK")


def test_unit_geometry_prefers_distribution_over_centroid():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _cluster_points(5, 0, 0)),
        _make_geometry_entry(0x22, _line_points(0, 0, 0, 10, 0, 0)),
    ]
    patch = StreamToc()
    patch.entries = [_make_geometry_entry(0x01, _line_points(0, 0, 0, 10, 0, 0))]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x22}, result.remap
    print("  Unit geometry remap uses distribution OK")


def test_unit_geometry_blocks_ambiguous_candidates():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1)),
        _make_geometry_entry(0x22, _box_points(0, 0, 0, 1, 1, 1)),
    ]
    patch = StreamToc()
    patch.entries = [_make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1))]

    result = build_unit_geometry_remap(
        patch,
        source,
        target,
        GeometryMatchSettings(min_margin=0.015),
    )
    assert not result.is_complete(), result
    assert result.ambiguous and result.ambiguous[0].source_file_id == 0x01
    print("  Unit geometry ambiguous match blocking OK")


def test_unit_geometry_reports_missing_and_extra_units():
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=UnitID),
        TocEntry(file_id=0x02, type_id=UnitID),
    ]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1)),
        _make_geometry_entry(0x33, _box_points(20, 0, 0, 1, 1, 1)),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1)),
        TocEntry(file_id=0x02, type_id=UnitID, toc_data=b"not-a-unit"),
    ]

    result = build_unit_geometry_remap(patch, source, target)
    assert not result.is_complete(), result
    assert result.remap == {0x01: 0x11}, result.remap
    assert {issue.source_file_id for issue in result.missing} == {0x02}
    assert result.extra_unit_file_ids == [0x33], result.extra_unit_file_ids
    print("  Unit geometry missing/extra diagnostics OK")


def test_unit_geometry_expands_any_to_body_variants():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Stocky")),
        _make_geometry_entry(0x22, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim")),
    ]
    patch = StreamToc()
    patch.entries = [_make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Any"))]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.expanded_remap == {0x01: (0x11, 0x22)}, result.expanded_remap
    assert result.extra_unit_file_ids == [], result.extra_unit_file_ids
    print("  Unit geometry Any variant expansion OK")


def test_unit_geometry_respects_specific_body_variant():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Stocky")),
        _make_geometry_entry(0x22, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim")),
    ]
    patch = StreamToc()
    patch.entries = [_make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Stocky"))]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.expanded_remap == {0x01: (0x11,)}, result.expanded_remap
    assert result.extra_unit_file_ids == [0x22], result.extra_unit_file_ids
    print("  Unit geometry specific body variant OK")


def test_unit_geometry_filters_structured_name_scope():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim", "Torso")),
        _make_geometry_entry(0x22, _line_points(0, 0, 0, 8, 0, 0), _custom_marker("Slim", "Hip")),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim", "Hip")),
    ]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x22}, result.remap
    print("  Unit geometry structured name scope OK")


def test_unit_geometry_trusts_unique_structured_target():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _line_points(20, 0, 0, 30, 0, 0), _custom_marker("Slim", "RightArm")),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim", "RightArm")),
    ]

    result = build_unit_geometry_remap(
        patch,
        source,
        target,
        GeometryMatchSettings(max_score=0.01),
    )
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x11}, result.remap
    print("  Unit geometry trusts unique structured target OK")


def test_unit_geometry_ignores_weight_and_piece_type_for_slot_scope():
    source = StreamToc()
    source.entries = [TocEntry(file_id=0x01, type_id=UnitID)]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(
            0x11,
            _line_points(20, 0, 0, 30, 0, 0),
            _custom_marker("Slim", "Torso", "Heavy", "Armor"),
        ),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(
            0x01,
            _box_points(0, 0, 0, 1, 1, 1),
            _custom_marker("Slim", "Torso", "Medium", "Undergarment"),
        ),
    ]

    result = build_unit_geometry_remap(
        patch,
        source,
        target,
        GeometryMatchSettings(max_score=0.01),
    )
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x11}, result.remap
    print("  Unit geometry ignores weight/piece for slot scope OK")


def test_unit_geometry_preassigns_identical_body_pair():
    source = StreamToc()
    source.entries = [
        TocEntry(file_id=0x01, type_id=UnitID),
        TocEntry(file_id=0x02, type_id=UnitID),
    ]
    target = StreamToc()
    target.entries = [
        _make_geometry_entry(0x11, _box_points(0, 0, 0, 1, 1, 1.02)),
        _make_geometry_entry(0x22, _box_points(0, 0, 0, 1, 1, 1)),
        _make_geometry_entry(0x33, _cluster_points(0.5, 0.5, 0.5)),
    ]
    patch = StreamToc()
    patch.entries = [
        _make_geometry_entry(0x01, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Stocky", "RightArm")),
        _make_geometry_entry(0x02, _box_points(0, 0, 0, 1, 1, 1), _custom_marker("Slim", "RightArm")),
    ]

    result = build_unit_geometry_remap(patch, source, target)
    assert result.is_complete(), result
    assert result.remap == {0x01: 0x11, 0x02: 0x22}, result.remap
    assert "body-pair" in result.match_levels[0x01], result.match_levels
    print("  Unit geometry identical body pair preassignment OK")


def _custom_marker(
    body_type: str,
    slot: str = "RightLeg",
    weight: str = "Medium",
    piece_type: str = "Undergarment",
) -> bytes:
    return (
        f"HelldiverCustomizationBodyType_{body_type}\x00"
        f"HelldiverCustomizationSlot_{slot}\x00"
        f"HelldiverCustomizationWeight_{weight}\x00"
        f"HelldiverCustomizationPieceType_{piece_type}\x00"
    ).encode("utf-8")


def _box_points(x: float, y: float, z: float, sx: float, sy: float, sz: float) -> list:
    return [
        (x, y, z),
        (x + sx, y, z),
        (x, y + sy, z),
        (x, y, z + sz),
        (x + sx, y + sy, z),
        (x + sx, y, z + sz),
        (x, y + sy, z + sz),
        (x + sx, y + sy, z + sz),
    ]


def _line_points(x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> list:
    return [
        (
            x1 + (x2 - x1) * index / 10,
            y1 + (y2 - y1) * index / 10,
            z1 + (z2 - z1) * index / 10,
        )
        for index in range(11)
    ]


def _cluster_points(x: float, y: float, z: float) -> list:
    return [
        (x - 0.1, y, z),
        (x + 0.1, y, z),
        (x, y - 0.1, z),
        (x, y + 0.1, z),
        (x, y, z - 0.1),
        (x, y, z + 0.1),
    ]


if __name__ == "__main__":
    print("self-test:")
    test_refs_roundtrip()
    test_streamtoc_roundtrip()
    test_non_unit_count_mismatch_does_not_block()
    test_unit_geometry_remap_ignores_names_and_order()
    test_unit_geometry_uses_patch_mod_geometry()
    test_unit_geometry_prefers_distribution_over_centroid()
    test_unit_geometry_blocks_ambiguous_candidates()
    test_unit_geometry_reports_missing_and_extra_units()
    test_unit_geometry_expands_any_to_body_variants()
    test_unit_geometry_respects_specific_body_variant()
    test_unit_geometry_filters_structured_name_scope()
    test_unit_geometry_trusts_unique_structured_target()
    test_unit_geometry_ignores_weight_and_piece_type_for_slot_scope()
    test_unit_geometry_preassigns_identical_body_pair()
    print("ALL PASS")
