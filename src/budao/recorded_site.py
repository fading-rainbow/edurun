"""从历史实际输入证据装配连通路线草案。不是校园道路/官方边界或完整点目录认证。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import networkx as nx
from shapely.geometry import Point

from .models import GeoPoint, RouteConfig
from .profiles import CheckpointCatalog, RunProfile, preflight_profile, private_file
from .routes import RoutePlanner, distance, path_length

TRACE_NAMES = (
    "mumu-formal-cp1-approach",
    "mumu-formal-cp1-remaining",
    "mumu-formal-cp2-approach",
    "mumu-formal-cp2-assigned",
    "mumu-formal-cp2-range",
    "mumu-formal-cp3-approach",
    "mumu-formal-cp3-assigned",
    "mumu-formal-cp3-confirmed",
    "mumu-formal-finish-ready",
    "mumu-formal-range-segment",
)


def command_trace(path: Path) -> tuple[GeoPoint, ...]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        value = json.loads(path.read_text("utf-8-sig"))
        observations = value["observations"]
        if value["record_submitted"] is not False or not 2 <= len(observations) <= 4096:
            raise ValueError
        points = []
        previous_time = -1
        previous_elapsed = -1
        for observation in observations:
            sampled, app = observation["t"], observation["app"]
            if (
                type(sampled) not in (int, float)
                or not previous_time < sampled < 1800
                or app["counter_label"] != "已打卡"
                or type(app["elapsed_s"]) is not int
                or app["elapsed_s"] < previous_elapsed
            ):
                raise ValueError
            previous_time, previous_elapsed = sampled, app["elapsed_s"]
            point = GeoPoint.model_validate(observation["prior_wgs84"])
            if points and distance(points[-1], point) > 15:
                raise ValueError
            if not points or points[-1] != point:
                points.append(point)
        last = GeoPoint.model_validate(value["last_wgs84"])
        if points[-1] != last:
            if distance(points[-1], last) > 15:
                raise ValueError
            points.append(last)
        if len(points) < 2 or path_length(points) <= 0:
            raise ValueError
        return tuple(points)
    except (ValueError, KeyError, TypeError, IndexError, OSError):
        raise ValueError("历史输入证据不完整或有间断；不回显坐标/日志，不推测连接道路") from None


def draft_network(traces: tuple[tuple[GeoPoint, ...], ...]) -> RouteConfig:
    if not traces or any(len(t) < 2 for t in traces):
        raise ValueError("至少需要一条实际输入轨迹")
    points = [point for trace in traces for point in trace]
    minx, maxx = min(p.lon for p in points) - 0.0003, max(p.lon for p in points) + 0.0003
    miny, maxy = min(p.lat for p in points) - 0.0003, max(p.lat for p in points) + 0.0003
    # 仅草案的几何包络，不声明这就是官方校方跑区。
    region = tuple(
        GeoPoint(lon=x, lat=y)
        for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny))
    )
    seed = RouteConfig(region=region, corridors=traces, baseline=(traces[0][0], traces[0][1]))
    planner = RoutePlanner(seed)
    if not nx.is_connected(planner.graph):
        raise ValueError("实际轨迹图不连通，不添加猜测直线桥接")
    start = min(planner.graph.nodes)
    lengths, paths = nx.single_source_dijkstra(planner.graph, start, weight="weight")
    end = max(lengths, key=lengths.get)
    forward = tuple(planner.geo(p) for p in paths[end])
    # 沿已存在图反向回跑，不能把非闭合KML终点直接跳回起点。
    baseline = forward + tuple(reversed(forward[:-1]))
    route = seed.model_copy(update={"baseline": baseline})
    RoutePlanner(route)
    if distance(baseline[0], baseline[-1]) > 0.05:
        raise ValueError("生成的基线不连续")
    return route


def prepare_recorded_site(root: Path) -> dict:
    output = root / ".local/profiles/current"
    profile_file = private_file(root, output / "profile.json", must_exist=False)
    route_file = private_file(root, output / "command-roads.draft.json", must_exist=False)
    manifest_file = private_file(root, output / "command-roads.provenance.json", must_exist=False)
    if any(p.exists() for p in (profile_file, route_file, manifest_file)):
        raise ValueError("场地草案或配置已有内容，保留原文件，不覆盖")
    sources = [
        private_file(root, Path(".local/runs/acceptance") / (n + ".json")) for n in TRACE_NAMES
    ]
    route = draft_network(tuple(command_trace(path) for path in sources))
    catalog_file = private_file(root, Path(".local/profiles/current/catalog.partial.json"))
    catalog = CheckpointCatalog.model_validate_json(catalog_file.read_text("utf-8-sig"))
    planner = RoutePlanner(route)
    for entry in catalog.entries:
        planner.plan(route.baseline[0], entry.approach)
        if planner.roads.distance(Point(planner.xy(entry.approach))) > 0.05:
            raise ValueError("确认位置不在历史实际输入轨迹上，不拉直线补道路")
    profile = RunProfile(
        profile_id="current",
        site_id=catalog.site_id,
        receipt_file=Path(".local/setup/mumu-owned-instance.json"),
        route_file=route_file.relative_to(root),
        catalog_file=catalog_file.relative_to(root),
        operator_roads_verified=False,
    )
    # 所有声明保持未核实，不把历史实际位置输入等同于地面道路/全部点位确认。
    manifest = {
        "source_kind": "historical_actual_commands_and_app_observations",
        "source_sha256": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "region_basis": "expanded recorded-input envelope, not official school boundary",
        "official_region_verified": False,
        "physical_roads_verified": False,
        "catalog_complete": False,
        "ready_for_unattended_run": False,
        "route_length_m": path_length(route.baseline),
        "baseline_closed": True,
        "invented_road_connections": False,
        "device_connected": False,
        "source_position_is_command_not_receiver_field": True,
    }
    output.mkdir(parents=True, exist_ok=True)
    for path, data in (
        (route_file, route.model_dump_json(indent=2)),
        (profile_file, profile.model_dump_json(indent=2)),
        (manifest_file, json.dumps(manifest, ensure_ascii=False, indent=2)),
    ):
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data + "\n")
    report = preflight_profile(profile_file, root)
    return {
        **report,
        "profile_created": True,
        "baseline_length_m": path_length(route.baseline),
        "official_region_verified": False,
        "historical_commands_not_physical_road_survey": True,
    }
