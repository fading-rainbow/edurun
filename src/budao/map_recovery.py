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
from PIL import Image, ImageFilter
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .checkpoint_vision import CheckpointAdvisor, CheckpointPixels
from .models import GeoPoint, PixelPoint, RouteConfig
from .routes import RoutePlanner, distance, path_length
from .sensor_probe import NORMAL, sensor_mode
from .ui_advisor import redacted_ui_image
from .vision import crop_image

CROP = (0, 366, 720, 932)
MAX_MAP_CALLS = 6  # 整个执行最多6次；局部增加缩放机会不增加调用总额或key额度。
MAX_MAP_ZOOMS = 3  # 437米目标在两次缩小后的裁剪视口外，仍保留总请求预算。


@dataclass(frozen=True)
class ReceivedRangeFix:
    position: GeoPoint
    frame_id: str
    received_ns: int


@dataclass(frozen=True)
class ReceivedAnchors:
    near: GeoPoint
    far: GeoPoint | None
    current: GeoPoint
    far_kind: str = "turn"
    recent_fixes: tuple[ReceivedRangeFix, ...] = ()
    range_observed_ns: int | None = None
    near_kind: str = "received_vertex"
    near_supports: tuple[GeoPoint, ...] = ()


class TraceRegistration:
    """Four-parameter similarity from two or three explicit geometric features.

    Fit residual is reported as fit residual, not independent validation. The
    original App's current target distance is the separate range-validation gate.
    """

    def __init__(self, planner, pixels, geos):
        if len(pixels) not in (2, 3) or len(geos) != len(pixels):
            raise ValueError("需要两个或三个明示的轨迹/定位特征")
        self.anchor_count = len(pixels)
        xy = np.asarray([planner.xy(p) for p in geos])
        if len(pixels) == 2 and (
            distance(*geos) < 40
            or math.hypot(pixels[0].x - pixels[1].x, pixels[0].y - pixels[1].y) < 40
        ):
            raise ValueError("两特征配准需要至少40米和40像素的独立基线")
        if len(pixels) == 3 and np.linalg.matrix_rank(np.column_stack([xy, np.ones(3)])) != 3:
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

    def validate_range(
        self, target, current, actual_range_m, *, recent_fixes=(), range_observed_ns=None
    ):
        estimate = self.map(target)
        predicted = distance(estimate, current)
        self.current_predicted_range_m, self.range_reference = predicted, None
        if not math.isfinite(actual_range_m) or not 0 < actual_range_m <= 500:
            raise ValueError("原App独立距离与地图估计不一致，不导航")
        if abs(predicted - actual_range_m) <= 3:
            return estimate, predicted
        # App卡片与地图定位回调异步。只核对同一日志末尾两条、在原UI采集两秒内
        # 的真实回调，不搜索任意历史位置，也不修改目标估计或3米阈值。
        if type(range_observed_ns) is int:
            for fix in recent_fixes[:2]:
                if (
                    isinstance(fix, ReceivedRangeFix)
                    and isinstance(fix.position, GeoPoint)
                    and type(fix.received_ns) is int
                    and 0 <= range_observed_ns - fix.received_ns <= 2_000_000_000
                    and type(fix.frame_id) is str
                    and 0 < len(fix.frame_id) <= 128
                    and self.planner.contains(fix.position)
                    and distance(current, fix.position) <= 10
                ):
                    measured = distance(estimate, fix.position)
                    if abs(measured - actual_range_m) <= 3:
                        self.range_reference = fix
                        self.range_observed_ns = range_observed_ns
                        return estimate, measured
        raise ValueError("原App独立距离与地图估计不一致，不导航")


