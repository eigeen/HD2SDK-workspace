"""Unit customization-name extraction shared by CLI armor migration."""
from __future__ import annotations

import re
from dataclasses import dataclass
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
    if any(value is None for value in values):
        return None
    return UnitCustomizationName(
        body_type=values[0] or "",
        slot=values[1] or "",
        weight=values[2] or "",
        piece_type=values[3] or "",
    )


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
