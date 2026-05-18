"""
Mod-armor migrator core: given a mod patch that targets one armor archive,
produce a parallel patch for each requested target archive by remapping
FileIDs and the cross-resource references baked inside Unit / Material blobs.

Mapping strategy
----------------
Armor Unit entries are matched by direct mesh geometry, not by archive order or
embedded names. Unit migration uses explicit trusted remap JSON when supplied;
otherwise the tool compares source/target Unit vertex distributions.

For non-Unit resources that are structurally parallel, archive order is used
as a best-effort fallback. Count mismatches are warnings; Unit mismatches still
depend on geometry remap safety.
"""
from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .archive import BundleIndex, StreamToc, list_file_ids
from .constants import TYPE_NAMES, UnitID, MaterialID, TexID, BoneID
from . import refs
from .padding import EmptyUnitTemplate, extract_template, pad_patch
from .unit_geometry import (
    UnitGeometryRemap,
    build_unit_geometry_remap,
    format_unit_geometry_issues,
)


log = logging.getLogger("mod_armor_migrator")


class UnsafePartialRemapError(ValueError):
    """Raised when archive-derived remap would silently target wrong slots."""


# ---------- Archive index ---------------------------------------------------

@dataclass
class ArmorEntry:
    hash_hex: str
    name: str

    @property
    def path(self) -> str:
        return self.hash_hex


def load_armor_index(json_path: str, category: str = "Armor") -> List[ArmorEntry]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if category not in data:
        raise KeyError(f"category {category!r} not found in {json_path}; "
                       f"available: {sorted(data.keys())}")
    return [ArmorEntry(h, n) for h, n in data[category].items()]


# ---------- Source autodetect ----------------------------------------------

def detect_source_archive(
    patch_ids_by_type: Dict[int, List[int]],
    archives: List[ArmorEntry],
    data_dir: str,
    bundle_index: Optional[BundleIndex] = None,
) -> Optional[Tuple[ArmorEntry, int]]:
    """Return (best matching archive, hit count) or None."""
    flat_patch_ids = {(tid, fid) for tid, ids in patch_ids_by_type.items() for fid in ids}
    if not flat_patch_ids:
        return None

    best: Optional[Tuple[ArmorEntry, int]] = None
    for entry in archives:
        path = os.path.join(data_dir, entry.hash_hex)
        if not _archive_exists(path, bundle_index):
            continue
        try:
            arch_ids = list_file_ids(path, bundle_index=bundle_index)
        except Exception as e:
            log.debug("skip %s: %s", entry.hash_hex, e)
            continue
        arch_flat = {(tid, fid) for tid, ids in arch_ids.items() for fid in ids}
        hits = len(flat_patch_ids & arch_flat)
        if hits and (best is None or hits > best[1]):
            best = (entry, hits)
    return best


# ---------- Per-target remap ------------------------------------------------

@dataclass
class MigrationReport:
    target: ArmorEntry
    out_path: str
    remap_size: int
    skipped_types: List[int] = field(default_factory=list)
    written_entries: int = 0
    type_counts: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    # type_counts[tid] = (source_count, target_count)


@dataclass
class RemapPlan:
    remap: Dict[int, int]
    skipped_types: List[int]
    type_counts: Dict[int, Tuple[int, int]]
    skipped_file_ids: set[int]
    extra_unit_file_ids: List[int]


@dataclass(frozen=True)
class EntryRewriteContext:
    remap: Dict[int, int]
    slot_remap: Dict[int, int]
    unit_targets: Dict[int, Tuple[int, ...]]
    source_units: Dict[int, Any]
    target_units: Dict[int, Any]


