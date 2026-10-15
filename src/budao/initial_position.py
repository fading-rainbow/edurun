"""新会话起点规划：配置坐标不是接收证据，定位写入只能在正式零状态之后。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import GeoPoint
from .routes import RoutePlanner


@dataclass(frozen=True)
class InitialPositionPlan:
    observed: GeoPoint
    planned: GeoPoint
    initialization_required: bool


def read_gps_position(dump: str) -> GeoPoint:
    # 不从 fused/network 或后续其他 provider 的缓存冒认 GPS。
    sections = re.split(r"(?m)^    ([a-zA-Z0-9_]+) provider:\s*$", dump)
    gps = [sections[i + 1] for i in range(1, len(sections), 2) if sections[i] == "gps"]
    if len(gps) != 1:
        raise ValueError("同设备GPS提供器缺失或不唯一")
    matches = re.findall(
        r"(?m)^\s*last location=Location\[gps (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) ",
        gps[0],
    )
    if len(matches) != 1:
        raise ValueError("没有唯一的同设备GPS缓存位置")
    return GeoPoint(lat=float(matches[0][0]), lon=float(matches[0][1]))


def plan_initial_position(
    planner: RoutePlanner, observed: GeoPoint, *, initialize_fresh: bool = False
) -> InitialPositionPlan:
    try:
        planner.plan(observed, planner.config.baseline[0])
    except ValueError:
        if initialize_fresh is not True:
            raise ValueError("当前定位不在可达通道；未授权新会话设置起点") from None
        # 仅使用经过同一路网/区域验证的本机配置，不能由模型生成起点。
        initial = planner.config.baseline[0]
        planner.plan(initial, initial)
        return InitialPositionPlan(observed, initial, True)
    return InitialPositionPlan(observed, observed, False)


def refresh_initial_position(connector, initial: GeoPoint, authorize):
    if not callable(authorize) or authorize() is not True:
        raise ValueError("本次起点设置未获许可")
    cap = connector.capture(pixels=True)
    view = cap.frame.inspection
    if (
        view.state != "metrics"
        or any(
            i not in {"start_controls_and_current_record_identity_unverified", "gps_searching"}
            for i in view.issues
        )
        or view.run_control_state != "not_started_or_countdown"
        or view.counter_label != "已打卡"
        or view.distance_m != 0
        or view.elapsed_s != 0
        or view.completed_checkpoints != 0
        or len(view.checkpoint_cards) != 1
        or view.checkpoint_cards[0].state != "pending"
        or cap.screenshot is None
        or not connector.template.matches(
            view, cap.screenshot, cap.hierarchy, allow_gps_searching=True
        )
    ):
        raise ValueError("起点设置只允许本次已核实规则的正式零状态")

    def permitted():
        return 0 <= connector.clock() - cap.frame.sampled_at < 2 and authorize() is True

    if not permitted():
        raise ValueError("本次零状态观察或起点设置许可已过期")
    connector.bridge.location_wgs84(initial, authorize=permitted, command_timeout_s=1)
