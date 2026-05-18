"""
Reference rewriting inside TocEntry payloads.

We only touch fields where Stingray stores cross-resource references.
Geometry / texture pixel data is copied byte-for-byte from the source mod.

References we know how to rewrite:

    Unit (TypeID UnitID)
        0x00 uint64 UnkRef1               (semantics unknown — best-effort remap)
        0x08 uint64 BonesRef              -> Bone resource
        0x10 uint64 CompositeRef          -> CompositeUnit
        0x18 uint64 UnkRef2               (semantics unknown — best-effort remap)
        0x20 uint64 StateMachineRef
        @MaterialsOffset (uint32 at 0x70):
            uint32 NumMaterials
            uint32[NumMaterials] SectionsIDs   (material-slot short IDs —
                                                 these ARE remapped via the
                                                 separate slot_remap arg)
            uint64[NumMaterials] MaterialIDs   (uint64 FileIDs — remapped)

        SectionsIDs (uint32) also appear inside MeshInfo and MeshSectionInfo
        records throughout the Unit body — they reference the same material
        slots and must be remapped consistently. We do this by scanning every
        4-byte aligned word in the TocData and replacing values found in the
        slot_remap table. This is safe because SectionsIDs are murmur32 hashes
        of slot names: collisions with offsets / sizes / indices are
        astronomically unlikely.

    Material (TypeID MaterialID)
        TexIDs at offset 136 + 4*NumTextures (uint64 each)

Anything else (raw GPU buffers, animation streams, vertex data) is left alone.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional

from .constants import UnitID, MaterialID


def _remap(value: int, table: Dict[int, int]) -> int:
    return table.get(value, value)


def rewrite_unit(
    toc_data: bytes,
    remap: Dict[int, int],
    slot_remap: Optional[Dict[int, int]] = None,
) -> bytes:
    """Return a new toc_data buffer with FileID + slot-ID references remapped.

    Parameters
    ----------
    toc_data
        The Unit's TocData blob.
    remap
        uint64 -> uint64 FileID remap (BonesRef, MaterialIDs, etc).
    slot_remap
        Optional uint32 -> uint32 material-slot-ID remap (SectionsIDs).
        Applied as a whole-blob 4-byte scan: every aligned uint32 word whose
        value matches a key in this map is rewritten to the mapped value.
    """
    if len(toc_data) < 0x80:
        return toc_data
    buf = bytearray(toc_data)

    # Header references (5 x uint64 at offsets 0, 8, 16, 24, 32).
    for off in (0x00, 0x08, 0x10, 0x18, 0x20):
        (old,) = struct.unpack_from("<Q", buf, off)
        new = _remap(old, remap)
        if new != old:
            struct.pack_into("<Q", buf, off, new)

    # Materials slot at MaterialsOffset (uint32 at 0x70).
    (materials_offset,) = struct.unpack_from("<I", buf, 0x70)
    if materials_offset and materials_offset + 4 <= len(buf):
        (num_materials,) = struct.unpack_from("<I", buf, materials_offset)
        # Layout: uint32 NumMaterials, uint32[NumMaterials] SectionsIDs,
        #         uint64[NumMaterials] MaterialIDs
        mat_id_start = materials_offset + 4 + 4 * num_materials
        end = mat_id_start + 8 * num_materials
        if 0 <= mat_id_start and end <= len(buf):
            for i in range(num_materials):
                pos = mat_id_start + 8 * i
                (old,) = struct.unpack_from("<Q", buf, pos)
                new = _remap(old, remap)
                if new != old:
                    struct.pack_into("<Q", buf, pos, new)

    # Whole-blob uint32 slot ID remap (SectionsIDs appear in many places).
    if slot_remap:
        for pos in range(0, len(buf) - 3, 4):
            (val,) = struct.unpack_from("<I", buf, pos)
            new = slot_remap.get(val)
            if new is not None and new != val:
                struct.pack_into("<I", buf, pos, new)

    return bytes(buf)


def rewrite_material(toc_data: bytes, remap: Dict[int, int]) -> bytes:
    """Remap TexIDs[] inside a Material TocData blob."""
    if len(toc_data) < 0x88:
        return toc_data
    buf = bytearray(toc_data)

    # NumTextures lives at offset 12+4+8+8+32 = 64 (0x40).
    (num_textures,) = struct.unpack_from("<I", buf, 0x40)
    tex_ids_start = 136 + 4 * num_textures   # past TexUnks[]
    end = tex_ids_start + 8 * num_textures
    if num_textures == 0 or end > len(buf):
        return bytes(buf)

    for i in range(num_textures):
        pos = tex_ids_start + 8 * i
        (old,) = struct.unpack_from("<Q", buf, pos)
        new = _remap(old, remap)
        if new != old:
            struct.pack_into("<Q", buf, pos, new)
    return bytes(buf)


def list_unit_refs(toc_data: bytes) -> List[int]:
    """Return all FileIDs a Unit blob points at (for diagnostics)."""
    if len(toc_data) < 0x80:
        return []
    out = list(struct.unpack_from("<QQQQQ", toc_data, 0))
    (materials_offset,) = struct.unpack_from("<I", toc_data, 0x70)
    if materials_offset and materials_offset + 4 <= len(toc_data):
        (num_materials,) = struct.unpack_from("<I", toc_data, materials_offset)
        start = materials_offset + 4 + 4 * num_materials
        end = start + 8 * num_materials
        if end <= len(toc_data):
            out.extend(struct.unpack_from(f"<{num_materials}Q", toc_data, start))
    return out


def list_material_refs(toc_data: bytes) -> List[int]:
    if len(toc_data) < 0x88:
        return []
    (num_textures,) = struct.unpack_from("<I", toc_data, 0x40)
    tex_ids_start = 136 + 4 * num_textures
    end = tex_ids_start + 8 * num_textures
    if num_textures == 0 or end > len(toc_data):
        return []
    return list(struct.unpack_from(f"<{num_textures}Q", toc_data, tex_ids_start))


# Dispatch by TypeID.

REWRITERS = {
    UnitID: rewrite_unit,
    MaterialID: rewrite_material,
}


def rewrite(
    type_id: int,
    toc_data: bytes,
    remap: Dict[int, int],
    slot_remap: Optional[Dict[int, int]] = None,
) -> bytes:
    if type_id == UnitID:
        return rewrite_unit(toc_data, remap, slot_remap=slot_remap)
    if type_id == MaterialID:
        return rewrite_material(toc_data, remap)
    return toc_data