def build_remap(
    source: StreamToc, target: StreamToc, log_prefix: str = ""
) -> RemapPlan:
    """Build a FileID remap from source to target archive entries.

    Unit entries are intentionally excluded because their archive order is not
    a stable slot identity. Automatic Unit mapping is done later by geometry.
    """
    src_by_type = source.by_type()
    tgt_by_type = target.by_type()
    remap: Dict[int, int] = {}
    skipped: List[int] = []
    counts: Dict[int, Tuple[int, int]] = {}
    skipped_file_ids: set[int] = set()

    for tid, src_entries in src_by_type.items():
        tgt_entries = tgt_by_type.get(tid, [])
        counts[tid] = (len(src_entries), len(tgt_entries))
        if len(tgt_entries) == 0:
            skipped.append(tid)
            skipped_file_ids.update(e.file_id for e in src_entries)
            log.debug("%s skip type %s (%s): not present in target",
                      log_prefix, TYPE_NAMES.get(tid, hex(tid)), tid)
            continue
        if len(src_entries) != len(tgt_entries):
            if tid == UnitID:
                log.warning(
                    "%s type Unit count mismatch: source=%d target=%d "
                    "(Unit order remap disabled)",
                    log_prefix, len(src_entries), len(tgt_entries),
                )
            else:
                log.warning(
                    "%s type %s count mismatch: source=%d target=%d "
                    "(partial ordinal remap)",
                    log_prefix, TYPE_NAMES.get(tid, hex(tid)),
                    len(src_entries), len(tgt_entries),
                )
        if tid == UnitID:
            if len(src_entries) > len(tgt_entries):
                skipped.append(tid)
                skipped_file_ids.update(e.file_id for e in src_entries)
            log.debug("%s skip Unit ordinal remap: Unit slots require geometry remap",
                      log_prefix)
            continue
        if len(src_entries) > len(tgt_entries):
            skipped.append(tid)
            skipped_file_ids.update(e.file_id for e in src_entries[len(tgt_entries):])
        for src_e, tgt_e in zip(src_entries, tgt_entries):
            if src_e.file_id != tgt_e.file_id:
                remap[src_e.file_id] = tgt_e.file_id
    src_units = src_by_type.get(UnitID, [])
    tgt_units = tgt_by_type.get(UnitID, [])
    extra_units = [e.file_id for e in tgt_units[len(src_units):]]
    return RemapPlan(remap, skipped, counts, skipped_file_ids, extra_units)


# ---------- Single migration ------------------------------------------------

