"""
Empty-mesh padding.

Background
----------
HD2's armor patch system overrides FileIDs: if a patch contains FileID X,
the game uses the patch's entry; otherwise it loads the original. So when a
target armor has *more* unit slots than the source mod covers, those extra
slots still render the target armor's original part (e.g. a backpack mount).

To hide those extras, modders insert an "empty Unit" entry per
extra slot. This module automates that step:

  1. Extract a known-good empty Unit blob from one variant of the reference
     mod (it must contain at least one such entry — typically every
     variant does).
  2. For each target, look up "which FileIDs in the target's full slot list
     are NOT covered by the migrated patch", and append empty-Unit entries
     for those FileIDs.

Where the data comes from
-------------------------
- **Empty Unit template**: identify or bundle a Unit that can be normalized
  into a dependency-free, non-drawing mesh, and capture (toc_data, gpu_data,
  stream_data, slot_id_template).
- **Target full slot list**: union the FileIDs across all reference variants
  for a given target armor. Multiple reference variants of the same target
  shouldn't exist, but unique-FileIDs across the entire mod (per armor
  reference) gives the target's covered slots. For armors without a
  reference, the user must specify the slot list explicitly.
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .archive import StreamToc, TocEntry
from .constants import UnitID


# ---------- Empty-Unit template --------------------------------------------

@dataclass
class EmptyUnitTemplate:
    """A minimal Unit's serialized bytes, to be cloned per extra slot."""
    toc_data: bytes
    gpu_data: bytes
    stream_data: bytes
    source_file_id: int                 # FileID this template was extracted from
    source_slot_ids: List[int]          # uint32 SectionsIDs found inside

    def clone_for(self, target_file_id: int,
                  slot_id_remap: Optional[Dict[int, int]] = None,
                  file_id_remap: Optional[Dict[int, int]] = None,
                  verbatim: bool = False) -> TocEntry:
        """Return a new TocEntry with the template's data, retargeted.

        Parameters
        ----------
        target_file_id
            FileID to assign to the new entry (this always changes).
        slot_id_remap / file_id_remap
            Optional remaps applied to the template's TocData. Ignored when
            `verbatim=True`.
        verbatim
            If True, the template's toc_data is written **as-is** (no slot ID
            or FileID rewriting inside the blob). Only the top-level FileID
            on the TocEntry changes. Use this when you have hand-authored an
            empty mesh and want the bytes preserved exactly.
        """
        if verbatim:
            toc = self.toc_data
        else:
            from . import refs
            toc = sanitize_empty_unit(self.toc_data)
            toc = refs.rewrite_unit(
                toc,
                file_id_remap or {},
                slot_remap=slot_id_remap,
            )
        return TocEntry(
            file_id=target_file_id,
            type_id=UnitID,
            toc_data=toc,
            gpu_data=self.gpu_data,
            stream_data=self.stream_data,
        )


def find_empty_unit_candidates(patch: StreamToc) -> List[TocEntry]:
    """Heuristic: rank Units by GPU buffer size (smallest = most likely empty
    mesh / single-point culler). Caller picks the first."""
    units = [e for e in patch.entries if e.type_id == UnitID]
    units.sort(key=lambda e: (len(e.gpu_data), len(e.toc_data)))
    return units


_cached_builtin: Optional[EmptyUnitTemplate] = None


def builtin_template() -> EmptyUnitTemplate:
    """Return the project-bundled empty Unit template source.

    The bytes come from `_builtin_empty_mesh.py` (a base64-encoded Blender
    export packaged as a standard LEGACY patch file). We parse that patch on
    first access to extract the inner Unit's TocData / GpuData / StreamData
    blobs — the same pieces `extract_template()` would pull off disk.

    This is the default padding template when the user does not pass
    `--empty-mesh-from`.
    """
    global _cached_builtin
    if _cached_builtin is not None:
        return _cached_builtin
    from . import _builtin_empty_mesh as m
    toc = StreamToc.from_buffers(
        toc_data=m.TOC_DATA, gpu_data=m.GPU_DATA, stream_data=m.STREAM_DATA,
        name="<builtin empty mesh>",
    )
    units = [e for e in toc.entries if e.type_id == UnitID]
    if not units:
        raise RuntimeError("built-in empty-mesh patch has no Unit entries")
    chosen = units[0]
    slot_ids: List[int] = []
    for pos in range(0, len(chosen.toc_data) - 3, 4):
        (v,) = struct.unpack_from("<I", chosen.toc_data, pos)
        slot_ids.append(v)
    _cached_builtin = EmptyUnitTemplate(
        toc_data=chosen.toc_data,
        gpu_data=chosen.gpu_data,
        stream_data=chosen.stream_data,
        source_file_id=chosen.file_id,
        source_slot_ids=slot_ids,
    )
    return _cached_builtin


