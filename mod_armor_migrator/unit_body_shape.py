"""Body-shape tie-breakers for Stocky/Slim Unit geometry matches."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .unit_names import UnitCustomizationName


@dataclass(frozen=True)
class BodyVariantPair:
    stocky_source_id: int
    slim_source_id: int


@dataclass(frozen=True)
class BodyPairPreassignmentRequest:
    result: Any
    source_signatures: Dict[int, Any]
    target_signatures: Dict[int, Any]
    source_names: Dict[int, Optional[UnitCustomizationName]]
    target_names: Dict[int, Optional[UnitCustomizationName]]
    target_variants: Dict[int, str]
    active_source_ids: set[int]


_EXPANSION_SAMPLE_COUNT = 512
_EXPANSION_THRESHOLD = 0.00005
_PAIR_SCORE_LIMIT = 1.0
_DEPTH_EXTENT_THRESHOLD = 0.0005


def apply_body_variant_pair_preassignment(request: BodyPairPreassignmentRequest) -> set[int]:
    """Assign Stocky/Slim source pairs to target near-twin pairs before single matching."""
    source_pairs = _preassignable_source_pairs(request)
    target_pairs = _unknown_near_twin_target_pairs(request)
    candidates = _body_pair_candidates(request, source_pairs, target_pairs)
    for pair, values in candidates.items():
        if not values:
            _record_unmatched_body_pair(request, pair)
    assignments = _solve_body_pair_assignment(
        tuple(pair for pair in source_pairs if candidates.get(pair)),
        candidates,
    )
    for pair in source_pairs:
        if candidates.get(pair) and pair not in assignments:
            _record_unmatched_body_pair(request, pair)
    taken_targets: set[int] = set()
    for pair, targets in assignments.items():
        ordered_targets = _orient_body_pair_targets(request, pair, targets)
        if ordered_targets is None:
            _record_ambiguous_body_pair(request, pair, targets)
            continue
        _set_body_pair_targets(
            request.result,
            request.source_signatures,
            request.target_signatures,
            pair,
            ordered_targets,
            "body-pair",
        )
        request.result.claimed_target_file_ids.update(ordered_targets)
        taken_targets.update(ordered_targets)
    return taken_targets


def apply_body_variant_pair_tiebreak(
    result: Any,
    source_signatures: Dict[int, Any],
    target_signatures: Dict[int, Any],
    source_names: Dict[int, Optional[UnitCustomizationName]],
    target_variants: Dict[int, str],
    active_source_ids: set[int],
) -> None:
    """Swap near-twin Unknown targets when directed expansion proves Stocky."""
    for pair in _body_variant_pairs(source_names, active_source_ids):
        if not _source_pair_has_stocky_expansion(source_signatures, pair):
            continue
        targets = _assigned_pair_targets(result, pair)
        if targets is None:
            continue
        stocky_target_id, slim_target_id = targets
        if not _can_compare_targets(target_variants, stocky_target_id, slim_target_id):
            continue
        if not _targets_are_near_twins(target_signatures, stocky_target_id, slim_target_id):
            continue
        fatter_target_id = _fatter_target_id(target_signatures, targets)
        if fatter_target_id is None or fatter_target_id == stocky_target_id:
            continue
        desired = fatter_target_id, stocky_target_id
        _set_body_pair_targets(result, source_signatures, target_signatures, pair, desired, "body-shape")


def _preassignable_source_pairs(
    request: BodyPairPreassignmentRequest,
) -> Tuple[BodyVariantPair, ...]:
    pairs = []
    for pair in _body_variant_pairs(request.source_names, request.active_source_ids):
        if pair.stocky_source_id not in request.source_signatures:
            continue
        if pair.slim_source_id not in request.source_signatures:
            continue
        if _has_exact_part_target(request, pair):
            continue
        pairs.append(pair)
    return tuple(pairs)


def _has_exact_part_target(
    request: BodyPairPreassignmentRequest,
    pair: BodyVariantPair,
) -> bool:
    source_name = request.source_names.get(pair.stocky_source_id)
    if source_name is None:
        return False
    return any(_same_part(source_name, name) for name in request.target_names.values())


def _same_part(
    source_name: UnitCustomizationName,
    target_name: Optional[UnitCustomizationName],
) -> bool:
    if target_name is None:
        return False
    return (
        source_name.slot == target_name.slot
        and source_name.weight == target_name.weight
        and source_name.piece_type == target_name.piece_type
    )


def _unknown_near_twin_target_pairs(
    request: BodyPairPreassignmentRequest,
) -> Tuple[Tuple[int, int], ...]:
    target_ids = [
        target_id
        for target_id in request.target_signatures
        if request.target_variants.get(target_id, "Unknown") == "Unknown"
    ]
    pairs: List[Tuple[int, int]] = []
    for left_index, left_id in enumerate(target_ids):
        for right_id in target_ids[left_index + 1:]:
            if _targets_are_near_twins(request.target_signatures, left_id, right_id):
                pairs.append((left_id, right_id))
    return tuple(pairs)


def _body_pair_candidates(
    request: BodyPairPreassignmentRequest,
    source_pairs: Tuple[BodyVariantPair, ...],
    target_pairs: Tuple[Tuple[int, int], ...],
) -> Dict[BodyVariantPair, Tuple[Tuple[Tuple[int, int], float], ...]]:
    candidates: Dict[BodyVariantPair, Tuple[Tuple[Tuple[int, int], float], ...]] = {}
    for pair in source_pairs:
        ranked = [
            (targets, _body_pair_score(request, pair, targets))
            for targets in target_pairs
        ]
        candidates[pair] = tuple(
            item for item in sorted(ranked, key=lambda value: value[1])
            if item[1] <= _PAIR_SCORE_LIMIT
        )
    return candidates


def _body_pair_score(
    request: BodyPairPreassignmentRequest,
    pair: BodyVariantPair,
    targets: Tuple[int, int],
) -> float:
    left_id, right_id = targets
    stocky = request.source_signatures[pair.stocky_source_id]
    slim = request.source_signatures[pair.slim_source_id]
    left = request.target_signatures[left_id]
    right = request.target_signatures[right_id]
    direct = _shape_score(stocky, left) + _shape_score(slim, right)
    swapped = _shape_score(stocky, right) + _shape_score(slim, left)
    return min(direct, swapped)


def _shape_score(source: Any, target: Any) -> float:
    scale = max(source.diagonal, target.diagonal, 0.000001)
    extent_score = _distance(_sorted_extents(source), _sorted_extents(target)) / scale
    diagonal_score = abs(math.log((source.diagonal + 0.000001) / (target.diagonal + 0.000001)))
    count_score = abs(math.log((source.vertex_count + 1) / (target.vertex_count + 1)))
    return extent_score + 0.25 * diagonal_score + 0.05 * count_score


def _sorted_extents(signature: Any) -> Tuple[float, float, float]:
    return tuple(sorted(signature.extents))


def _solve_body_pair_assignment(
    source_pairs: Tuple[BodyVariantPair, ...],
    candidates: Dict[BodyVariantPair, Tuple[Tuple[Tuple[int, int], float], ...]],
) -> Dict[BodyVariantPair, Tuple[int, int]]:
    assigned: Dict[BodyVariantPair, Tuple[int, int]] = {}
    used_targets: set[int] = set()
    for pair in sorted(source_pairs, key=lambda item: _best_pair_score(candidates, item)):
        for targets, _score in candidates[pair]:
            if targets[0] in used_targets or targets[1] in used_targets:
                continue
            assigned[pair] = targets
            used_targets.update(targets)
            break
    return assigned


def _best_pair_score(
    candidates: Dict[BodyVariantPair, Tuple[Tuple[Tuple[int, int], float], ...]],
    pair: BodyVariantPair,
) -> float:
    return candidates[pair][0][1] if candidates[pair] else float("inf")


def _orient_body_pair_targets(
    request: BodyPairPreassignmentRequest,
    pair: BodyVariantPair,
    targets: Tuple[int, int],
) -> Optional[Tuple[int, int]]:
    fatter_target_id = _fatter_target_id(request.target_signatures, targets)
    if fatter_target_id is not None:
        return fatter_target_id, _other_target(targets, fatter_target_id)
    stockier_target_id = _stockier_depth_target_id(request.target_signatures, targets)
    if stockier_target_id is not None:
        return stockier_target_id, _other_target(targets, stockier_target_id)
    return None


def _stockier_depth_target_id(
    target_signatures: Dict[int, Any],
    targets: Tuple[int, int],
) -> Optional[int]:
    left_id, right_id = targets
    left_depth = target_signatures[left_id].extents[2]
    right_depth = target_signatures[right_id].extents[2]
    if abs(left_depth - right_depth) < _DEPTH_EXTENT_THRESHOLD:
        return None
    return left_id if left_depth > right_depth else right_id


def _other_target(targets: Tuple[int, int], target_id: int) -> int:
    return targets[1] if targets[0] == target_id else targets[0]


def _record_ambiguous_body_pair(
    request: BodyPairPreassignmentRequest,
    pair: BodyVariantPair,
    targets: Tuple[int, int],
) -> None:
    from .unit_geometry import UnitGeometryIssue

    reason = "ambiguous Stocky/Slim body-pair target orientation"
    for source_id in (pair.stocky_source_id, pair.slim_source_id):
        request.result.ambiguous.append(UnitGeometryIssue(source_id, reason, targets))


def _record_unmatched_body_pair(
    request: BodyPairPreassignmentRequest,
    pair: BodyVariantPair,
) -> None:
    from .unit_geometry import UnitGeometryIssue

    reason = "no safe Stocky/Slim body-pair target candidates"
    for source_id in (pair.stocky_source_id, pair.slim_source_id):
        request.result.ambiguous.append(UnitGeometryIssue(source_id, reason))


def _body_variant_pairs(
    source_names: Dict[int, Optional[UnitCustomizationName]],
    active_source_ids: set[int],
) -> Tuple[BodyVariantPair, ...]:
    grouped: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    for source_id in active_source_ids:
        name = source_names.get(source_id)
        if name is None or name.body_variant() not in {"Stocky", "Slim"}:
            continue
        grouped.setdefault(_pair_key(name), {})[name.body_variant()] = source_id
    return tuple(
        BodyVariantPair(values["Stocky"], values["Slim"])
        for values in grouped.values()
        if "Stocky" in values and "Slim" in values
    )


def _pair_key(name: UnitCustomizationName) -> Tuple[str, str, str]:
    return name.slot, name.weight, name.piece_type


def _source_pair_has_stocky_expansion(
    source_signatures: Dict[int, Any],
    pair: BodyVariantPair,
) -> bool:
    slim = source_signatures[pair.slim_source_id]
    stocky = source_signatures[pair.stocky_source_id]
    return _is_directed_expansion(slim, stocky)


def _assigned_pair_targets(
    result: Any,
    pair: BodyVariantPair,
) -> Optional[Tuple[int, int]]:
    stocky_targets = result.expanded_remap.get(pair.stocky_source_id, ())
    slim_targets = result.expanded_remap.get(pair.slim_source_id, ())
    if len(stocky_targets) != 1 or len(slim_targets) != 1:
        return None
    return stocky_targets[0], slim_targets[0]


def _can_compare_targets(
    target_variants: Dict[int, str],
    stocky_target_id: int,
    slim_target_id: int,
) -> bool:
    return (
        target_variants.get(stocky_target_id, "Unknown") == "Unknown"
        and target_variants.get(slim_target_id, "Unknown") == "Unknown"
    )


def _targets_are_near_twins(
    target_signatures: Dict[int, Any],
    left_target_id: int,
    right_target_id: int,
) -> bool:
    left = target_signatures[left_target_id]
    right = target_signatures[right_target_id]
    scale = max(left.diagonal, right.diagonal, 0.000001)
    center_distance = _distance(left.center, right.center) / scale
    extent_distance = _distance(left.extents, right.extents) / scale
    return center_distance < 0.08 and extent_distance < 0.08


def _fatter_target_id(
    target_signatures: Dict[int, Any],
    targets: Tuple[int, int],
) -> Optional[int]:
    left_id, right_id = targets
    left = target_signatures[left_id]
    right = target_signatures[right_id]
    if _is_directed_expansion(left, right):
        return right_id
    if _is_directed_expansion(right, left):
        return left_id
    return None


def _is_directed_expansion(inner: Any, outer: Any) -> bool:
    outward = _signed_expansion_score(inner, outer)
    inward = _signed_expansion_score(outer, inner)
    return outward > _EXPANSION_THRESHOLD and inward < -_EXPANSION_THRESHOLD


def _signed_expansion_score(inner: Any, outer: Any) -> float:
    long_axis = max(range(3), key=lambda axis: inner.extents[axis])
    inner_points = _downsample_points(inner.points, _EXPANSION_SAMPLE_COUNT)
    outer_points = _downsample_points(outer.points, _EXPANSION_SAMPLE_COUNT)
    values = [
        value
        for point in inner_points
        if (value := _point_expansion(point, outer_points, inner.center, long_axis)) is not None
    ]
    return sum(values) / len(values) if values else 0.0


def _point_expansion(
    point: Tuple[float, float, float],
    outer_points: Tuple[Tuple[float, float, float], ...],
    center: Tuple[float, float, float],
    long_axis: int,
) -> Optional[float]:
    nearest = _nearest_point(point, outer_points)
    offset = _without_axis(_vector_subtract(nearest, point), long_axis)
    radial = _without_axis(_vector_subtract(point, center), long_axis)
    radial_length = _vector_length(radial)
    if radial_length < 0.000001:
        return None
    return _dot(offset, tuple(value / radial_length for value in radial))


def _downsample_points(
    points: Tuple[Tuple[float, float, float], ...],
    sample_count: int,
) -> Tuple[Tuple[float, float, float], ...]:
    if len(points) <= sample_count:
        return points
    last = len(points) - 1
    return tuple(points[round(last * index / (sample_count - 1))] for index in range(sample_count))


def _nearest_point(
    point: Tuple[float, float, float],
    points: Tuple[Tuple[float, float, float], ...],
) -> Tuple[float, float, float]:
    return min(points, key=lambda other: _squared_distance(point, other))


def _set_body_pair_targets(
    result: Any,
    source_signatures: Dict[int, Any],
    target_signatures: Dict[int, Any],
    pair: BodyVariantPair,
    targets: Tuple[int, int],
    level: str,
) -> None:
    stocky_target_id, slim_target_id = targets
    result.expanded_remap[pair.stocky_source_id] = (stocky_target_id,)
    result.expanded_remap[pair.slim_source_id] = (slim_target_id,)
    result.remap[pair.stocky_source_id] = stocky_target_id
    result.remap[pair.slim_source_id] = slim_target_id
    _refresh_score(result, source_signatures, target_signatures, pair.stocky_source_id, stocky_target_id, level)
    _refresh_score(result, source_signatures, target_signatures, pair.slim_source_id, slim_target_id, level)


def _refresh_score(
    result: Any,
    source_signatures: Dict[int, Any],
    target_signatures: Dict[int, Any],
    source_id: int,
    target_id: int,
    level: str,
) -> None:
    from .unit_geometry import score_signatures

    result.scores[source_id] = score_signatures(source_signatures[source_id], target_signatures[target_id])
    result.match_levels[source_id] = _append_match_level(result.match_levels.get(source_id, ""), level)


def _append_match_level(current: str, level: str) -> str:
    if not current:
        return level
    if level in current.split(","):
        return current
    return f"{current},{level}"


def _without_axis(vector: Tuple[float, float, float], axis: int) -> Tuple[float, float, float]:
    values = list(vector)
    values[axis] = 0.0
    return tuple(values)


def _vector_subtract(
    left: Tuple[float, float, float],
    right: Tuple[float, float, float],
) -> Tuple[float, float, float]:
    return tuple(left[index] - right[index] for index in range(3))


def _vector_length(vector: Tuple[float, float, float]) -> float:
    return math.sqrt(_dot(vector, vector))


def _dot(left: Tuple[float, float, float], right: Tuple[float, float, float]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _squared_distance(
    left: Tuple[float, float, float],
    right: Tuple[float, float, float],
) -> float:
    return sum((left[index] - right[index]) ** 2 for index in range(3))


def _distance(left: Tuple[float, ...], right: Tuple[float, ...]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(len(left))))