def migrate_one(
    patch: StreamToc,
    source: Optional[StreamToc],
    target: Optional[StreamToc],
    target_entry: ArmorEntry,
    out_root: str,
    patch_suffix: str = "patch_0",
    precomputed: Optional[dict] = None,
    reference_remap: Optional[dict] = None,
    empty_unit_template: Optional[EmptyUnitTemplate] = None,
    empty_mesh_verbatim: bool = False,
    experimental_partial_remap: bool = False,
) -> MigrationReport:
    """Migrate one patch.

    Either provide (source, target) StreamToc pairs to derive the remap from
    armor archives, OR provide `precomputed` with {"file_ids": {...},
    "slot_ids": {...}} (typically from extract_remap.py).
    """
    if precomputed is not None:
        remap = {int(k): int(v) for k, v in precomputed.get("file_ids", {}).items()}
        slot_remap = {
            int(k, 16) if isinstance(k, str) and k.startswith("0x") else int(k):
            int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)
            for k, v in precomputed.get("slot_ids", {}).items()
        }
        skipped: List[int] = []
        counts: Dict[int, Tuple[int, int]] = {}
        skipped_file_ids: set[int] = set()
        extra_unit_file_ids = [int(x) for x in precomputed.get("extra_unit_file_ids", [])]
        unit_targets: Dict[int, Tuple[int, ...]] = {}
    else:
        assert source is not None and target is not None
        plan = build_remap(source, target, log_prefix=f"[{target_entry.name}]")
        remap = plan.remap
        skipped = plan.skipped_types
        counts = plan.type_counts
        skipped_file_ids = plan.skipped_file_ids
        extra_unit_file_ids = plan.extra_unit_file_ids
        slot_remap = {}
        unit_targets = {}
        write_file_ids: Optional[set[int]] = None
        if reference_remap is not None:
            ref_file_ids = {int(k): int(v) for k, v in reference_remap.get("file_ids", {}).items()}
            ref_slot_ids = {
                int(k, 16) if isinstance(k, str) and k.startswith("0x") else int(k):
                int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)
                for k, v in reference_remap.get("slot_ids", {}).items()
            }
            remap.update(ref_file_ids)
            slot_remap.update(ref_slot_ids)
            if "extra_unit_file_ids" in reference_remap:
                extra_unit_file_ids = [int(x) for x in reference_remap["extra_unit_file_ids"]]
            skipped_file_ids.difference_update(ref_file_ids.keys())
            log.info("[%s] applied reference remap override: %d FileIDs, %d slot IDs",
                     target_entry.name, len(ref_file_ids), len(ref_slot_ids))
        else:
            unit_remap = build_unit_geometry_remap(patch, source, target)
            if experimental_partial_remap:
                write_file_ids = _patch_dependency_file_ids(
                    patch,
                    set(unit_remap.remap.keys()),
                )
            if not unit_remap.is_complete():
                if not experimental_partial_remap:
                    raise UnsafePartialRemapError(
                        _format_unsafe_unit_geometry_message(target_entry, unit_remap)
                    )
                _log_experimental_unit_remap(target_entry, unit_remap)
                skipped_file_ids.update(_unit_issue_file_ids(unit_remap))
            remap.update(unit_remap.remap)
            if unit_remap.extra_unit_file_ids:
                extra_unit_file_ids = unit_remap.extra_unit_file_ids
            unit_targets = unit_remap.expanded_remap
            empty_remap, extra_unit_file_ids = _assign_empty_unit_placeholders(
                patch,
                unit_remap,
            )
            remap.update(empty_remap)
            skipped_file_ids.difference_update(unit_remap.remap.keys())
            skipped_file_ids.difference_update(empty_remap.keys())
            _log_unit_geometry_remap(target_entry, unit_remap)
            if empty_remap:
                log.info("[%s] mapped %d empty source Unit placeholder(s)",
                         target_entry.name, len(empty_remap))
            if write_file_ids is not None:
                _log_patch_dependency_summary(target_entry, patch, write_file_ids)
            _log_referenced_texture_remaps(patch, source, remap, target_entry, write_file_ids)
    rewrite_context = _entry_rewrite_context(remap, slot_remap, unit_targets, (source, target))
    new_patch = StreamToc()
    written = 0
    for e in patch.entries:
        if 'write_file_ids' in locals() and write_file_ids is not None and e.file_id not in write_file_ids:
            log.warning("[%s] dropping entry FileID=%d type=%s (not in migrated Unit dependency chain)",
                        target_entry.name, e.file_id, TYPE_NAMES.get(e.type_id, hex(e.type_id)))
            continue
        if e.file_id in skipped_file_ids:
            log.warning("[%s] dropping entry FileID=%d type=%s (target lacks matching slot)",
                        target_entry.name, e.file_id, TYPE_NAMES.get(e.type_id, hex(e.type_id)))
            continue

        for new_file_id in _entry_target_file_ids(e, rewrite_context):
            entry_remap = _entry_specific_remap(e, new_file_id, rewrite_context)
            new_entry = type(e)(
                file_id=new_file_id,
                type_id=e.type_id,
                toc_data=refs.rewrite(
                    e.type_id,
                    e.toc_data,
                    entry_remap,
                    slot_remap=slot_remap,
                ),
                gpu_data=e.gpu_data,
                stream_data=e.stream_data,
            )
            new_patch.entries.append(new_entry)
            written += 1

    # Apply padding for extra target unit slots not covered by the source mod.
    padded_extras: List[int] = []
    if extra_unit_file_ids and empty_unit_template is not None:
        new_patch, padded_extras = pad_patch(
            new_patch, extra_unit_file_ids, empty_unit_template,
            slot_id_remap=slot_remap, verbatim=empty_mesh_verbatim,
        )
        log.info("[%s] padded %d extra Unit slot(s) with empty mesh",
                 target_entry.name, len(padded_extras))
    elif extra_unit_file_ids and empty_unit_template is None:
        log.warning("[%s] target has %d extra Unit slot(s) but no empty-mesh "
                    "template supplied — extras NOT padded; original armor "
                    "parts will remain visible",
                    target_entry.name, len(extra_unit_file_ids))

    safe_name = _safe_filename(target_entry.name)
    out_dir = os.path.join(out_root, f"{target_entry.hash_hex}_{safe_name}")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{target_entry.hash_hex}.{patch_suffix}")
    new_patch.write(out_path)

    return MigrationReport(
        target=target_entry,
        out_path=out_path,
        remap_size=len(remap),
        skipped_types=skipped,
        written_entries=written,
        type_counts=counts,
    )


# ---------- Driver ----------------------------------------------------------