def _trace_values(path: Path, extra_paths):
    paths = tuple(dict.fromkeys((path, *extra_paths)))
    if len(paths) > 5 or sum(p.stat().st_size for p in paths) > 4 * 1024 * 1024:
        raise ValueError("接收轨迹日志超过有限窗口")
    values, seen, previous = [], {}, None
    # 来源由本机已验证的续跑父子链给出：最旧父记录在最后，不能用排序重写时钟。
    for source in reversed(paths):
        for line in source.read_text("utf-8-sig").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("接收轨迹行结构未知")
            frame_id = value.get("frame_id")
            if isinstance(frame_id, str):
                if frame_id in seen:
                    if seen[frame_id] != value:
                        raise ValueError("重复接收帧数据冲突，不拼接")
                    continue
                seen[frame_id] = value
            if len(paths) > 1:
                sampled, observed = value.get("sampled_at"), value.get("android_observed_ns")
                if (
                    type(sampled) not in (int, float)
                    or not math.isfinite(sampled)
                    or sampled < 0
                    or type(observed) is not int
                    or observed <= 0
                    or (previous is not None and (sampled < previous[0] or observed < previous[1]))
                ):
                    raise ValueError("接收轨迹跨时钟会话或缺少真实采集时间，不拼接")
                previous = sampled, observed
            values.append(value)
    return values


def received_trace(
    path: Path,
    planner: RoutePlanner,
    *,
    start_frame_id=None,
    start_request_id=None,
    extra_paths=(),
    pair_mode=False,
):
    points, start_point, fixes, latest_observed = [], None, [], None
    for value in _trace_values(path, extra_paths):
        if value.get("exclude_route_calibration") is True:
            continue  # 保留原始行；暂停访问的跳距不是连续跑步轨迹，不作为拐角配准依据。
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
        latest_observed = observed
        if type(value.get("frame_id")) is str:
            fixes.append(ReceivedRangeFix(point, value["frame_id"], gps["last_received_ns"]))
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
    recent, seen = [], set()
    for fix in reversed(fixes):
        identity = fix.received_ns
        if 0 <= latest_observed - fix.received_ns <= 2_000_000_000 and identity not in seen:
            recent.append(fix)
            seen.add(identity)
            if len(recent) == 2:
                break
    current, turns = points[-1], []

    def independent(left, right):
        a, b, c = (np.asarray(planner.xy(p)) for p in (left, right, current))
        u, v = b - a, c - a
        return abs(u[0] * v[1] - u[1] * v[0]) / max(np.linalg.norm(u), np.linalg.norm(v), 1) >= 5

    for i in range(len(simplified) - 2, 0, -1):
        b = np.asarray(simplified[i])
        # GPS圆角可被RDP保留为相距几米的两个顶点；跨过短邻边读取方向。
        # 常规模式保留原顶点；配准模式明确记录由实际直线段支持的派生交点。
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
        # 折返180度在已绘轨迹上重合，没有可辨认的新几何拐角。
        if angle < 25 or angle > 155 or distance(point, turns[-1] if turns else current) < 10:
            continue
        if pair_mode:
            left, right = before + 1, after - 1
            while (
                before > 0
                and np.linalg.norm(np.asarray(simplified[left]) - simplified[before]) < 10
            ):
                before -= 1
            while (
                after < len(simplified) - 1
                and np.linalg.norm(np.asarray(simplified[after]) - simplified[right]) < 10
            ):
                after += 1
            supports = np.asarray([simplified[j] for j in (before, left, right, after)])
            incoming, outgoing = supports[1] - supports[0], supports[3] - supports[2]
            if min(np.linalg.norm(incoming), np.linalg.norm(outgoing)) < 10:
                continue
            turn = math.degrees(
                math.atan2(
                    abs(incoming[0] * outgoing[1] - incoming[1] * outgoing[0]), incoming @ outgoing
                )
            )
            if not 25 <= turn <= 155:
                continue
            try:
                amount = np.linalg.solve(
                    np.column_stack((incoming, -outgoing)), supports[2] - supports[0]
                )
            except np.linalg.LinAlgError:
                continue
            corner = supports[0] + amount[0] * incoming
            derived = planner.geo(corner)
            if (
                np.linalg.norm(corner - b) > 10
                or LineString(supports).distance(Point(corner)) > 5
                or not planner.contains(derived)
                or distance(derived, current) < 40
            ):
                continue
            return ReceivedAnchors(
                near=derived,
                far=None,
                current=current,
                far_kind="none",
                recent_fixes=tuple(recent),
                range_observed_ns=latest_observed,
                near_kind="supported_line_intersection",
                near_supports=tuple(planner.geo(p) for p in supports),
            )
        turns.append(point)
        if len(turns) == 2:
            if not independent(turns[0], turns[1]):
                turns.pop()
                continue
            if (
                start_point is not None
                and distance(start_point, points[0]) < 5
                and distance(start_point, turns[0]) >= 10
                and independent(turns[0], start_point)
                and distance(start_point, current) <= distance(turns[1], current)
            ):
                return ReceivedAnchors(
                    turns[0], start_point, current, "start_marker", tuple(recent), latest_observed
                )
            return ReceivedAnchors(
                turns[0],
                turns[1],
                current,
                recent_fixes=tuple(recent),
                range_observed_ns=latest_observed,
            )
    # 首点可能在转角附近就分配，短转角基线不足时仍可使用明确起点图标。
    # 起点必须来自原始开始帧及其任务的实际接收，绝不使用任意轨迹端点。
    if (
        pair_mode
        and start_point is not None
        and distance(start_point, points[0]) < 5
        and distance(start_point, current) >= 40
    ):
        return ReceivedAnchors(
            near=start_point,
            far=None,
            current=current,
            far_kind="start_marker_pair",
            recent_fixes=tuple(recent),
            range_observed_ns=latest_observed,
            near_kind="received_start_marker",
            near_supports=(start_point,),
        )
    # 第一随机点可能在走完一个转角时就分配。只有原始开始凭据对应的那一帧
    # 独立接收位置，才可与明确的地图起始图标配对；任意线段端点不是起点证明。
    if (
        turns
        and start_point is not None
        and distance(start_point, points[0]) < 5
        and distance(start_point, turns[0]) >= 10
        and independent(turns[0], start_point)
    ):
        return ReceivedAnchors(
            turns[0], start_point, current, "start_marker", tuple(recent), latest_observed
        )
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