def extract_template(patch_path: str, picker_index: int = 0) -> EmptyUnitTemplate:
    """Pull the smallest-GPU Unit out of a reference patch as the template."""
    p = StreamToc.from_files(patch_path)
    candidates = find_empty_unit_candidates(p)
    if not candidates:
        raise RuntimeError(f"no Unit entries in {patch_path}")
    chosen = candidates[picker_index]
    # Capture all uint32 words from the toc_data for later slot_id remapping
    # diagnostics; not used in clone() unless caller supplies a remap.
    slot_ids: List[int] = []
    for pos in range(0, len(chosen.toc_data) - 3, 4):
        (v,) = struct.unpack_from("<I", chosen.toc_data, pos)
        slot_ids.append(v)
    return EmptyUnitTemplate(
        toc_data=chosen.toc_data,
        gpu_data=chosen.gpu_data,
        stream_data=chosen.stream_data,
        source_file_id=chosen.file_id,
        source_slot_ids=slot_ids,
    )


# ---------- Empty-Unit audit / normalization -------------------------------

@dataclass
class EmptyUnitAudit:
    """Dependency and draw-call summary for an empty Unit candidate."""
    header_refs: List[int]
    material_ids: List[int]
    num_indices: int
    section_indices: List[int]

    @property
    def external_refs(self) -> List[int]:
        return [v for v in self.header_refs + self.material_ids if v != 0]

    @property
    def is_dependency_free(self) -> bool:
        return not self.external_refs

    @property
    def is_non_drawing(self) -> bool:
        return self.num_indices == 0 and all(v == 0 for v in self.section_indices)


def audit_empty_unit(toc_data: bytes) -> EmptyUnitAudit:
    """Return the external references and draw counts in a Unit blob."""
    header_refs = list(struct.unpack_from("<QQQQQ", toc_data, 0))
    material_ids = _read_material_ids(toc_data)
    section_indices = _read_section_index_counts(toc_data)
    num_indices = sum(section_indices)
    return EmptyUnitAudit(header_refs, material_ids, num_indices, section_indices)


def sanitize_empty_unit(toc_data: bytes) -> bytes:
    """Normalize a padding Unit so it has no material refs and draws nothing."""
    buf = bytearray(toc_data)
    _zero_global_material_refs(buf)
    _zero_stream_indices(buf)
    _zero_section_indices(buf)
    return bytes(buf)


def _header_offsets(toc_data: bytes) -> Tuple[int, int, int]:
    stream_off, ending_off, mesh_off = struct.unpack_from("<III", toc_data, 0x5C)
    materials_off, = struct.unpack_from("<I", toc_data, 0x70)
    return stream_off, mesh_off, materials_off


def _read_material_ids(toc_data: bytes) -> List[int]:
    _, _, materials_off = _header_offsets(toc_data)
    if materials_off == 0 or materials_off + 4 > len(toc_data):
        return []
    num_mats, = struct.unpack_from("<I", toc_data, materials_off)
    ids_off = materials_off + 4 + 4 * num_mats
    if ids_off + 8 * num_mats > len(toc_data):
        return []
    return list(struct.unpack_from(f"<{num_mats}Q", toc_data, ids_off))