def migrate_all(
    patch_path: str,
    data_dir: str,
    out_dir: str,
    archive_index_json: str,
    source_hash: Optional[str] = None,
    target_hashes: Optional[List[str]] = None,
    category: str = "Armor",
    patch_suffix: str = "patch_0",
    empty_unit_template: Optional[EmptyUnitTemplate] = None,
    empty_mesh_verbatim: bool = False,
    reference_remap_json: Optional[str] = None,
    experimental_partial_remap: bool = False,
) -> List[MigrationReport]:
    os.makedirs(out_dir, exist_ok=True)
    bundle_index = _load_bundle_index(data_dir)
    reference_remaps = _load_reference_remaps(reference_remap_json)

    log.info("Loading patch: %s", patch_path)
    patch = StreamToc.from_files(patch_path)
    log.info("  %d entries across %d types", len(patch.entries), len(patch.by_type()))

    archives = load_armor_index(archive_index_json, category=category)
    by_hash = {e.hash_hex: e for e in archives}

    # Pick source.
    if source_hash:
        if source_hash not in by_hash:
            raise SystemExit(f"--source {source_hash} not found in {category} index")
        source_entry = by_hash[source_hash]
        log.info("Source archive: %s (%s) [user-specified]",
                 source_entry.hash_hex, source_entry.name)
    else:
        patch_ids = {}
        for e in patch.entries:
            patch_ids.setdefault(e.type_id, []).append(e.file_id)
        detected = detect_source_archive(patch_ids, archives, data_dir, bundle_index=bundle_index)
        if detected is None:
            raise SystemExit(
                "Could not auto-detect the source armor archive. "
                "Pass --source <hash> explicitly. The patch's FileIDs did not "
                f"match any {category} archive in {data_dir}."
            )
        source_entry, hits = detected
        log.info("Source archive auto-detected: %s (%s) — %d FileIDs match",
                 source_entry.hash_hex, source_entry.name, hits)

    source_path = os.path.join(data_dir, source_entry.hash_hex)
    source = StreamToc.from_files(source_path, bundle_index=bundle_index)
    log.info("Source loaded: %d entries", len(source.entries))

    # Targets.
    if target_hashes:
        targets = []
        for h in target_hashes:
            if h not in by_hash:
                log.warning("target %s not in index, skipping", h)
                continue
            targets.append(by_hash[h])
    else:
        targets = [e for e in archives if e.hash_hex != source_entry.hash_hex]

    reports: List[MigrationReport] = []
    for tgt in targets:
        tgt_path = os.path.join(data_dir, tgt.hash_hex)
        if not _archive_exists(tgt_path, bundle_index):
            log.warning("target %s (%s) missing from data dir, skipping",
                        tgt.hash_hex, tgt.name)
            continue
        try:
            target = StreamToc.from_files(tgt_path, bundle_index=bundle_index)
        except Exception as e:
            log.error("failed to load target %s: %s", tgt.hash_hex, e)
            continue
        try:
            reference_remap = reference_remaps.get(tgt.name)
            r = migrate_one(
                patch, source, target, tgt, out_dir,
                patch_suffix=patch_suffix,
                reference_remap=reference_remap,
                empty_unit_template=empty_unit_template,
                empty_mesh_verbatim=empty_mesh_verbatim,
                experimental_partial_remap=experimental_partial_remap,
            )
        except UnsafePartialRemapError as e:
            log.error("migration to %s blocked: %s", tgt.hash_hex, e)
            continue
        except Exception as e:
            log.exception("migration to %s failed: %s", tgt.hash_hex, e)
            continue
        log.info(
            "  -> %s [%s]: %d entries, %d ids remapped, %d types skipped",
            tgt.name, tgt.hash_hex, r.written_entries, r.remap_size, len(r.skipped_types),
        )
        reports.append(r)

    return reports


# ---------- Precomputed-remap driver ----------------------------------------

