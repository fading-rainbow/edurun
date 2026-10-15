"""Paused-map recovery: model pixels + own received trace + independent card range.

Experimental navigation candidates are NOT historical App confirmations or grades.
The fixed region, speed, distance and completion guards cannot be changed by the model.
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .checkpoint_vision import CheckpointAdvisor, CheckpointPixels
from .models import GeoPoint, PixelPoint, RouteConfig
from .routes import RoutePlanner, distance, path_length
from .sensor_probe import NORMAL, sensor_mode
from .ui_advisor import redacted_ui_image
from .vision import crop_image

CROP = (0, 366, 720, 932)


class TraceRegistration:
    """Four-parameter similarity, three independent trace/position anchors.

    Fit residual is reported as fit residual, not independent validation. The
    original App's current target distance is the separate range-validation gate.
    """

    def __init__(self, planner, pixels, geos):
        if len(pixels) != 3 or len(geos) != 3:
            raise ValueError("需要两个接收轨迹转角及当前接收定位")
        xy = np.asarray([planner.xy(p) for p in geos])
        if np.linalg.matrix_rank(np.column_stack([xy, np.ones(3)])) != 3:
            raise ValueError("轨迹与当前位置共线，不能标定")
        rows, rhs = [], []
        for p, (x, y) in zip(pixels, xy, strict=True):
            rows.extend([[p.x, p.y, 1, 0], [-p.y, p.x, 0, 1]])
            rhs.extend([x, y])
        self.coefficients = np.linalg.lstsq(rows, rhs, rcond=None)[0]
        self.planner = planner
        scale = math.hypot(*self.coefficients[:2])
        if not 0.05 <= scale <= 10:
            raise ValueError("地图比例超出有限校园视口")
        self.fit_residuals_m = [distance(self.map(p), g) for p, g in zip(pixels, geos, strict=True)]
        if max(self.fit_residuals_m) > 2:
            raise ValueError("轨迹像素拟合误差超过2米，不生成候选路线")

    def map(self, pixel):
        a, b, dx, dy = self.coefficients
        point = self.planner.geo((a * pixel.x + b * pixel.y + dx, -a * pixel.y + b * pixel.x + dy))
        if not self.planner.contains(point):
            raise ValueError("地图估计位置离开声明区域")
        return point

    def validate_range(self, target, current, actual_range_m):
        estimate = self.map(target)
        predicted = distance(estimate, current)
        if (
            not math.isfinite(actual_range_m)
            or not 0 < actual_range_m <= 500
            or abs(predicted - actual_range_m) > 3
        ):
            raise ValueError("原App独立距离与地图估计不一致，不导航")
        return estimate, predicted


def received_trace(path: Path, planner: RoutePlanner):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("接收轨迹日志超过有限窗口")
    points = []
    for line in path.read_text("utf-8-sig").splitlines():
        value = json.loads(line)
        gps, observed = value.get("gps_reception"), value.get("android_observed_ns")
        if not gps or not gps.get("registered") or not gps.get("has_accuracy"):
            continue
        if (
            type(observed) is not int
            or type(gps.get("last_received_ns")) is not int
            or not 0 <= observed - gps["last_received_ns"] <= 1_500_000_000
            or not 0 <= gps["accuracy_m"] <= 5
            or gps.get("provider") != "gps"
        ):
            continue
        point = GeoPoint(lon=gps["longitude"], lat=gps["latitude"])
        if not planner.contains(point):
            raise ValueError("实际接收位置越界")
        if not points or distance(points[-1], point) > 0.1:
            points.append(point)
    if len(points) < 8:
        raise ValueError("实际接收轨迹不足，不能标定未知点")
    simplified = list(LineString([planner.xy(p) for p in points]).simplify(1).coords)
    if len(simplified) < 4:
        raise ValueError("缺少两个实际接收转角")
    far, near, current = [planner.geo(p) for p in simplified[-3:]]
    if min(distance(far, near), distance(near, current)) < 10:
        raise ValueError("独立接收转角间距不足")
    return near, far, current


def refined_target_center(image, point):
    pixels = np.asarray(image.convert("RGB"))
    yy, xx = np.indices(pixels.shape[:2])
    radii = np.hypot(xx - point.x, yy - point.y)
    ring = (radii >= 13) & (radii <= 19)
    if np.mean(pixels[ring].max(axis=1) < 80) < 0.6:
        raise ValueError("当前标记不是已采集的黑圈白心，不使用像素")
    ys, xs = np.nonzero((pixels.min(axis=2) > 250) & (radii <= 12))
    if not 30 <= len(xs) <= 350:
        raise ValueError("目标白心像素不明确")
    return PixelPoint(x=float(xs.mean()), y=float(ys.mean()))


class MapRecovery:
    def __init__(self, root, config, connector, dispatcher, *, trace_sources=()):
        self.root, self.config = root, config
        self.connector, self.dispatcher = connector, dispatcher
        self.trace_sources = tuple(trace_sources)
        self.calls = 0

    def resolve(self, authorize):
        c, d = self.connector, self.dispatcher
        if authorize() is not True or self.calls >= 3 or d.worker.state.moving:
            raise ValueError("没有暂停许可或地图请求预算已用完")
        if sensor_mode(c.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
            raise ValueError("地图请求前输入模式尚未恢复正常")
        cap = c.capture()
        view = cap.frame.inspection
        assigned = [p for p in view.checkpoint_cards if p.state == "assigned"]
        if (
            view.run_control_state != "paused"
            or len(assigned) != 1
            or view.issues != ("start_controls_and_current_record_identity_unverified",)
        ):
            raise ValueError("不是唯一当前未打卡的暂停页，不发送图片")
        card = assigned[0]
        if card.name in d.observer.allowed_names:
            return
        source = None
        for path in (c.evidence / "input-observations.jsonl", *self.trace_sources):
            if path.exists():
                try:
                    near, far, actual = received_trace(path, d.planner)
                    source = path
                    break
                except ValueError:
                    continue
        if source is None:
            raise ValueError("没有本次实际接收轨迹的地图标定依据")
        # One map-only pinch. Does not start, move GPS, end or save the record.
        map_view = c.ui.device(resourceId="com.lptiyu.tanke:id/textureMapView")
        if map_view.count != 1 or authorize() is not True:
            raise ValueError("地图控件或许可未知")
        c.bridge.check_device()
        if card.distance_m > 80:
            map_view.gesture((100, 640), (620, 640), (260, 640), (460, 640), steps=40)
        cap = c.capture(pixels=True)
        if cap.frame.inspection != view:
            raise ValueError("缩放后原始记录或卡片改变")
        after = c.capture()
        if after.frame.inspection != view:
            raise ValueError("截图期间原记录或卡片改变，不发送")
        image = Image.open(
            io.BytesIO(
                redacted_ui_image(cap.screenshot, cap.hierarchy, after_hierarchy=after.hierarchy)
            )
        )
        png = crop_image(image, CROP)
        cropped = Image.open(io.BytesIO(png)).convert("RGB")
        enlarged = cropped.resize((1440, 1132))
        out = io.BytesIO()
        enlarged.save(out, format="PNG")
        self.calls += 1
        client = CheckpointAdvisor.from_private_config(self.root, self.config)
        try:
            advice = client.locate(out.getvalue())
        finally:
            client.close()
        data = advice.model_dump()
        for name in ("current", "target", "near_turn", "far_turn"):
            if data[name] is not None:
                data[name] = {k: v / 2 for k, v in data[name].items()}
        data["road_path"] = [{k: v / 2 for k, v in p.items()} for p in data["road_path"]]
        advice = CheckpointPixels.model_validate(data)
        if (
            advice.confidence < 0.9
            or any(
                p is None
                for p in (advice.current, advice.target, advice.near_turn, advice.far_turn)
            )
            or len(advice.road_path) < 2
        ):
            raise ValueError("地图像素置信度或路径不足")
        latest = c.capture(pixels=True)
        if authorize() is not True or latest.frame.inspection != view:
            raise ValueError("慢模型返回后原记录或许可改变")
        after = c.capture()
        if after.frame.inspection != view:
            raise ValueError("最后截图期间原记录或卡片改变")
        fresh_png = crop_image(
            Image.open(
                io.BytesIO(
                    redacted_ui_image(
                        latest.screenshot, latest.hierarchy, after_hierarchy=after.hierarchy
                    )
                )
            ),
            CROP,
        )
        fresh = np.asarray(Image.open(io.BytesIO(fresh_png)).convert("RGB"), dtype=float)
        if np.sqrt(np.mean((fresh - np.asarray(cropped, dtype=float)) ** 2)) > 3:
            raise ValueError("模型期间地图视口改变，标定失效")
        registration = TraceRegistration(
            d.planner, [advice.near_turn, advice.far_turn, advice.current], [near, far, actual]
        )
        target_pixel = refined_target_center(cropped, advice.target)
        target, predicted = registration.validate_range(target_pixel, actual, card.distance_m)
        path = [registration.map(p) for p in advice.road_path]
        if distance(path[0], actual) > 3 or distance(path[-1], target) > 2:
            raise ValueError("建议道路起止点不一致")
        # Translate the map's received coordinate space back to this worker's
        # command position. This is a measured local adapter offset, not a jump.
        ox, oy = d.planner.xy(d.worker.engine.position)
        gx, gy = d.planner.xy(actual)

        def shift(p):
            return d.planner.geo((d.planner.xy(p)[0] + ox - gx, d.planner.xy(p)[1] + oy - gy))

        path = [d.worker.engine.position] + [shift(p) for p in path[1:-1]] + [shift(target)]
        if path_length(path) > 600:
            raise ValueError("模型道路路径超出有界恢复范围")
        line = LineString([d.planner.xy(p) for p in path])
        if (
            not d.planner.region.covers(line)
            or line.difference(d.planner.roads.buffer(5)).length > 60
        ):
            raise ValueError("模型新增通道越界或超过60米实验上限")
        snap = d.planner.geo(nearest_points(d.planner.roads, Point(ox, oy))[0].coords[0])
        corridor = tuple(([snap] if distance(snap, path[0]) > 0.05 else []) + path)
        final_line = LineString([d.planner.xy(p) for p in corridor])
        if (
            not d.planner.region.covers(final_line)
            or final_line.difference(d.planner.roads.buffer(5)).length > 60
        ):
            raise ValueError("连接已声明道路的最终通道不合格")
        config = RouteConfig.model_validate(
            d.planner.config.model_dump() | {"corridors": d.planner.config.corridors + (corridor,)}
        )
        planner = RoutePlanner(config)
        planner.plan(d.worker.engine.position, path[-1])
        with d.worker.lock:
            d.planner = d.worker.engine.planner = planner
            d.route_key = None
        d.observer.navigation_candidates[card.name] = path[-1]
        proof = {
            "name": card.name,
            "app_confirmed": False,
            "trace_source": str(source),
            "fit_residuals_m": registration.fit_residuals_m,
            "actual_range_m": card.distance_m,
            "predicted_range_m": predicted,
            "candidate": path[-1].model_dump(),
            "road_path": [p.model_dump() for p in path],
            "pixels": advice.model_dump(),
        }
        (c.evidence / f"map-candidate-{self.calls}.json").write_text(
            json.dumps(proof, ensure_ascii=False, indent=2), "utf-8"
        )
        (c.evidence / f"map-input-{self.calls}.png").write_bytes(png)
