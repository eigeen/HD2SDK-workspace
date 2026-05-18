"""Semantic Unit slot matching for armor migration."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .archive import StreamToc, TocEntry
from .constants import UnitID


_CUSTOMIZATION_RE = re.compile(
    rb"HelldiverCustomization(?:BodyType|Slot|Weight|PieceType)_[A-Za-z0-9_]+"
)
_BODY_PREFIX = "HelldiverCustomizationBodyType_"
_SLOT_PREFIX = "HelldiverCustomizationSlot_"
_WEIGHT_PREFIX = "HelldiverCustomizationWeight_"
_PIECE_PREFIX = "HelldiverCustomizationPieceType_"
_PREFIXES = (_BODY_PREFIX, _SLOT_PREFIX, _WEIGHT_PREFIX, _PIECE_PREFIX)


@dataclass(frozen=True)
class UnitSemanticKey:
    body_type: str
    slot: str
    weight: str
    piece_type: str

    def label(self) -> str:
        """Return a compact human-readable key for diagnostics."""
        return "/".join((self.body_type, self.slot, self.weight, self.piece_type))


@dataclass(frozen=True)
class UnitSemanticIssue:
    source_file_id: int
    reason: str
    key: Optional[UnitSemanticKey] = None
    match_level: str = ""
    match_key: Tuple[str, ...] = ()
    candidates: Tuple[int, ...] = ()


@dataclass
class UnitSemanticRemap:
    remap: Dict[int, int] = field(default_factory=dict)
    match_levels: Dict[int, str] = field(default_factory=dict)
    missing: List[UnitSemanticIssue] = field(default_factory=list)
    ambiguous: List[UnitSemanticIssue] = field(default_factory=list)

    def is_complete(self) -> bool:
        """Return True when every source Unit had exactly one target match."""
        return not self.missing and not self.ambiguous


def extract_unit_semantic_key(toc_data: bytes) -> Optional[UnitSemanticKey]:
    """Extract the Unit customization semantic key from a Unit TocData blob."""
    values = _customization_values(toc_data)
    if any(value is None for value in values):
        return None
    return UnitSemanticKey(
        body_type=values[0] or "",
        slot=values[1] or "",
        weight=values[2] or "",
        piece_type=values[3] or "",
    )


def build_unit_semantic_remap(
    patch: StreamToc,
    source: StreamToc,
    target: StreamToc,
) -> UnitSemanticRemap:
    """Build Unit FileID remaps through prioritized customization metadata."""
    source_units = _entries_by_file_id(source, UnitID)
    target_index = _target_units_by_priority_key(target)
    result = UnitSemanticRemap()

    for patch_entry in patch.by_type().get(UnitID, []):
        if patch_entry.file_id not in source_units:
            continue
        key = _entry_semantic_key(patch_entry, source_units[patch_entry.file_id])
        if key is None:
            result.missing.append(
                UnitSemanticIssue(patch_entry.file_id, "missing source Unit semantic key")
            )
            continue
        _apply_priority_match(result, patch_entry.file_id, key, target_index)
    return result


def format_unit_semantic_issues(result: UnitSemanticRemap, limit: int = 6) -> str:
    """Format missing/ambiguous Unit semantic matches for error messages."""
    issues = result.missing + result.ambiguous
    parts: List[str] = []
    for issue in issues[:limit]:
        key = _format_issue_key(issue)
        extra = ""
        if issue.candidates:
            extra = f", candidates={list(issue.candidates)}"
        parts.append(f"{issue.source_file_id}: {issue.reason} ({key}{extra})")
    if len(issues) > limit:
        parts.append(f"... {len(issues) - limit} more")
    return "; ".join(parts)


def _customization_values(toc_data: bytes) -> List[Optional[str]]:
    strings = [
        match.group().decode("utf-8", errors="ignore")
        for match in _CUSTOMIZATION_RE.finditer(toc_data)
    ]
    values: List[Optional[str]] = []
    for prefix in _PREFIXES:
        matches = [item for item in strings if item.startswith(prefix)]
        values.append(matches[-1][len(prefix):] if matches else None)
    return values


def _entry_semantic_key(
    patch_entry: TocEntry,
    source_entry: TocEntry,
) -> Optional[UnitSemanticKey]:
    return (
        extract_unit_semantic_key(patch_entry.toc_data)
        or extract_unit_semantic_key(source_entry.toc_data)
    )


def _target_units_by_priority_key(target: StreamToc) -> Dict[Tuple[str, Tuple[str, ...]], List[TocEntry]]:
    out: Dict[Tuple[str, Tuple[str, ...]], List[TocEntry]] = {}
    for entry in target.by_type().get(UnitID, []):
        key = extract_unit_semantic_key(entry.toc_data)
        if key is None:
            continue
        for priority_key in _priority_keys(key):
            out.setdefault(priority_key, []).append(entry)
    return out


def _apply_priority_match(
    result: UnitSemanticRemap,
    source_file_id: int,
    key: UnitSemanticKey,
    target_index: Dict[Tuple[str, Tuple[str, ...]], List[TocEntry]],
) -> None:
    for match_level, match_key in _priority_keys(key):
        matches = target_index.get((match_level, match_key), [])
        if len(matches) == 1:
            result.remap[source_file_id] = matches[0].file_id
            result.match_levels[source_file_id] = match_level
            return
        if len(matches) > 1:
            result.ambiguous.append(
                UnitSemanticIssue(
                    source_file_id,
                    "multiple target Units for semantic priority key",
                    key,
                    match_level,
                    match_key,
                    tuple(entry.file_id for entry in matches),
                )
            )
            return
    result.missing.append(
        UnitSemanticIssue(source_file_id, "no target Unit for semantic priority keys", key)
    )


def _priority_keys(key: UnitSemanticKey) -> List[Tuple[str, Tuple[str, ...]]]:
    return [
        ("1234", (key.body_type, key.slot, key.weight, key.piece_type)),
        ("124", (key.body_type, key.slot, key.piece_type)),
        ("24", (key.slot, key.piece_type)),
    ]


def _format_issue_key(issue: UnitSemanticIssue) -> str:
    if issue.match_level:
        return f"{issue.match_level}:{'/'.join(issue.match_key)}"
    if issue.key is not None:
        return issue.key.label()
    return "<no key>"


def _entries_by_file_id(toc: StreamToc, type_id: int) -> Dict[int, TocEntry]:
    return {entry.file_id: entry for entry in toc.by_type().get(type_id, [])}