def migrate_from_remap_json(
    patch_path: str,
    remap_json_path: str,
    out_dir: str,
    target_names: Optional[List[str]] = None,
    patch_suffix: str = "patch_0",
    empty_unit_template: Optional[EmptyUnitTemplate] = None,
    empty_mesh_verbatim: bool = False,
) -> List[MigrationReport]:
    """Migrate using a precomputed remap table (from extract_remap.py).

    No game data directory needed. Output filenames inherit the patch_filename
    from the remap.json (typically `9ba626afa44a3aa3.patch_0`).
    """
    os.makedirs(out_dir, exist_ok=True)
    with open(remap_json_path, "r", encoding="utf-8") as f:
        table: Dict[str, Any] = json.load(f)

    patch_filename = table.get("patch_filename", os.path.basename(patch_path))
    base_hash = patch_filename.split(".")[0]
    log.info("Loading patch: %s", patch_path)
    patch = StreamToc.from_files(patch_path)
    log.info("  %d entries across %d types", len(patch.entries), len(patch.by_type()))

    targets = table["targets"]
    if target_names:
        targets = {k: v for k, v in targets.items() if k in target_names}

    reports: List[MigrationReport] = []
    for name, payload in targets.items():
        target_entry = ArmorEntry(hash_hex=base_hash, name=name)
        try:
            r = migrate_one(
                patch=patch,
                source=None,
                target=None,
                target_entry=target_entry,
                out_root=out_dir,
                patch_suffix=patch_suffix,
                precomputed=payload,
                empty_unit_template=empty_unit_template,
                empty_mesh_verbatim=empty_mesh_verbatim,
            )
        except Exception as e:
            log.exception("migration to %s failed: %s", name, e)
            continue
        # Rename output file to use the patch_filename's base (since all
        # variants share the same `9ba626afa44a3aa3.patch_0` naming).
        log.info("  -> %s: %d entries, %d FileIDs, %d slot IDs",
                 name, r.written_entries, r.remap_size, len(payload.get("slot_ids", {})))
        reports.append(r)
    return reports


# ---------- helpers ---------------------------------------------------------

def _safe_filename(s: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in s).strip().rstrip(".")


def _load_bundle_index(data_dir: str) -> Optional[BundleIndex]:
    bundle_path = os.path.join(data_dir, "bundles.nxa")
    if not os.path.exists(bundle_path):
        return None
    log.info("Loading Slim bundle index: %s", bundle_path)
    return BundleIndex.from_data_dir(data_dir)


def _archive_exists(path: str, bundle_index: Optional[BundleIndex]) -> bool:
    if os.path.exists(path):
        return True
    return bundle_index is not None and bundle_index.has_package(path)


def _load_reference_remaps(path: Optional[str]) -> Dict[str, dict]:
    if path is None:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("targets", {})


def _entries_by_file_id(toc: StreamToc, type_id: int) -> Dict[int, Any]:
    """Index entries of one TypeID by FileID."""
    return {entry.file_id: entry for entry in toc.by_type().get(type_id, [])}


def _entry_rewrite_context(
    remap: Dict[int, int],
    slot_remap: Dict[int, int],
    unit_targets: Dict[int, Tuple[int, ...]],
    archives: Tuple[Optional[StreamToc], Optional[StreamToc]],
) -> EntryRewriteContext:
    """Prepare immutable lookup state for writing migrated patch entries."""
    source, target = archives
    source_units = _entries_by_file_id(source, UnitID) if source is not None else {}
    target_units = _entries_by_file_id(target, UnitID) if target is not None else {}
    return EntryRewriteContext(remap, slot_remap, unit_targets, source_units, target_units)


def _assign_empty_unit_placeholders(
    patch: StreamToc,
    unit_remap: UnitGeometryRemap,
) -> Tuple[Dict[int, int], List[int]]:
    """Map invisible source Unit placeholders onto unmatched target Unit slots."""
    target_ids = list(unit_remap.extra_unit_file_ids)
    assignments: Dict[int, int] = {}
    for entry in _empty_patch_unit_entries(patch, unit_remap):
        if not target_ids:
            break
        assignments[entry.file_id] = target_ids.pop(0)
    return assignments, target_ids


def _empty_patch_unit_entries(
    patch: StreamToc,
    unit_remap: UnitGeometryRemap,
) -> List[Any]:
    empty_ids = unit_remap.empty_source_file_ids
    return [
        entry for entry in patch.by_type().get(UnitID, [])
        if entry.file_id in empty_ids
    ]


