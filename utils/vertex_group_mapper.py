from dataclasses import dataclass, field
from math import sqrt
from uuid import uuid4

import mathutils


@dataclass(frozen=True)
class MatchSettings:
    min_vertex_weight: float = 0.001
    min_vertex_count: int = 3
    max_score: float = 0.30
    diagnostic_top_n: int = 3


@dataclass(frozen=True)
class GroupSample:
    name: str
    index: int
    center: tuple
    vertex_count: int


@dataclass(frozen=True)
class MatchDecision:
    source_name: str
    target_name: str
    score: float
    margin: float


@dataclass(frozen=True)
class Diagnostic:
    source_name: str
    rankings: list
    score: float
    margin: float
    status: str


@dataclass(frozen=True)
class RenameResult:
    renamed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    conflicts: int = 0
    reference_groups: int = 0
    diagnostics: list = field(default_factory=list)
    target_count: int = 0
    scale: float = 1.0


def rename_vertex_groups_by_position(target_object, reference_object):
    settings = MatchSettings()
    target_samples, empty_targets = build_group_sample_data(target_object, settings)
    reference_samples, _ = build_group_sample_data(reference_object, settings)
    scale = reference_cloud_scale(reference_samples)
    matches, skipped, diagnostics = match_group_samples(target_samples, reference_samples, settings, scale)
    rename_plan = build_rename_plan(target_object.vertex_groups, matches)
    apply_rename_plan(target_object.vertex_groups, rename_plan)
    return build_result(rename_plan, empty_targets + skipped, matches, reference_samples, diagnostics, len(target_samples), scale)


def build_group_sample_data(obj, settings):
    samples = []
    skipped = []
    for vertex_group in obj.vertex_groups:
        sample = build_group_sample(obj, vertex_group, settings)
        append_group_sample(samples, skipped, vertex_group.name, sample)
    return samples, skipped


def append_group_sample(samples, skipped, group_name, sample):
    if sample is None:
        skipped.append(group_name)
        return
    samples.append(sample)


def world_point(obj, point):
    return obj.matrix_world @ mathutils.Vector(point)


def build_group_sample(obj, vertex_group, settings):
    weighted_points = collect_weighted_points(obj, vertex_group, settings)
    if len(weighted_points) < settings.min_vertex_count:
        return None
    center = weighted_center(weighted_points)
    return GroupSample(vertex_group.name, vertex_group.index, center, len(weighted_points))


def collect_weighted_points(obj, vertex_group, settings):
    points = []
    for vertex in obj.data.vertices:
        weight = vertex_group_weight(vertex, vertex_group.index)
        if weight > settings.min_vertex_weight:
            points.append((world_point(obj, vertex.co), weight))
    return points


def vertex_group_weight(vertex, group_index):
    for group in vertex.groups:
        if group.group == group_index:
            return group.weight
    return 0.0


def weighted_center(weighted_points):
    total_weight = sum(weight for _point, weight in weighted_points)
    return tuple(weighted_axis_value(weighted_points, axis, total_weight) for axis in range(3))


def weighted_axis_value(weighted_points, axis, total_weight):
    return sum(point[axis] * weight for point, weight in weighted_points) / total_weight


def reference_cloud_scale(reference_samples):
    if len(reference_samples) < 2:
        return 1.0
    centers = [sample.center for sample in reference_samples]
    centroid = tuple(sum(center[axis] for center in centers) / len(centers) for axis in range(3))
    variance = sum(
        sum((center[axis] - centroid[axis]) ** 2 for axis in range(3))
        for center in centers
    ) / len(centers)
    return max(sqrt(variance), 0.000001)


def match_group_samples(target_samples, reference_samples, settings, scale):
    if not reference_samples:
        return [], [sample.name for sample in target_samples], []
    tentatives = build_tentatives(target_samples, reference_samples, settings, scale)
    reference_names = {sample.name for sample in reference_samples}
    return resolve_tentatives(tentatives, reference_names, settings)


def build_tentatives(target_samples, reference_samples, settings, scale):
    tentatives = []
    for target_sample in target_samples:
        ranked = ranked_reference_matches(target_sample, reference_samples, scale)
        top_rankings = [(ref.name, score) for ref, score in ranked[:settings.diagnostic_top_n]]
        tentatives.append((target_sample, ranked, top_rankings))
    return tentatives


def resolve_tentatives(tentatives, reference_names, settings):
    matches = []
    skipped = []
    diagnostics = []
    preserved, remaining = split_preserved(tentatives, reference_names)
    for target_sample, ranked, top_rankings in preserved:
        matches.append(MatchDecision(target_sample.name, target_sample.name, 0.0, 0.0))
        diagnostics.append(Diagnostic(target_sample.name, top_rankings, 0.0, 1.0, 'preserved'))
    for target_sample, ranked, top_rankings in remaining:
        decision, diagnostic = assign_nearest(target_sample, ranked, top_rankings, settings)
        diagnostics.append(diagnostic)
        if decision is None:
            skipped.append(target_sample.name)
            continue
        matches.append(decision)
    return matches, skipped, diagnostics