def _stream_info_bases(toc_data: bytes) -> List[int]:
    stream_off, _, _ = _header_offsets(toc_data)
    if stream_off == 0 or stream_off + 4 > len(toc_data):
        return []
    num_streams, = struct.unpack_from("<I", toc_data, stream_off)
    offsets_at = stream_off + 4
    bases: List[int] = []
    for i in range(num_streams):
        rel, = struct.unpack_from("<I", toc_data, offsets_at + 4 * i)
        bases.append(stream_off + rel)
    return [b for b in bases if b + 416 <= len(toc_data)]


def _read_stream_index_counts(toc_data: bytes) -> List[int]:
    return [struct.unpack_from("<I", toc_data, base + 384)[0]
            for base in _stream_info_bases(toc_data)]


def _mesh_info_bases(toc_data: bytes) -> List[int]:
    _, mesh_off, _ = _header_offsets(toc_data)
    if mesh_off == 0 or mesh_off + 4 > len(toc_data):
        return []
    num_meshes, = struct.unpack_from("<I", toc_data, mesh_off)
    offsets_at = mesh_off + 4
    bases: List[int] = []
    for i in range(num_meshes):
        rel, = struct.unpack_from("<I", toc_data, offsets_at + 4 * i)
        bases.append(mesh_off + rel)
    return [b for b in bases if b + 128 <= len(toc_data)]


def _section_offsets(toc_data: bytes) -> List[int]:
    offsets: List[int] = []
    for base in _mesh_info_bases(toc_data):
        num_sections, section_rel = struct.unpack_from("<II", toc_data, base + 120)
        for i in range(num_sections):
            off = base + section_rel + 24 * i
            if off + 24 <= len(toc_data):
                offsets.append(off)
    return offsets


def _read_section_index_counts(toc_data: bytes) -> List[int]:
    return [struct.unpack_from("<I", toc_data, off + 16)[0]
            for off in _section_offsets(toc_data)]


def _zero_global_material_refs(buf: bytearray) -> None:
    ids = _read_material_ids(buf)
    _, _, materials_off = _header_offsets(buf)
    ids_off = materials_off + 4 + 4 * len(ids)
    for i in range(len(ids)):
        struct.pack_into("<Q", buf, ids_off + 8 * i, 0)


def _zero_stream_indices(buf: bytearray) -> None:
    for base in _stream_info_bases(buf):
        struct.pack_into("<I", buf, base + 384, 0)
        struct.pack_into("<I", buf, base + 412, 0)


def _zero_section_indices(buf: bytearray) -> None:
    for off in _section_offsets(buf):
        struct.pack_into("<I", buf, off + 16, 0)


# ---------- Slot-list inference from reference variants ---------------------

def collect_target_slot_lists(reference_dir: str,
                              patch_filename: str = "9ba626afa44a3aa3.patch_0"
                              ) -> Dict[str, List[int]]:
    """For each subfolder under reference_dir, return the list of Unit FileIDs
    seen in that variant's patch. The union across variants of the same target
    is the most complete view we have of that target's slot set."""
    out: Dict[str, List[int]] = {}
    for name in sorted(os.listdir(reference_dir)):
        sub = os.path.join(reference_dir, name)
        if not os.path.isdir(sub):
            continue
        path = os.path.join(sub, patch_filename)
        if not os.path.exists(path):
            continue
        p = StreamToc.from_files(path)
        fids = [e.file_id for e in p.entries if e.type_id == UnitID]
        out[name] = fids
    return out


# ---------- Padding ---------------------------------------------------------

def pad_patch(
    patch: StreamToc,
    target_full_unit_slots: List[int],
    template: EmptyUnitTemplate,
    slot_id_remap: Optional[Dict[int, int]] = None,
    verbatim: bool = False,
) -> Tuple[StreamToc, List[int]]:
    """Append empty-Unit entries for any FileID in `target_full_unit_slots`
    that the patch doesn't already cover.

    Returns (patch_with_padding, list_of_appended_file_ids).
    """
    covered = {e.file_id for e in patch.entries if e.type_id == UnitID}
    extras = [fid for fid in target_full_unit_slots if fid not in covered]
    for fid in extras:
        new_entry = template.clone_for(
            fid, slot_id_remap=slot_id_remap, verbatim=verbatim
        )
        patch.entries.append(new_entry)
    return patch, extras
