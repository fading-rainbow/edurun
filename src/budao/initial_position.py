"""新会话起点规划与已开始会话的同锚点接续；配置坐标不是接收证据。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import GeoPoint
from .routes import RoutePlanner, distance
from .target import same_anchor_cards, same_resume_cards, same_startup_anchor_cards


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


def refresh_started_anchor(connector, proof, initial: GeoPoint, authorize):
    """开始衔接期间只刷新已接收的同一锚点，不启用路线或改写GPS信号。"""
    from .resume import ResumeWitness
    from .runtime import StartWitness

    if (
        not isinstance(proof, (StartWitness, ResumeWitness))
        or not callable(authorize)
        or authorize() is not True
    ):
        raise ValueError("同位置接续缺少本次开始/继续因果证据或许可")
    proof = type(proof).model_validate(proof.model_dump())
    original = proof.original if isinstance(proof, ResumeWitness) else proof
    if original.serial != connector.bridge.serial or not isinstance(initial, GeoPoint):
        raise ValueError("同位置接续设备或锚点不匹配")
    initial = GeoPoint.model_validate(initial.model_dump())
    current = read_gps_position(connector.bridge.run("shell", "dumpsys", "location"))
    if distance(current, initial) > 10:
        raise ValueError("同位置接续不能搬移当前锚点")
    cap = connector.capture()
    v, previous = cap.frame.inspection, proof.second_running.inspection
    card_reference = proof.saved_pause.inspection if isinstance(proof, ResumeWitness) else previous
    cards_match = (
        same_startup_anchor_cards
        if isinstance(proof, StartWitness)
        and previous.distance_m == 0
        and previous.completed_checkpoints == 0
        else same_anchor_cards
    )
    if isinstance(proof, ResumeWitness):

        def cards_match(current, reference):
            return same_resume_cards(current, reference, pending_refresh_m=proof.pending_refresh_m)

    if (
        v.package != previous.package
        or v.activity != previous.activity
        or v.version != previous.version
        or v.state != "metrics"
        or v.run_control_state != "running"
        or v.counter_label != "已打卡"
        or v.completed_checkpoints != previous.completed_checkpoints
        or v.distance_m != previous.distance_m
        or not cards_match(v.checkpoint_cards, card_reference.checkpoint_cards)
        or v.elapsed_s is None
        or not previous.elapsed_s <= v.elapsed_s <= previous.elapsed_s + 15
        or v.gps_label not in {"GPS信号优", "GPS信号弱"}
        or any(i != "start_controls_and_current_record_identity_unverified" for i in v.issues)
    ):
        raise ValueError("同位置接续不是刚确认开始的同一打卡进度会话")

    def permitted():
        return 0 <= connector.clock() - cap.frame.sampled_at < 2 and authorize() is True

    if not permitted():
        raise ValueError("同位置接续观察或许可已经过期")
    connector.bridge.location_wgs84(initial, authorize=permitted, command_timeout_s=1)