def _entry_target_file_ids(entry: Any, context: EntryRewriteContext) -> Tuple[int, ...]:
    """Return one or more output FileIDs for a patch entry."""
    if entry.type_id == UnitID and entry.file_id in context.unit_targets:
        return context.unit_targets[entry.file_id]
    return (context.remap.get(entry.file_id, entry.file_id),)


def _entry_specific_remap(
    entry: Any,
    target_file_id: int,
    context: EntryRewriteContext,
) -> Dict[int, int]:
    """Return remap table with Unit header refs specialized for one target Unit."""
    if entry.type_id != UnitID or entry.file_id == target_file_id:
        return context.remap
    if entry.file_id not in context.source_units or target_file_id not in context.target_units:
        return context.remap
    remap = dict(context.remap)
    source_refs = _unit_header_refs(context.source_units[entry.file_id].toc_data)
    target_refs = _unit_header_refs(context.target_units[target_file_id].toc_data)
    _add_header_pair_remaps_silent(remap, source_refs, target_refs)
    return remap


def _add_header_pair_remaps_silent(
    remap: Dict[int, int],
    source_refs: Tuple[int, ...],
    target_refs: Tuple[int, ...],
) -> None:
    for source_ref, target_ref in zip(source_refs, target_refs):
        if source_ref == 0 or target_ref == 0:
            continue
        remap[source_ref] = target_ref


def _patch_dependency_file_ids(patch: StreamToc, unit_file_ids: set[int]) -> set[int]:
    """Return patch entries reachable from migrated Unit entries."""
    patch_entries = {entry.file_id: entry for entry in patch.entries}
    result = set(unit_file_ids)
    queue = [patch_entries[file_id] for file_id in unit_file_ids if file_id in patch_entries]
    while queue:
        entry = queue.pop(0)
        for ref_id in _patch_refs(entry, patch_entries):
            if ref_id in result:
                continue
            result.add(ref_id)
            queue.append(patch_entries[ref_id])
    return result


def _patch_refs(entry, patch_entries: Dict[int, Any]) -> List[int]:
    """Return FileIDs referenced by one patch entry that are also in the patch."""
    if entry.type_id == UnitID:
        raw_refs = refs.list_unit_refs(entry.toc_data)
    elif entry.type_id == MaterialID:
        raw_refs = refs.list_material_refs(entry.toc_data)
    else:
        raw_refs = []
    return [ref_id for ref_id in raw_refs if ref_id in patch_entries]


def _format_unsafe_unit_geometry_message(
    target_entry: ArmorEntry,
    unit_remap: UnitGeometryRemap,
) -> str:
    """Describe why Unit geometry matching could not produce a full remap."""
    details = format_unit_geometry_issues(unit_remap)
    return (
        f"[{target_entry.name}] incomplete Unit geometry remap. "
        "Unit slots are matched by mesh geometry and must not be matched by "
        f"archive order. {details}"
    )


def _unit_issue_file_ids(unit_remap: UnitGeometryRemap) -> set[int]:
    """Return all source Unit FileIDs that relaxed geometry matching could not map."""
    return {issue.source_file_id for issue in unit_remap.missing + unit_remap.ambiguous}


def _log_experimental_unit_remap(
    target_entry: ArmorEntry,
    unit_remap: UnitGeometryRemap,
) -> None:
    """Warn about skipped Units when experimental partial output is requested."""
    details = format_unit_geometry_issues(unit_remap, limit=12)
    log.warning(
        "[%s] experimental partial Unit geometry remap: %d mapped, %d skipped. %s",
        target_entry.name,
        len(unit_remap.remap),
        len(_unit_issue_file_ids(unit_remap)),
        details,
    )


def _add_unit_header_ref_remaps(
    remap: Dict[int, int],
    unit_remap: UnitGeometryRemap,
    source: StreamToc,
    target: StreamToc,
    target_entry: ArmorEntry,
) -> None:
    """Map Unit header dependencies through matched source/target Unit pairs."""
    source_units = _entries_by_file_id(source, UnitID)
    target_units = _entries_by_file_id(target, UnitID)
    added = 0
    for source_id, target_id in unit_remap.remap.items():
        source_refs = _unit_header_refs(source_units[source_id].toc_data)
        target_refs = _unit_header_refs(target_units[target_id].toc_data)
        added += _add_header_pair_remaps(remap, source_refs, target_refs, target_entry)
    if added:
        log.info("[%s] added %d Unit header dependency remap(s)", target_entry.name, added)


