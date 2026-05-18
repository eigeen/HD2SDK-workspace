"""Geometry-based Unit matching for armor migration."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .archive import StreamToc, TocEntry
from .constants import UnitID


Point3 = Tuple[float, float, float]
Matrix4 = Tuple[float, ...]


@dataclass(frozen=True)
class GeometryMatchSettings:
    max_score: float = 1.5
    min_margin: float = 0.0
    sample_count: int = 96
    quantiles: Tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 0.90)


@dataclass(frozen=True)
class UnitGeometrySignature:
    file_id: int
    points: Tuple[Point3, ...]
    sample_points: Tuple[Point3, ...]
    vertex_count: int
    center: Point3
    extents: Point3
    diagonal: float
    axis_quantiles: Tuple[float, ...]
    radial_quantiles: Tuple[float, ...]


@dataclass(frozen=True)
class UnitGeometryIssue:
    source_file_id: int
    reason: str
    candidates: Tuple[int, ...] = ()


@dataclass
class UnitGeometryRemap:
    remap: Dict[int, int] = field(default_factory=dict)
    match_levels: Dict[int, str] = field(default_factory=dict)
    scores: Dict[int, float] = field(default_factory=dict)
    margins: Dict[int, float] = field(default_factory=dict)
    rankings: Dict[int, Tuple[Tuple[int, float], ...]] = field(default_factory=dict)
    missing: List[UnitGeometryIssue] = field(default_factory=list)
    ambiguous: List[UnitGeometryIssue] = field(default_factory=list)
    extra_unit_file_ids: List[int] = field(default_factory=list)
    claimed_target_file_ids: set[int] = field(default_factory=set)

    def is_complete(self) -> bool:
        """Return True when every patch-used source Unit has one geometry match."""
        return not self.missing and not self.ambiguous


@dataclass(frozen=True)
class StreamLayout:
    num_vertices: int
    vertex_stride: int
    vertex_offset: int
    position_offset: int
    position_format: int


@dataclass(frozen=True)
class MeshSection:
    vertex_offset: int
    num_vertices: int


@dataclass(frozen=True)
class MeshLayout:
    transform_index: int
    lod_index: int
    stream_index: int
    sections: Tuple[MeshSection, ...]


def build_unit_geometry_remap(
    patch: StreamToc,
    source: StreamToc,
    target: StreamToc,
    settings: Optional[GeometryMatchSettings] = None,
) -> UnitGeometryRemap:
    """Build Unit FileID remaps by comparing parsed vertex distributions."""
    active_settings = settings or GeometryMatchSettings()
    patch_unit_ids = _patch_source_unit_ids(patch, source)
    source_signatures = build_archive_signatures(source, active_settings)
    target_signatures = build_archive_signatures(target, active_settings)
    result = UnitGeometryRemap()
    _record_missing_patch_units(result, patch_unit_ids, source_signatures, target_signatures)
    _assign_geometry_matches(result, source_signatures, target_signatures, patch_unit_ids, active_settings)
    result.extra_unit_file_ids = _unmatched_target_ids(target, result)
    return result


def build_archive_signatures(
    toc: StreamToc,
    settings: GeometryMatchSettings,
) -> Dict[int, UnitGeometrySignature]:
    """Return parseable Unit geometry signatures indexed by FileID."""
    signatures: Dict[int, UnitGeometrySignature] = {}
    for entry in toc.by_type().get(UnitID, []):
        signature = build_unit_signature(entry, settings)
        if signature is not None:
            signatures[entry.file_id] = signature
    return signatures


def build_unit_signature(
    entry: TocEntry,
    settings: Optional[GeometryMatchSettings] = None,
) -> Optional[UnitGeometrySignature]:
    """Parse one direct Unit mesh into a distribution signature."""
    active_settings = settings or GeometryMatchSettings()
    points = parse_unit_points(entry)
    if not points:
        return None
    sample_points = tuple(_downsample_points(points, active_settings.sample_count))
    center, extents, diagonal = _bounding_box_stats(points)
    axis_quantiles = _axis_quantiles(points, active_settings.quantiles)
    radial_quantiles = _radial_quantiles(points, center, active_settings.quantiles)
    return UnitGeometrySignature(
        entry.file_id,
        tuple(points),
        sample_points,
        len(points),
        center,
        extents,
        diagonal,
        axis_quantiles,
        radial_quantiles,
    )


def parse_unit_points(entry: TocEntry) -> List[Point3]:
    """Read direct Unit mesh position vertices from toc/gpu bytes."""
    stream_layouts = _read_stream_layouts(entry.toc_data)
    mesh_layouts = _select_primary_meshes(_read_mesh_layouts(entry.toc_data))
    transforms = _read_transform_matrices(entry.toc_data)
    points: List[Point3] = []
    for mesh in mesh_layouts:
        points.extend(_mesh_points(entry.gpu_data, stream_layouts, transforms, mesh))
    return points


def format_unit_geometry_issues(result: UnitGeometryRemap, limit: int = 6) -> str:
    """Format missing/ambiguous Unit geometry matches for diagnostics."""
    issues = result.missing + result.ambiguous
    parts: List[str] = []
    for issue in issues[:limit]:
        suffix = f", candidates={list(issue.candidates)}" if issue.candidates else ""
        parts.append(f"{issue.source_file_id}: {issue.reason}{suffix}")
    if len(issues) > limit:
        parts.append(f"... {len(issues) - limit} more")
    return "; ".join(parts)


def score_signatures(
    source: UnitGeometrySignature,
    target: UnitGeometrySignature,
) -> float:
    """Return a normalized geometric distance; lower means more similar."""
    scale = max(source.diagonal, target.diagonal, 0.000001)
    cloud_score = _symmetric_cloud_distance(source, target) / scale
    quantile_score = _tuple_distance(source.axis_quantiles, target.axis_quantiles) / scale
    radial_score = _tuple_distance(source.radial_quantiles, target.radial_quantiles) / scale
    bbox_score = _bbox_score(source, target, scale)
    count_score = abs(math.log((source.vertex_count + 1) / (target.vertex_count + 1)))
    return 0.55 * cloud_score + 0.20 * quantile_score + 0.10 * radial_score + 0.10 * bbox_score + 0.05 * count_score


def _patch_source_unit_ids(patch: StreamToc, source: StreamToc) -> set[int]:
    source_unit_ids = {entry.file_id for entry in source.by_type().get(UnitID, [])}
    return {
        entry.file_id
        for entry in patch.by_type().get(UnitID, [])
        if entry.file_id in source_unit_ids
    }


def _record_missing_patch_units(
    result: UnitGeometryRemap,
    patch_unit_ids: set[int],
    source_signatures: Dict[int, UnitGeometrySignature],
    target_signatures: Dict[int, UnitGeometrySignature],
) -> None:
    if target_signatures:
        missing_ids = patch_unit_ids - set(source_signatures)
        reason = "source Unit has no parseable direct geometry"
    else:
        missing_ids = set(patch_unit_ids)
        reason = "target archive has no parseable direct Unit geometry"
    for file_id in sorted(missing_ids):
        result.missing.append(UnitGeometryIssue(file_id, reason))


def _assign_geometry_matches(
    result: UnitGeometryRemap,
    source_signatures: Dict[int, UnitGeometrySignature],
    target_signatures: Dict[int, UnitGeometrySignature],
    patch_unit_ids: set[int],
    settings: GeometryMatchSettings,
) -> None:
    rankings = _rank_all_sources(source_signatures, target_signatures)
    _record_patch_rankings(result, rankings, patch_unit_ids, settings)
    taken_targets: set[int] = set()
    for source_id in _assignment_order(rankings, patch_unit_ids):
        if _is_blocked_patch_source(result, source_id, patch_unit_ids):
            continue
        _assign_first_available(result, rankings[source_id], source_id, taken_targets, patch_unit_ids, settings)


def _rank_all_sources(
    source_signatures: Dict[int, UnitGeometrySignature],
    target_signatures: Dict[int, UnitGeometrySignature],
) -> Dict[int, List[Tuple[int, float]]]:
    rankings: Dict[int, List[Tuple[int, float]]] = {}
    for source_id, signature in source_signatures.items():
        ranked = [
            (target_id, score_signatures(signature, target_signature))
            for target_id, target_signature in target_signatures.items()
        ]
        rankings[source_id] = sorted(ranked, key=lambda item: item[1])
    return rankings


def _record_patch_rankings(
    result: UnitGeometryRemap,
    rankings: Dict[int, List[Tuple[int, float]]],
    patch_unit_ids: set[int],
    settings: GeometryMatchSettings,
) -> None:
    for source_id in sorted(patch_unit_ids & set(rankings)):
        ranked = rankings[source_id]
        result.rankings[source_id] = tuple(ranked[:3])
        issue = _ranking_issue(source_id, ranked, settings)
        if issue is None:
            continue
        target_list = tuple(target_id for target_id, _score in ranked[:3])
        if issue == "ambiguous geometry match":
            result.ambiguous.append(UnitGeometryIssue(source_id, issue, target_list))
        else:
            result.missing.append(UnitGeometryIssue(source_id, issue, target_list))


def _ranking_issue(
    source_id: int,
    ranked: List[Tuple[int, float]],
    settings: GeometryMatchSettings,
) -> Optional[str]:
    if not ranked:
        return "no target Unit geometry candidates"
    if ranked[0][1] > settings.max_score:
        return "best geometry match exceeds score threshold"
    if len(ranked) > 1 and ranked[1][1] - ranked[0][1] < settings.min_margin:
        return "ambiguous geometry match"
    return None


def _assignment_order(
    rankings: Dict[int, List[Tuple[int, float]]],
    patch_unit_ids: set[int],
) -> List[int]:
    return sorted(
        rankings,
        key=lambda source_id: (
            0 if source_id in patch_unit_ids else 1,
            rankings[source_id][0][1] if rankings[source_id] else float("inf"),
            source_id,
        ),
    )


def _is_blocked_patch_source(
    result: UnitGeometryRemap,
    source_id: int,
    patch_unit_ids: set[int],
) -> bool:
    if source_id not in patch_unit_ids:
        return False
    blocked = _issue_file_ids(result.missing) | _issue_file_ids(result.ambiguous)
    return source_id in blocked


def _assign_first_available(
    result: UnitGeometryRemap,
    ranked: List[Tuple[int, float]],
    source_id: int,
    taken_targets: set[int],
    patch_unit_ids: set[int],
    settings: GeometryMatchSettings,
) -> None:
    for target_id, score in ranked:
        if target_id in taken_targets or score > settings.max_score:
            continue
        taken_targets.add(target_id)
        result.claimed_target_file_ids.add(target_id)
        if source_id in patch_unit_ids:
            result.remap[source_id] = target_id
            result.match_levels[source_id] = "geometry"
            result.scores[source_id] = score
            result.margins[source_id] = _margin_for_target(ranked, target_id)
        return
    if source_id in patch_unit_ids:
        result.missing.append(UnitGeometryIssue(source_id, "no unclaimed target Unit geometry candidate"))


def _margin_for_target(ranked: List[Tuple[int, float]], target_id: int) -> float:
    chosen = next(score for candidate_id, score in ranked if candidate_id == target_id)
    alternatives = [score for candidate_id, score in ranked if candidate_id != target_id]
    return 1.0 if not alternatives else alternatives[0] - chosen


def _unmatched_target_ids(target: StreamToc, result: UnitGeometryRemap) -> List[int]:
    target_ids = [entry.file_id for entry in target.by_type().get(UnitID, [])]
    return [file_id for file_id in target_ids if file_id not in result.claimed_target_file_ids]


def _issue_file_ids(issues: Iterable[UnitGeometryIssue]) -> set[int]:
    return {issue.source_file_id for issue in issues}


def _read_stream_layouts(toc_data: bytes) -> List[StreamLayout]:
    stream_off = _read_u32(toc_data, 0x5C)
    if stream_off == 0 or stream_off + 4 > len(toc_data):
        return []
    num_streams = _read_u32(toc_data, stream_off)
    bases = _offset_table_bases(toc_data, stream_off, num_streams)
    return [_read_stream_layout(toc_data, base) for base in bases]


def _read_stream_layout(toc_data: bytes, base: int) -> StreamLayout:
    num_components = _read_u64(toc_data, base + 328)
    vertex_count = _read_u32(toc_data, base + 352)
    vertex_stride = _read_u32(toc_data, base + 356)
    vertex_offset = _read_u32(toc_data, base + 416)
    position = _position_component(toc_data, base + 8, int(num_components), _read_u32(toc_data, 0x2C))
    if position is None:
        return StreamLayout(vertex_count, vertex_stride, vertex_offset, -1, -1)
    return StreamLayout(vertex_count, vertex_stride, vertex_offset, position[0], position[1])


def _position_component(
    toc_data: bytes,
    offset: int,
    num_components: int,
    version: int,
) -> Optional[Tuple[int, int]]:
    cursor = 0
    for index in range(num_components):
        component_at = offset + 20 * index
        if component_at + 20 > len(toc_data):
            return None
        component_type, component_format = struct.unpack_from("<II", toc_data, component_at)
        if component_type == 0:
            return cursor, component_format
        component_size = _component_size(version, component_format)
        if component_size == 0:
            return None
        cursor += component_size
    return None


def _read_mesh_layouts(toc_data: bytes) -> List[MeshLayout]:
    mesh_off = _read_u32(toc_data, 0x64)
    if mesh_off == 0 or mesh_off + 4 > len(toc_data):
        return []
    num_meshes = _read_u32(toc_data, mesh_off)
    bases = _offset_table_bases(toc_data, mesh_off, num_meshes)
    return [_read_mesh_layout(toc_data, base) for base in bases]


def _read_mesh_layout(toc_data: bytes, base: int) -> MeshLayout:
    transform_index = _read_u32(toc_data, base + 48)
    lod_index = _read_i32(toc_data, base + 56)
    stream_index = _read_u32(toc_data, base + 60)
    num_sections = _read_u32(toc_data, base + 120)
    section_offset = _read_u32(toc_data, base + 124)
    sections = _read_sections(toc_data, base + section_offset, num_sections)
    return MeshLayout(transform_index, lod_index, stream_index, tuple(sections))


def _read_sections(toc_data: bytes, start: int, count: int) -> List[MeshSection]:
    sections: List[MeshSection] = []
    for index in range(count):
        section_at = start + 24 * index
        if section_at + 24 > len(toc_data):
            continue
        vertex_offset = _read_u32(toc_data, section_at + 4)
        num_vertices = _read_u32(toc_data, section_at + 8)
        sections.append(MeshSection(vertex_offset, num_vertices))
    return sections


def _select_primary_meshes(meshes: List[MeshLayout]) -> List[MeshLayout]:
    lod_zero = [mesh for mesh in meshes if mesh.lod_index == 0]
    if lod_zero:
        return lod_zero
    non_negative = [mesh for mesh in meshes if mesh.lod_index >= 0]
    if non_negative:
        best_lod = min(mesh.lod_index for mesh in non_negative)
        return [mesh for mesh in non_negative if mesh.lod_index == best_lod]
    return meshes


def _read_transform_matrices(toc_data: bytes) -> List[Matrix4]:
    transform_off = _read_u32(toc_data, 0x34)
    if transform_off == 0 or transform_off + 16 > len(toc_data):
        return []
    count = _read_u32(toc_data, transform_off)
    matrices_at = transform_off + 16 + 48 * count
    return [_read_matrix(toc_data, matrices_at + 64 * index) for index in range(count)]


def _read_matrix(toc_data: bytes, offset: int) -> Matrix4:
    if offset + 64 > len(toc_data):
        return _identity_matrix()
    return struct.unpack_from("<16f", toc_data, offset)


def _mesh_points(
    gpu_data: bytes,
    stream_layouts: List[StreamLayout],
    transforms: List[Matrix4],
    mesh: MeshLayout,
) -> List[Point3]:
    if mesh.stream_index >= len(stream_layouts):
        return []
    stream = stream_layouts[mesh.stream_index]
    if stream.position_offset < 0 or stream.vertex_stride <= 0:
        return []
    matrix = _matrix_for_mesh(transforms, mesh.transform_index)
    points: List[Point3] = []
    for section in mesh.sections:
        points.extend(_section_points(gpu_data, stream, matrix, section))
    return points


def _section_points(
    gpu_data: bytes,
    stream: StreamLayout,
    matrix: Matrix4,
    section: MeshSection,
) -> List[Point3]:
    points: List[Point3] = []
    end = min(section.vertex_offset + section.num_vertices, stream.num_vertices)
    for vertex_index in range(section.vertex_offset, end):
        point = _read_vertex_position(gpu_data, stream, vertex_index)
        if point is not None:
            points.append(_transform_point(point, matrix))
    return points


def _read_vertex_position(
    gpu_data: bytes,
    stream: StreamLayout,
    vertex_index: int,
) -> Optional[Point3]:
    offset = stream.vertex_offset + vertex_index * stream.vertex_stride + stream.position_offset
    if offset < 0 or offset + _component_size(0, stream.position_format) > len(gpu_data):
        return None
    return _decode_position(gpu_data, offset, stream.position_format)


def _decode_position(gpu_data: bytes, offset: int, position_format: int) -> Optional[Point3]:
    if position_format == 0:
        return (struct.unpack_from("<f", gpu_data, offset)[0], 0.0, 0.0)
    if position_format == 1:
        x, y = struct.unpack_from("<2f", gpu_data, offset)
        return (x, y, 0.0)
    if position_format == 2:
        return struct.unpack_from("<3f", gpu_data, offset)
    if position_format == 3:
        x, y, z, _w = struct.unpack_from("<4f", gpu_data, offset)
        return (x, y, z)
    if position_format == 33:
        x, y = struct.unpack_from("<2e", gpu_data, offset)
        return (float(x), float(y), 0.0)
    if position_format == 35:
        x, y, z, _w = struct.unpack_from("<4e", gpu_data, offset)
        return (float(x), float(y), float(z))
    return None


def _component_size(version: int, component_format: int) -> int:
    if version == 10800437:
        sizes = {0: 4, 1: 8, 2: 12, 3: 16, 4: 4, 20: 16, 24: 4, 25: 4, 26: 4, 29: 4, 31: 8}
        return sizes.get(component_format, 0)
    sizes = {0: 4, 1: 8, 2: 12, 3: 16, 4: 4, 24: 16, 28: 4, 29: 4, 30: 4, 33: 4, 35: 8}
    return sizes.get(component_format, 0)


def _matrix_for_mesh(transforms: List[Matrix4], transform_index: int) -> Matrix4:
    if transform_index < len(transforms):
        return transforms[transform_index]
    return _identity_matrix()


def _transform_point(point: Point3, matrix: Matrix4) -> Point3:
    x, y, z = point
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )


def _identity_matrix() -> Matrix4:
    return (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _offset_table_bases(toc_data: bytes, table_off: int, count: int) -> List[int]:
    bases: List[int] = []
    offsets_at = table_off + 4
    for index in range(count):
        offset_at = offsets_at + 4 * index
        if offset_at + 4 > len(toc_data):
            continue
        relative = _read_u32(toc_data, offset_at)
        base = table_off + relative
        if 0 <= base < len(toc_data):
            bases.append(base)
    return bases


def _bounding_box_stats(points: List[Point3]) -> Tuple[Point3, Point3, float]:
    mins = tuple(min(point[axis] for point in points) for axis in range(3))
    maxs = tuple(max(point[axis] for point in points) for axis in range(3))
    center = tuple((mins[axis] + maxs[axis]) / 2.0 for axis in range(3))
    extents = tuple(maxs[axis] - mins[axis] for axis in range(3))
    return center, extents, _vector_length(extents)


def _axis_quantiles(points: List[Point3], quantiles: Tuple[float, ...]) -> Tuple[float, ...]:
    values: List[float] = []
    for axis in range(3):
        ordered = sorted(point[axis] for point in points)
        values.extend(_quantile_values(ordered, quantiles))
    return tuple(values)


def _radial_quantiles(
    points: List[Point3],
    center: Point3,
    quantiles: Tuple[float, ...],
) -> Tuple[float, ...]:
    ordered = sorted(_vector_distance(point, center) for point in points)
    return tuple(_quantile_values(ordered, quantiles))


def _quantile_values(ordered: List[float], quantiles: Tuple[float, ...]) -> List[float]:
    if not ordered:
        return [0.0 for _ in quantiles]
    last = len(ordered) - 1
    return [ordered[round(last * quantile)] for quantile in quantiles]


def _downsample_points(points: List[Point3], sample_count: int) -> List[Point3]:
    if len(points) <= sample_count:
        return list(points)
    last = len(points) - 1
    return [points[round(last * index / (sample_count - 1))] for index in range(sample_count)]


def _symmetric_cloud_distance(
    source: UnitGeometrySignature,
    target: UnitGeometrySignature,
) -> float:
    left = _mean_nearest_distance(source.sample_points, target.sample_points)
    right = _mean_nearest_distance(target.sample_points, source.sample_points)
    return (left + right) / 2.0


def _mean_nearest_distance(points: Tuple[Point3, ...], candidates: Tuple[Point3, ...]) -> float:
    if not points or not candidates:
        return float("inf")
    return sum(_nearest_distance(point, candidates) for point in points) / len(points)


def _nearest_distance(point: Point3, candidates: Tuple[Point3, ...]) -> float:
    return min(_vector_distance(point, candidate) for candidate in candidates)


def _bbox_score(source: UnitGeometrySignature, target: UnitGeometrySignature, scale: float) -> float:
    center_score = _vector_distance(source.center, target.center) / scale
    extent_score = _vector_distance(source.extents, target.extents) / scale
    return (center_score + extent_score) / 2.0


def _tuple_distance(left: Tuple[float, ...], right: Tuple[float, ...]) -> float:
    if not left or not right:
        return 0.0
    count = min(len(left), len(right))
    return sum(abs(left[index] - right[index]) for index in range(count)) / count


def _vector_distance(left: Point3, right: Point3) -> float:
    return _vector_length(tuple(left[axis] - right[axis] for axis in range(3)))


def _vector_length(value: Tuple[float, ...]) -> float:
    return math.sqrt(sum(component * component for component in value))


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, offset)[0]


def _read_i32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        return 0
    return struct.unpack_from("<i", data, offset)[0]


def _read_u64(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]
