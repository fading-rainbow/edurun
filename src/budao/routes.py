"""使用 Shapely/NetworkX/GeographicLib，不自行实现 GIS 或寻路算法。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import networkx as nx
from geographiclib.geodesic import Geodesic
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .models import GeoPoint, RouteConfig


def distance(a: GeoPoint, b: GeoPoint) -> float:
    return float(Geodesic.WGS84.Inverse(a.lat, a.lon, b.lat, b.lon)["s12"])


def path_length(points: tuple[GeoPoint, ...] | list[GeoPoint]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:], strict=False))


def interpolate(a: GeoPoint, b: GeoPoint, metres: float) -> GeoPoint:
    inverse = Geodesic.WGS84.Inverse(a.lat, a.lon, b.lat, b.lon)
    length = inverse["s12"]
    if length == 0:
        return a
    forward = Geodesic.WGS84.Direct(a.lat, a.lon, inverse["azi1"], min(length, max(0, metres)))
    return GeoPoint(lon=forward["lon2"], lat=forward["lat2"])


def import_track(path: Path) -> tuple[GeoPoint, ...]:
    """KML/GPX 按规范读取 WGS84；不私自推断或修正 GCJ02。"""
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("路线文件超过2MiB")
    data = path.read_text("utf-8-sig")
    if "<!DOCTYPE" in data.upper() or "<!ENTITY" in data.upper():
        raise ValueError("路线 XML 不允许 DTD/实体")
    points: list[GeoPoint] = []
    suffix = path.suffix.lower()
    if suffix in {".kml", ".gpx"}:
        root = ET.fromstring(data)
        if suffix == ".kml":
            groups = list(root.findall(".//{*}coordinates"))
            if len(groups) != 1:
                raise ValueError("请提供单条 KML 路线，不能拼接不相连的多个轨迹")
            for item in (groups[0].text or "").split():
                values = item.split(",")
                points.append(GeoPoint(lon=float(values[0]), lat=float(values[1])))
        else:
            segments = list(root.findall(".//{*}trkseg"))
            if len(segments) > 1:
                raise ValueError("GPX 多段轨迹必须先明确连接方式")
            elements = root.findall(".//{*}trkpt") or root.findall(".//{*}rtept")
            points = [
                GeoPoint(lon=float(p.attrib["lon"]), lat=float(p.attrib["lat"])) for p in elements
            ]
    elif suffix in {".json", ".geojson"}:
        item = json.loads(data)
        geometry = item.get("geometry", item)
        if geometry.get("type") != "LineString":
            raise ValueError("GeoJSON 路线必须为 LineString")
        if item.get("crs") or item.get("coordinate_system", "WGS84") != "WGS84":
            raise ValueError("仅接收明确的 WGS84；GCJ02 请先校准并转换")
        points = [GeoPoint(lon=p[0], lat=p[1]) for p in geometry["coordinates"]]
    else:
        raise ValueError("支持 KML、GPX、GeoJSON")
    if len(points) < 2 or path_length(points) <= 0:
        raise ValueError("轨迹必须有两个以上有效点且长度大于0")
    return tuple(points)


class RoutePlanner:
    def __init__(self, config: RouteConfig):
        self.config = config
        origin = config.region[0]
        local = CRS.from_proj4(
            f"+proj=aeqd +lat_0={origin.lat} +lon_0={origin.lon} +datum=WGS84 +units=m"
        )
        self.forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
        self.backward = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
        self.region = Polygon([self.xy(p) for p in config.region])
        if not self.region.is_valid or self.region.area <= 0:
            raise ValueError("区域多边形自交或面积为0")
        if self.region.area > 100_000_000:
            raise ValueError("区域过大；本工具按校园小范围标定")
        lines = [LineString([self.xy(p) for p in line]) for line in config.corridors]
        if any(line.length <= 0 or not self.region.covers(line) for line in lines):
            raise ValueError("存在零长或越界通道")
        merged = unary_union(lines)
        segments = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
        self.graph = nx.Graph()
        for line in segments:
            coordinates = list(line.coords)
            for a, b in zip(coordinates, coordinates[1:], strict=False):
                a, b = self.node(a), self.node(b)
                if a != b:
                    self.graph.add_edge(a, b, weight=Point(a).distance(Point(b)))
        if self.graph.number_of_edges() == 0:
            raise ValueError("没有有效通道边")
        self.validate_path(config.baseline)
        roads = unary_union(lines)
        self.roads = roads
        baseline = LineString([self.xy(p) for p in config.baseline])
        if not roads.buffer(0.05).covers(baseline):
            raise ValueError("基线路线不沿可跑通道")

    @staticmethod
    def node(xy) -> tuple[float, float]:
        return round(xy[0], 6), round(xy[1], 6)

    def xy(self, point: GeoPoint) -> tuple[float, float]:
        return self.forward.transform(point.lon, point.lat)

    def geo(self, xy) -> GeoPoint:
        lon, lat = self.backward.transform(*xy)
        return GeoPoint(lon=lon, lat=lat)

    def contains(self, point: GeoPoint) -> bool:
        return self.region.covers(Point(self.xy(point)))

    def validate_path(self, points: tuple[GeoPoint, ...] | list[GeoPoint]) -> None:
        if len(points) < 2 or not self.region.covers(LineString([self.xy(p) for p in points])):
            raise ValueError("路径越界或不足两个点")

    def _insert(self, graph: nx.Graph, point: GeoPoint) -> tuple[float, float]:
        position = Point(self.xy(point))
        if not self.region.covers(position):
            raise ValueError("当前位置或目标点不在允许区域")
        edges = list(graph.edges())
        a, b = min(edges, key=lambda edge: LineString(edge).distance(position))
        line = LineString([a, b])
        closest = line.interpolate(line.project(position))
        if position.distance(closest) > self.config.snap_tolerance_m:
            raise ValueError("目标离已知通道过远，不猜测穿越建筑的路径")
        snapped = self.node(closest.coords[0])
        exact = self.node(position.coords[0])
        if snapped not in {a, b}:
            graph.remove_edge(a, b)
            graph.add_edge(a, snapped, weight=Point(a).distance(Point(snapped)))
            graph.add_edge(snapped, b, weight=Point(snapped).distance(Point(b)))
        if exact != snapped:
            connector = LineString([exact, snapped])
            if not self.region.covers(connector):
                raise ValueError("连接通道时越界")
            graph.add_edge(exact, snapped, weight=position.distance(closest))
        return exact

    def plan(self, start: GeoPoint, target: GeoPoint) -> tuple[GeoPoint, ...]:
        graph = self.graph.copy()
        source = self._insert(graph, start)
        destination = self._insert(graph, target)
        try:
            nodes = nx.shortest_path(graph, source, destination, weight="weight")
        except nx.NetworkXNoPath as error:
            raise ValueError("目标与当前位置不连通") from error
        points = tuple(self.geo(n) for n in nodes)
        if len(points) == 1:
            points = (start, target)
        self.validate_path(points)
        return points