def refined_green_center(image: Image.Image, approximate: PixelPoint, *, radius: int) -> PixelPoint:
    """细化固定大小的绿色定位/起点圆，不用原App距离调整像素或比例。"""
    if radius not in (17, 22):
        raise ValueError("未知绿色圆形图标尺寸")
    x, y = approximate.x, approximate.y
    left, top = int(x) - 30, int(y) - 30
    if left < 0 or top < 0 or left + 60 > image.width or top + 60 > image.height:
        raise ValueError("绿色锚点圆被裁剪")
    rgb = np.asarray(image.convert("RGB").crop((left, top, left + 60, top + 60))).astype(int)
    red, green, blue = (rgb[:, :, i] for i in range(3))
    # 深绿色图标与纯亮绿色轨迹分开；白字/箭头在内圈，不作为外圆边缘。
    mask = (
        (red < 120)
        & (green > 100)
        & (green < 210)
        & (blue > 50)
        & (blue < 190)
        & (green - red > 65)
        & (green - blue > 20)
    )
    interior = (
        np.roll(mask, 1, axis=0)
        & np.roll(mask, -1, axis=0)
        & np.roll(mask, 1, axis=1)
        & np.roll(mask, -1, axis=1)
    )
    ys, xs = np.nonzero(mask & ~interior)
    points = np.column_stack((xs + left, ys + top))
    lengths = np.linalg.norm(points - [x, y], axis=1)
    points = points[(lengths > radius * 0.75) & (lengths < radius + 3)]
    center, fitted_radius = np.array([x, y]), float(radius)
    for _ in range(5):
        selected = points[np.abs(np.linalg.norm(points - center, axis=1) - fitted_radius) < 2]
        if len(selected) < 45:
            raise ValueError("绿色圆形锚点边缘不足")
        angles = np.arctan2(selected[:, 1] - center[1], selected[:, 0] - center[0])
        if len(set(np.floor((angles + math.pi) / (math.pi / 4)).astype(int) % 8)) < 7:
            raise ValueError("绿色圆形锚点边缘不完整")
        fit = np.linalg.lstsq(
            np.column_stack((2 * selected, np.ones(len(selected)))),
            (selected**2).sum(axis=1),
            rcond=None,
        )[0]
        center = fit[:2]
        squared = fit[2] + center @ center
        if squared <= 0:
            raise ValueError("绿色锚点不是圆形")
        fitted_radius = math.sqrt(squared)
    if np.linalg.norm(center - [x, y]) > 4 or abs(fitted_radius - radius) > 2:
        raise ValueError("绿色圆形锚点与模型定位不一致")
    return PixelPoint(x=float(center[0]), y=float(center[1]))


