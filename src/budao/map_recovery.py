"""Paused-map recovery: model pixels + own received trace + independent card range.

Experimental navigation candidates are NOT historical App confirmations or grades.
The fixed region, speed, distance and completion guards cannot be changed by the model.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass
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
MAX_MAP_CALLS = 6  # 三个未知点，每点最多当前视口+一次缩小；不增加现有key的总额度。


@dataclass(frozen=True)
class ReceivedAnchors:
    near: GeoPoint
    far: GeoPoint
    current: GeoPoint
    far_kind: str = "turn"


class TraceRegistration:
    """Four-parameter similarity, three independent trace/position anchors.

    Fit residual is reported as fit residual, not independent validation. The
    original App's current target distance is the separate range-validation gate.
    """

    def __init__(self, planner, pixels, geos):
        if len(pixels) != 3 or len(geos) != 3:
            raise ValueError("需要两个接收轨迹锚点及当前接收定位")
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


def received_trace(
    path: Path, planner: RoutePlanner, *, start_frame_id=None, start_request_id=None
):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("接收轨迹日志超过有限窗口")
    points, start_point = [], None
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
        if (
            start_frame_id is not None
            and start_request_id is not None
            and value.get("frame_id") == start_frame_id
            and value.get("motion", {}).get("request_id") == start_request_id
        ):
            if start_point is not None:
                raise ValueError("本次开始帧的接收定位不唯一")
            start_point = point
        if not points or distance(points[-1], point) > 0.1:
            points.append(point)
    if len(points) < 8:
        raise ValueError("实际接收轨迹不足，不能标定未知点")
    simplified = list(LineString([planner.xy(p) for p in points]).simplify(2).coords)
    current, turns = points[-1], []
    for i in range(len(simplified) - 2, 0, -1):
        b = np.asarray(simplified[i])
        # GPS采样的圆角可被RDP保留为相距几米的两个真实顶点。支持方向应跨过
        # 这些短邻边，而不是丢掉整个转角；锚点仍是原始接收点，不计算虚拟交点。
        before, after = i - 1, i + 1
        while before > 0 and np.linalg.norm(b - simplified[before]) < 10:
            before -= 1
        while after < len(simplified) - 1 and np.linalg.norm(simplified[after] - b) < 10:
            after += 1
        a, c = np.asarray(simplified[before]), np.asarray(simplified[after])
        incoming, outgoing = b - a, c - b
        if min(np.linalg.norm(incoming), np.linalg.norm(outgoing)) < 10:
            continue
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        angle = math.degrees(math.atan2(abs(cross), incoming @ outgoing))
        point = planner.geo(simplified[i])
        if angle < 25 or distance(point, turns[-1] if turns else current) < 10:
            continue
        turns.append(point)
        if len(turns) == 2:
            return ReceivedAnchors(turns[0], turns[1], current)
    # 第一随机点可能在走完一个转角时就分配。只有原始开始凭据对应的那一帧
    # 独立接收位置，才可与明确的地图起始图标配对；任意线段端点不是起点证明。
    if (
        turns
        and start_point is not None
        and distance(start_point, points[0]) < 5
        and distance(start_point, turns[0]) >= 10
    ):
        return ReceivedAnchors(turns[0], start_point, current, "start_marker")
    raise ValueError("独立接收转角不足，且无绑定本次开始帧的地图起点依据")


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
        try:
            return self._resolve(authorize)
        except (ValueError, RuntimeError) as error:
            # 仅本机私有诊断，公共状态仍是固定提示；不能再次丢失实际标定失败原因。
            with (self.connector.evidence / "map-recovery-failures.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    json.dumps(
                        {"type": type(error).__name__, "detail": str(error)}, ensure_ascii=False
                    )
                    + "\n"
                )
            raise

    def _resolve(self, authorize):
        c, d = self.connector, self.dispatcher
        if authorize() is not True or self.calls >= MAX_MAP_CALLS or d.worker.state.moving:
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
        from .runtime import StartWitness

        original = StartWitness.model_validate_json(
            (c.evidence / "start-witness.json").read_text("utf-8-sig")
        )
        if original.serial != c.bridge.serial:
            raise ValueError("地图标定的开始凭据设备不匹配")
        for path in (c.evidence / "input-observations.jsonl", *self.trace_sources):
            if path.exists():
                try:
                    anchors = received_trace(
                        path,
                        d.planner,
                        start_frame_id=original.before.frame_id,
                        start_request_id=original.request_id,
                    )
                    near, far, actual = anchors.near, anchors.far, anchors.current
                    source = path
                    break
                except ValueError as error:
                    with (c.evidence / "map-trace-rejections.jsonl").open(
                        "a", encoding="utf-8"
                    ) as stream:
                        stream.write(
                            json.dumps(
                                {"source_record": path.parent.name, "detail": str(error)},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    continue
        if source is None:
            raise ValueError("没有本次实际接收轨迹的地图标定依据")
        # 先看实际视口；只有标记/锚点不可见时才允许一次缩小，不能每次恢复都盲目缩放。
        map_view = c.ui.device(resourceId="com.lptiyu.tanke:id/textureMapView")
        if map_view.count != 1 or authorize() is not True:
            raise ValueError("地图控件或许可未知")
        for attempt in range(2):
            cap = c.capture(pixels=True)
            if cap.frame.inspection != view:
                raise ValueError("地图观察期间原始记录或卡片改变")
            after = c.capture()
            if after.frame.inspection != view:
                raise ValueError("截图期间原记录或卡片改变，不发送")
            image = Image.open(
                io.BytesIO(
                    redacted_ui_image(
                        cap.screenshot, cap.hierarchy, after_hierarchy=after.hierarchy
                    )
                )
            )
            png = crop_image(image, CROP)
            cropped = Image.open(io.BytesIO(png)).convert("RGB")
            enlarged = cropped.resize((1440, 1132))
            out = io.BytesIO()
            enlarged.save(out, format="PNG")
            if self.calls >= MAX_MAP_CALLS or authorize() is not True:
                raise ValueError("地图建议许可或预算已用完")
            self.calls += 1
            (c.evidence / f"map-input-{self.calls}.png").write_bytes(png)
            client = CheckpointAdvisor.from_private_config(self.root, self.config)
            try:
                advice = client.locate(out.getvalue(), far_anchor_kind=anchors.far_kind)
            finally:
                client.close()
            (c.evidence / f"map-advice-{self.calls}.json").write_text(
                json.dumps({"navigation_authorized": False, "pixels": advice.model_dump()}), "utf-8"
            )
            if advice.confidence >= 0.9 and all(
                p is not None
                for p in (advice.current, advice.target, advice.near_turn, advice.far_turn)
            ):
                break
            if attempt or card.distance_m <= 80 or self.calls >= MAX_MAP_CALLS:
                raise ValueError("地图像素置信度或锚点不足")
            current = c.capture().frame.inspection
            if current != view or authorize() is not True:
                raise ValueError("缩放前原始记录或许可改变")
            c.bridge.check_device()
            map_view.gesture((100, 640), (620, 640), (260, 640), (460, 640), steps=40)
        data = advice.model_dump()
        for name in ("current", "target", "near_turn", "far_turn"):
            if data[name] is not None:
                data[name] = {k: v / 2 for k, v in data[name].items()}
        data["road_path"] = [{k: v / 2 for k, v in p.items()} for p in data["road_path"]]
        advice = CheckpointPixels.model_validate(data)
        if advice.confidence < 0.9 or any(
            p is None for p in (advice.current, advice.target, advice.near_turn, advice.far_turn)
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
        # Translate the map's received coordinate space back to this worker's
        # command position. This is a measured local adapter offset, not a jump.
        ox, oy = d.planner.xy(d.worker.engine.position)
        gx, gy = d.planner.xy(actual)

        def shift(p):
            return d.planner.geo((d.planner.xy(p)[0] + ox - gx, d.planner.xy(p)[1] + oy - gy))

        using_known_roads, approach_only, approach_gap_m = False, False, 0.0
        command_target = shift(target)
        try:
            path = list(d.planner.plan(d.worker.engine.position, command_target))
            using_known_roads = True
        except ValueError:
            if len(advice.road_path) < 2:
                closest = d.planner.geo(
                    nearest_points(d.planner.roads, Point(d.planner.xy(command_target)))[0].coords[
                        0
                    ]
                )
                approach_gap_m = distance(closest, command_target)
                if approach_gap_m > 60:
                    raise ValueError("目标不在既有路网且地图道路不可见；不猜测路径") from None
                # 只前往已有道路上距标记最近的接近点，不假定App打卡半径、不中途穿楼。
                # 到达后仍必须观察原App两帧真实确认，十秒不确认即暂停，不将接近当成功。
                path = list(d.planner.plan(d.worker.engine.position, closest))
                using_known_roads, approach_only = True, True
            else:
                path = [registration.map(p) for p in advice.road_path]
                if distance(path[0], actual) > 3 or distance(path[-1], target) > 2:
                    raise ValueError("建议道路起止点不一致") from None
                path = (
                    [d.worker.engine.position] + [shift(p) for p in path[1:-1]] + [command_target]
                )
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
        planner = d.planner
        if not using_known_roads:
            config = RouteConfig.model_validate(
                d.planner.config.model_dump()
                | {"corridors": d.planner.config.corridors + (corridor,)}
            )
            planner = RoutePlanner(config)
        planner.plan(d.worker.engine.position, path[-1])
        with d.worker.lock:
            d.planner = d.worker.engine.planner = planner
            d.route_key = None
        d.observer.navigation_candidates[card.name] = path[-1]
        if approach_only:
            d.observer.approach_only_names.add(card.name)
        proof = {
            "name": card.name,
            "app_confirmed": False,
            "trace_source": str(source),
            "far_anchor_kind": anchors.far_kind,
            "using_known_roads": using_known_roads,
            "approach_only": approach_only,
            "approach_gap_m": approach_gap_m,
            "estimated_target": command_target.model_dump(),
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