def split_preserved(tentatives, reference_names):
    preserved = []
    remaining = []
    for tentative in tentatives:
        target_sample, _ranked, _top = tentative
        if target_sample.name in reference_names:
            preserved.append(tentative)
        else:
            remaining.append(tentative)
    return preserved, remaining


def assign_nearest(target_sample, ranked, top_rankings, settings):
    if not ranked:
        return None, Diagnostic(target_sample.name, top_rankings, float('inf'), 0.0, 'no_reference')
    best_ref, best_score = ranked[0]
    margin = margin_of(ranked)
    status = classify_match(best_score, settings)
    diagnostic = Diagnostic(target_sample.name, top_rankings, best_score, margin, status)
    if status != 'matched':
        return None, diagnostic
    return MatchDecision(target_sample.name, best_ref.name, best_score, margin), diagnostic


def margin_of(ranked):
    if len(ranked) < 2:
        return 1.0
    return ranked[1][1] - ranked[0][1]


def classify_match(best_score, settings):
    if best_score > settings.max_score:
        return 'rejected_score'
    return 'matched'


def ranked_reference_matches(target_sample, reference_samples, scale):
    ranked = []
    for reference_sample in reference_samples:
        score = group_match_score(target_sample, reference_sample, scale)
        ranked.append((reference_sample, score))
    return sorted(ranked, key=lambda item: item[1])


def group_match_score(target_sample, reference_sample, scale):
    return vector_distance(target_sample.center, reference_sample.center) / scale


def vector_distance(left, right):
    return sqrt(sum((left[axis] - right[axis]) ** 2 for axis in range(3)))


def build_rename_plan(vertex_groups, matches):
    match_names = grouped_match_names(matches)
    existing_names = names_excluded_from_plan(vertex_groups, matches)
    return plan_group_renames(vertex_groups, match_names, existing_names)


def grouped_match_names(matches):
    names = {}
    for match in matches:
        names.setdefault(match.target_name, []).append(match.source_name)
    return names


def names_excluded_from_plan(vertex_groups, matches):
    matched_names = {match.source_name for match in matches}
    return {group.name for group in vertex_groups if group.name not in matched_names}


def plan_group_renames(vertex_groups, match_names, existing_names):
    plan = {}
    for target_name, source_names in match_names.items():
        ordered_sources = order_sources_for_target(source_names, target_name)
        planned_names = planned_names_for_target(target_name, ordered_sources)
        rename_pairs = zip(ordered_sources, planned_names)
        add_available_renames(plan, vertex_groups, rename_pairs, existing_names)
    return plan


def order_sources_for_target(source_names, target_name):
    return sorted(source_names, key=lambda name: (0 if name == target_name else 1, name))


def planned_names_for_target(target_name, source_names):
    if len(source_names) == 1:
        return [target_name]
    return [target_name] + [f"{target_name}.{index:03d}" for index in range(1, len(source_names))]


def add_available_renames(plan, vertex_groups, rename_pairs, existing_names):
    for source_name, planned_name in rename_pairs:
        if source_name == planned_name:
            continue
        group = vertex_groups.get(source_name)
        if group is not None and planned_name not in existing_names:
            plan[source_name] = planned_name
            existing_names.add(planned_name)


def apply_rename_plan(vertex_groups, rename_plan):
    temporary_plan = make_temporary_plan(rename_plan)
    rename_groups(vertex_groups, temporary_plan)
    rename_groups(vertex_groups, invert_plan(temporary_plan, rename_plan))


def make_temporary_plan(rename_plan):
    token = uuid4().hex
    return {source: f"__hd2_vg_tmp_{token}_{index}" for index, source in enumerate(rename_plan)}


def rename_groups(vertex_groups, rename_plan):
    for source_name, target_name in rename_plan.items():
        vertex_group = vertex_groups.get(source_name)
        if vertex_group is not None:
            vertex_group.name = target_name


def invert_plan(temporary_plan, final_plan):
    return {temporary: final_plan[source] for source, temporary in temporary_plan.items()}


def build_result(rename_plan, skipped, matches, reference_samples, diagnostics, target_count, scale):
    planned_sources = set(rename_plan.keys())
    failed_sources = [
        match.source_name for match in matches
        if match.source_name not in planned_sources and match.source_name != match.target_name
    ]
    all_skipped = sorted(set(skipped + failed_sources))
    return RenameResult(
        sorted(rename_plan.items()),
        all_skipped,
        count_conflicting_targets(matches),
        len(reference_samples),
        diagnostics,
        target_count,
        scale,
    )


def count_conflicting_targets(matches):
    target_counts = {}
    for match in matches:
        target_counts[match.target_name] = target_counts.get(match.target_name, 0) + 1
    return sum(1 for count in target_counts.values() if count > 1)