def same_map_viewport(before: Image.Image, after: Image.Image, target: PixelPoint) -> bool:
    """比较轨迹覆盖物和打卡标记，不把底图瓦片/地名加载误报成视口移动。"""
    if before.size != after.size:
        return False
    masks = []
    for image in (before, after):
        rgb = np.asarray(image.convert("RGB"))
        mask = (rgb[:, :, 1] > 220) & (rgb[:, :, 0] < 60) & (rgb[:, :, 2] < 60)
        if not 100 <= mask.sum() <= mask.size / 4:
            return False
        masks.append(mask)
    if not 0.99 <= masks[0].sum() / masks[1].sum() <= 1.01:
        return False
    expanded = [
        np.asarray(Image.fromarray((mask * 255).astype("uint8")).filter(ImageFilter.MaxFilter(3)))
        > 0
        for mask in masks
    ]
    if any(
        (mask & other).sum() / mask.sum() < 0.995
        for mask, other in zip(masks, reversed(expanded), strict=True)
    ):
        return False
    try:
        first, second = (refined_target_center(image, target) for image in (before, after))
    except ValueError:
        return False
    return math.hypot(first.x - second.x, first.y - second.y) <= 1


class MapRecovery:
    def __init__(self, root, config, connector, dispatcher, *, trace_sources=()):
        self.root, self.config = root, config
        self.connector, self.dispatcher = connector, dispatcher
        self.trace_sources = tuple(trace_sources)
        self.calls = 0

    def resolve(self, authorize, *, calibration_only=False):
        if type(calibration_only) is not bool:
            raise ValueError("仅标定选项必须明确")
        try:
            return self._resolve(authorize, calibration_only=calibration_only)
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

    def _resolve(self, authorize, *, calibration_only=False):
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
        if card.name in d.observer.allowed_names and not calibration_only:
            return
        source = None
        from .runtime import StartWitness

        original = StartWitness.model_validate_json(
            (c.evidence / "start-witness.json").read_text("utf-8-sig")
        )
        if original.serial != c.bridge.serial:
            raise ValueError("地图标定的开始凭据设备不匹配")
        paths = tuple(
            p for p in (c.evidence / "input-observations.jsonl", *self.trace_sources) if p.is_file()
        )
        if not paths:
            raise ValueError("没有本次实际接收轨迹的地图标定依据")
        anchors = received_trace(
            paths[0],
            d.planner,
            extra_paths=paths[1:],
            start_frame_id=original.before.frame_id,
            start_request_id=original.request_id,
            pair_mode=True,
        )
        near, actual = anchors.near, anchors.current
        source = paths[0]
        # 先看实际视口；标记/锚点不可见时最多缩小三次，总调用预算仍是6次。
        map_view = c.ui.device(resourceId="com.lptiyu.tanke:id/textureMapView")
        if map_view.count != 1 or authorize() is not True:
            raise ValueError("地图控件或许可未知")
        for attempt in range(MAX_MAP_ZOOMS + 1):
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
                p is not None for p in (advice.current, advice.target, advice.near_turn)
            ):
                break
            if attempt == MAX_MAP_ZOOMS or card.distance_m <= 80 or self.calls >= MAX_MAP_CALLS:
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
            p is None for p in (advice.current, advice.target, advice.near_turn)
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
        target_pixel = refined_target_center(cropped, advice.target)
        fresh = Image.open(io.BytesIO(fresh_png)).convert("RGB")
        if not same_map_viewport(cropped, fresh, target_pixel):
            raise ValueError("模型期间地图视口改变，标定失效")
        anchor_pixel, current_pixel = advice.near_turn, advice.current
        if anchors.far_kind == "start_marker_pair":
            anchor_pixel = refined_green_center(cropped, anchor_pixel, radius=22)
            current_pixel = refined_green_center(cropped, current_pixel, radius=17)
        registration = TraceRegistration(d.planner, [anchor_pixel, current_pixel], [near, actual])
        target, predicted = registration.validate_range(
            target_pixel,
            actual,
            card.distance_m,
            recent_fixes=anchors.recent_fixes,
            range_observed_ns=anchors.range_observed_ns,
        )
        # Translate the map's received coordinate space back to this worker's
        # command position. This is a measured local adapter offset, not a jump.
        ox, oy = d.planner.xy(d.worker.engine.position)
        gx, gy = d.planner.xy(actual)

        def shift(p):
            return d.planner.geo((d.planner.xy(p)[0] + ox - gx, d.planner.xy(p)[1] + oy - gy))

        using_known_roads, approach_only, approach_gap_m = False, False, 0.0
        command_target = shift(target)
        if calibration_only:
            # 对照实验只获得经实收轨迹/原App距离独立校验的中心；不改路网、导航或位置。
            proof = {
                "name": card.name,
                "number": card.number,
                "original_request": original.request_id,
                "paused_frame": latest.frame.model_dump(mode="json"),
                "start_anchor_frame_id": original.before.frame_id,
                "far_anchor_kind": anchors.far_kind,
                "received_current": actual.model_dump(),
                "command_current": d.worker.engine.position.model_dump(),
                "received_target": target.model_dump(),
                "command_target": command_target.model_dump(),
                "actual_range_m": card.distance_m,
                "predicted_range_m": predicted,
                "fit_residuals_m": registration.fit_residuals_m,
                "registration_pixels": {
                    "anchor": anchor_pixel.model_dump(),
                    "current": current_pixel.model_dump(),
                    "target": target_pixel.model_dump(),
                },
                "navigation_authorized": False,
                "position_command_sent": False,
                "app_confirmed": False,
                "pixels": advice.model_dump(),
            }
            (c.evidence / f"map-calibration-{self.calls}.json").write_text(
                json.dumps(proof, ensure_ascii=False, indent=2), "utf-8"
            )
            return proof
        try:
            path = list(d.planner.plan(d.worker.engine.position, command_target))
            using_known_roads = True
        except ValueError:
            closest = d.planner.geo(
                nearest_points(d.planner.roads, Point(d.planner.xy(command_target)))[0].coords[0]
            )
            known_gap_m = distance(closest, command_target)
            if known_gap_m <= 60:
                # 只前往已有道路上距标记最近的接近点，不假定App打卡半径、不中途穿楼。
                # 到达后仍必须观察原App两帧真实确认，十秒不确认即暂停，不将接近当成功。
                path = list(d.planner.plan(d.worker.engine.position, closest))
                using_known_roads, approach_only = True, True
                approach_gap_m = known_gap_m
            else:
                if len(advice.road_path) < 2:
                    raise ValueError("目标不在既有路网且地图道路不可见；不猜测路径") from None
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
            "trace_sources": [str(path) for path in paths],
            "far_anchor_kind": anchors.far_kind,
            "anchor_count": 2,
            "near_feature_kind": anchors.near_kind,
            "near_feature_supports": [p.model_dump() for p in anchors.near_supports],
            "registration_features": {
                "anchor": near.model_dump(),
                "received_current": actual.model_dump(),
            },
            "start_anchor_frame_id": original.before.frame_id
            if anchors.near_kind == "received_start_marker"
            else None,
            "using_known_roads": using_known_roads,
            "approach_only": approach_only,
            "approach_gap_m": approach_gap_m,
            "estimated_target": command_target.model_dump(),
            "fit_residuals_m": registration.fit_residuals_m,
            "actual_range_m": card.distance_m,
            "predicted_range_m": predicted,
            "current_predicted_range_m": getattr(
                registration, "current_predicted_range_m", predicted
            ),
            "range_reference": (
                {
                    "frame_id": registration.range_reference.frame_id,
                    "position": registration.range_reference.position.model_dump(),
                    "received_ns": registration.range_reference.received_ns,
                    "observed_ns": registration.range_observed_ns,
                }
                if getattr(registration, "range_reference", None) is not None
                else None
            ),
            "candidate": path[-1].model_dump(),
            "road_path": [p.model_dump() for p in path],
            "pixels": advice.model_dump(),
        }
        (c.evidence / f"map-candidate-{self.calls}.json").write_text(
            json.dumps(proof, ensure_ascii=False, indent=2), "utf-8"
        )
        (c.evidence / f"map-input-{self.calls}.png").write_bytes(png)