def _add_header_pair_remaps(
    remap: Dict[int, int],
    source_refs: Tuple[int, ...],
    target_refs: Tuple[int, ...],
    target_entry: ArmorEntry,
) -> int:
    added = 0
    for source_ref, target_ref in zip(source_refs, target_refs):
        if source_ref == 0 or target_ref == 0 or source_ref == target_ref:
            continue
        existing = remap.get(source_ref)
        if existing is not None and existing != target_ref:
            log.warning("[%s] conflicting Unit header dependency remap %d -> %d/%d",
                        target_entry.name, source_ref, existing, target_ref)
            continue
        if existing is None:
            added += 1
        remap[source_ref] = target_ref
    return added


def _unit_header_refs(toc_data: bytes) -> Tuple[int, ...]:
    if len(toc_data) < 0x28:
        return (0, 0, 0, 0, 0)
    return struct.unpack_from("<QQQQQ", toc_data, 0)


def _log_unit_geometry_remap(
    target_entry: ArmorEntry,
    unit_remap: UnitGeometryRemap,
) -> None:
    """Log geometry Unit remap scores and top candidates."""
    log.info("[%s] applied geometry Unit remap: %d FileIDs",
             target_entry.name, len(unit_remap.remap))
    for source_id, target_ids in sorted(unit_remap.expanded_remap.items()):
        score = unit_remap.scores.get(source_id, 0.0)
        margin = unit_remap.margins.get(source_id, 0.0)
        ranking = unit_remap.rankings.get(source_id, ())
        log.info(
            "[%s]   Unit %d -> %s level=%s score=%.4f margin=%.4f candidates=%s",
            target_entry.name,
            source_id,
            list(target_ids),
            unit_remap.match_levels.get(source_id, "geometry"),
            score,
            margin,
            list(ranking),
        )


def _log_referenced_texture_remaps(
    patch: StreamToc,
    source: StreamToc,
    remap: Dict[int, int],
    target_entry: ArmorEntry,
    write_file_ids: Optional[set[int]] = None,
) -> None:
    """Log source Texture FileIDs referenced by patch Materials and their remaps."""
    referenced = _referenced_source_texture_ids(patch, source, write_file_ids)
    if not referenced:
        return
    mapped = [tex_id for tex_id in referenced if tex_id in remap]
    missing = [tex_id for tex_id in referenced if tex_id not in remap]
    log.info(
        "[%s] source Texture refs from Materials: %d mapped, %d missing",
        target_entry.name,
        len(mapped),
        len(missing),
    )
    for tex_id in mapped[:12]:
        log.info("[%s]   Texture %d -> %d", target_entry.name, tex_id, remap[tex_id])
    if len(mapped) > 12:
        log.info("[%s]   ... %d more mapped Texture refs", target_entry.name, len(mapped) - 12)
    if missing:
        log.warning("[%s] missing Texture remap refs: %s", target_entry.name, missing[:12])


def _log_patch_dependency_summary(
    target_entry: ArmorEntry,
    patch: StreamToc,
    write_file_ids: set[int],
) -> None:
    """Log which patch entries are retained by the migrated Unit dependency chain."""
    counts: Dict[str, int] = {}
    for entry in patch.entries:
        if entry.file_id not in write_file_ids:
            continue
        name = TYPE_NAMES.get(entry.type_id, hex(entry.type_id))
        counts[name] = counts.get(name, 0) + 1
    details = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    log.info("[%s] migrated Unit dependency patch entries: %s", target_entry.name, details)


def _referenced_source_texture_ids(
    patch: StreamToc,
    source: StreamToc,
    write_file_ids: Optional[set[int]] = None,
) -> List[int]:
    """Return source Texture FileIDs referenced by patch Material blobs."""
    source_textures = {
        entry.file_id for entry in source.by_type().get(TexID, [])
    }
    out: List[int] = []
    for entry in patch.by_type().get(MaterialID, []):
        if write_file_ids is not None and entry.file_id not in write_file_ids:
            continue
        for tex_id in refs.list_material_refs(entry.toc_data):
            if tex_id in source_textures and tex_id not in out:
                out.append(tex_id)
    return out
