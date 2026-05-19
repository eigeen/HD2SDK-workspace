"""Unit customization-name extraction shared by CLI armor migration."""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional


_CUSTOMIZATION_RE = re.compile(
    rb"HelldiverCustomization(?:BodyType|Slot|Weight|PieceType)_[A-Za-z0-9_]+"
)
_BODY_PREFIX = "HelldiverCustomizationBodyType_"
_SLOT_PREFIX = "HelldiverCustomizationSlot_"
_WEIGHT_PREFIX = "HelldiverCustomizationWeight_"
_PIECE_PREFIX = "HelldiverCustomizationPieceType_"
_PREFIXES = (_BODY_PREFIX, _SLOT_PREFIX, _WEIGHT_PREFIX, _PIECE_PREFIX)
_BODY_VARIANT_ALIASES = {
    "Any": "Any",
    "Stocky": "Stocky",
    "Slim": "Slim",
}


@dataclass(frozen=True)
class UnitCustomizationName:
    body_type: str
    slot: str
    weight: str
    piece_type: str

    def label(self) -> str:
        """Return the NameFromMesh-style customization path."""
        return "/".join((self.body_type, self.slot, self.weight, self.piece_type))

    def body_variant(self) -> str:
        """Return the BodyType variant used to constrain geometry matching."""
        return _BODY_VARIANT_ALIASES.get(self.body_type, "Unknown")


def extract_unit_customization_name(toc_data: bytes) -> Optional[UnitCustomizationName]:
    """Extract BodyType/Slot/Weight/PieceType strings from Unit TocData."""
    values = _customization_values(toc_data)
    if not any(value is None for value in values):
        return UnitCustomizationName(
            body_type=values[0] or "",
            slot=values[1] or "",
            weight=values[2] or "",
            piece_type=values[3] or "",
        )
    return _extract_bonehash_customization_name(toc_data)


def unit_body_variant(toc_data: bytes) -> str:
    """Return Stocky/Slim/Any, keeping missing names in an Unknown bucket."""
    name = extract_unit_customization_name(toc_data)
    if name is None:
        return "Unknown"
    return name.body_variant()


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


def _extract_bonehash_customization_name(toc_data: bytes) -> Optional[UnitCustomizationName]:
    """Infer customization semantics from known mesh group bone hashes."""
    matches = {
        semantic
        for name in _bonehash_names_in_blob(toc_data)
        for semantic in [_semantic_from_bone_name(name)]
        if semantic is not None
    }
    if len(matches) != 1:
        return None
    body_type, slot, piece_type = next(iter(matches))
    return UnitCustomizationName(body_type, slot, "Medium", piece_type)


def _bonehash_names_in_blob(toc_data: bytes) -> List[str]:
    bone_names = _load_relevant_bonehash_names()
    names: List[str] = []
    for offset in range(0, max(0, len(toc_data) - 3)):
        value = struct.unpack_from("<I", toc_data, offset)[0]
        name = bone_names.get(value)
        if name is not None:
            names.append(name)
    return names


@lru_cache(maxsize=1)
def _load_relevant_bonehash_names() -> dict[int, str]:
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "hashlists", "bonehash.txt")
    )
    names: dict[int, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                parsed = _parse_bonehash_line(line)
                if parsed is None:
                    continue
                value, name = parsed
                if _semantic_from_bone_name(name) is not None:
                    names[value] = name
    except OSError:
        return {}
    return names


def _parse_bonehash_line(line: str) -> Optional[tuple[int, str]]:
    parts = line.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


def _semantic_from_bone_name(name: str) -> Optional[tuple[str, str, str]]:
    normalized = _normalize_bone_mesh_name(name)
    if not normalized.startswith(("g_", "grp_")):
        return None
    parts = normalized.split("_")
    if parts[-1] not in {"male", "female"}:
        return None
    body_type = "Stocky" if parts[-1] == "male" else "Slim"
    slot_piece = _slot_piece_from_bone_part("_".join(parts[1:-1]))
    if slot_piece is None:
        return None
    slot, piece_type = slot_piece
    return body_type, slot, piece_type


def _normalize_bone_mesh_name(name: str) -> str:
    lower = name.lower()
    lower = re.sub(r"_lod\d+$", "", lower)
    lower = re.sub(r"_(shadow|cloth)$", "", lower)
    return lower


def _slot_piece_from_bone_part(part: str) -> Optional[tuple[str, str]]:
    if part in {"torso_undergarment"}:
        return "Torso", "Undergarment"
    if part in {"torso"}:
        return "Torso", "Armor"
    if part in {"torso_arm_l"}:
        return "LeftArm", "Undergarment"
    if part in {"torso_arm_r"}:
        return "RightArm", "Undergarment"
    if part in {"shoulder_l", "l_shoulder"}:
        return "LeftShoulder", "Armor"
    if part in {"shoulder_r", "r_shoulder"}:
        return "RightShoulder", "Armor"
    if part in {"legs_hips_undergarment"}:
        return "Hip", "Undergarment"
    if part in {"legs_hips"}:
        return "Hip", "Armor"
    return None
