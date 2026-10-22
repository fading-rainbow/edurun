"""显式暂停跳转对照：只发一次位置命令，不继续、运动、打卡或保存成绩。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from subprocess import TimeoutExpired

from .gps_reception import received_gps_matches
from .initial_position import read_gps_position
from .models import GeoPoint
from .mumu import MuMuBridge
from .routes import distance
from .runtime import CapturedRunFrame, StartWitness
from .sensor_probe import NORMAL, sensor_mode
from .target import same_anchor_cards


def relocate_paused_once(
    connector,
    original,
    calibration,
    planner,
    evidence: Path,
    authorize,
    *,
    gps_request,
    start_day: date,
):
    """实验结果只报告原App/实际接收；暂停是否排除跳距仍须之后的原App成绩验证。"""
    bridge = connector.bridge
    if not isinstance(bridge, MuMuBridge) or authorize() is not True:
        raise ValueError("暂停跳转仅限明确授权的独立MuMu")
    original = StartWitness.model_validate(original.model_dump())
    saved = CapturedRunFrame.model_validate(calibration["paused_frame"])
    current, target, received = (
        GeoPoint.model_validate(calibration[k])
        for k in ("command_current", "command_target", "received_current")
    )
    received_target = GeoPoint.model_validate(calibration["received_target"])
    reference = saved.inspection
    identity = (
        reference.package,
        reference.activity,
        reference.version,
        reference.state,
        reference.counter_label,
    )
    original_view = original.second_running.inspection
    cards = [c for c in reference.checkpoint_cards if c.state == "assigned"]
    if (
        original.serial != bridge.serial
        or identity
        != (
            original_view.package,
            original_view.activity,
            original_view.version,
            original_view.state,
            original_view.counter_label,
        )
        or calibration.get("original_request") != original.request_id
        or calibration.get("start_anchor_frame_id") != original.before.frame_id
        or saved.sampled_at <= original.second_running.sampled_at
        or reference.run_control_state != "paused"
        or len(cards) != 1
        or (cards[0].name, cards[0].number) != (calibration["name"], calibration["number"])
        or any(
            calibration.get(k) is not False
            for k in ("navigation_authorized", "position_command_sent", "app_confirmed")
        )
        or not all(planner.contains(p) for p in (current, target, received, received_target))
        or not 10 <= distance(current, target) <= 500
        or abs(calibration["predicted_range_m"] - calibration["actual_range_m"]) > 3
        or abs(cards[0].distance_m - calibration["actual_range_m"]) > 0.01
        or distance(read_gps_position(bridge.run("shell", "dumpsys", "location")), received) > 5
        or bridge.run("shell", "date", "+%F").strip() != start_day.isoformat()
        or sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL
    ):
        raise ValueError("暂停跳转缺少同日、同会话、独立距离及区域标定依据")
    # 标定中的适配器偏移只能是测得的局部平移，不能换成其他目标。
    expected_xy = tuple(
        a + b - c
        for a, b, c in zip(
            planner.xy(received_target), planner.xy(current), planner.xy(received), strict=True
        )
    )
    if distance(target, planner.geo(expected_xy)) > 0.05:
        raise ValueError("暂停跳转目标与测得标定不一致")
    return _move_paused_once(
        connector, original, calibration, saved, received, target, evidence, authorize, gps_request
    )


def return_to_pause_anchor_once(
    connector,
    original,
    calibration,
    visit,
    planner,
    evidence: Path,
    authorize,
    *,
    gps_request,
    start_day: date,
):
    """原App实际两帧确认打卡并再次暂停后，只允许返回此次出发锚点，不改里程。"""
    original = StartWitness.model_validate(original.model_dump())
    old = CapturedRunFrame.model_validate(calibration["paused_frame"])
    final = CapturedRunFrame.model_validate(visit["final"])
    target = GeoPoint.model_validate(calibration["command_current"])
    received = GeoPoint.model_validate(calibration["received_target"])
    names = tuple(c.name for c in old.inspection.checkpoint_cards if c.state == "completed") + (
        calibration["name"],
    )
    completed = []
    for raw in visit["observed"]:
        frame = CapturedRunFrame.model_validate(raw)
        view = frame.inspection
        if (
            view.run_control_state == "running"
            and view.completed_checkpoints == old.inspection.completed_checkpoints + 1
            and tuple(c.name for c in view.checkpoint_cards if c.state == "completed") == names
            and abs(view.distance_m - old.inspection.distance_m) <= 10
        ):
            completed.append(frame)
    if (
        not isinstance(connector.bridge, MuMuBridge)
        or (
            final.inspection.package,
            final.inspection.activity,
            final.inspection.version,
            final.inspection.state,
            final.inspection.counter_label,
        )
        != (
            old.inspection.package,
            old.inspection.activity,
            old.inspection.version,
            old.inspection.state,
            old.inspection.counter_label,
        )
        or original.serial != connector.bridge.serial
        or calibration["original_request"] != original.request_id
        or calibration["start_anchor_frame_id"] != original.before.frame_id
        or len(completed) < 2
        or not old.sampled_at
        < completed[-2].sampled_at
        < completed[-1].sampled_at
        < final.sampled_at
        or completed[-2].frame_id == completed[-1].frame_id
        or final.inspection.run_control_state != "paused"
        or final.inspection.completed_checkpoints != len(names)
        or final.inspection.elapsed_s < completed[-1].inspection.elapsed_s
        or any(
            i != "start_controls_and_current_record_identity_unverified"
            for i in final.inspection.issues
        )
        or tuple(c.name for c in final.inspection.checkpoint_cards if c.state == "completed")
        != names
        or abs(final.inspection.distance_m - old.inspection.distance_m) > 10
        or visit.get("submitted") is not False
        or visit.get("motion_started") is not False
        or not all(planner.contains(p) for p in (target, received))
        or distance(
            read_gps_position(connector.bridge.run("shell", "dumpsys", "location")), received
        )
        > 10
        or connector.bridge.run("shell", "date", "+%F").strip() != start_day.isoformat()
        or sensor_mode(connector.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL
        or authorize() is not True
    ):
        raise ValueError("只返回本次实际双帧打卡后再次暂停的出发位置")
    return _move_paused_once(
        connector,
        original,
        {"kind": "return_to_same_pause_anchor", "calibration": calibration, "visit": visit},
        final,
        received,
        target,
        evidence,
        authorize,
        gps_request,
    )


def _move_paused_once(
    connector, original, calibration, saved, received, target, evidence, authorize, gps_request
):
    bridge, reference = connector.bridge, saved.inspection
    identity = (
        reference.package,
        reference.activity,
        reference.version,
        reference.state,
        reference.counter_label,
    )
    before, began = [], connector.clock()
    while len(before) < 2 and connector.clock() - began < 10 and authorize() is True:
        cap = connector.capture(motion_request=gps_request)
        view = cap.frame.inspection
        if (
            cap.frame.sampled_at <= saved.sampled_at
            or (view.package, view.activity, view.version, view.state, view.counter_label)
            != identity
            or view.run_control_state != "paused"
            or view.distance_m != reference.distance_m
            or view.elapsed_s != reference.elapsed_s
            or view.completed_checkpoints != reference.completed_checkpoints
            or not same_anchor_cards(view.checkpoint_cards, reference.checkpoint_cards)
            or any(
                i != "start_controls_and_current_record_identity_unverified" for i in view.issues
            )
        ):
            raise ValueError("暂停跳转前原App进度未确认")
        if (
            cap.motion_snapshot is None
            or cap.motion_snapshot.get("raw_motion_probe", {}).get("sent_rows") != 0
        ):
            raise ValueError("暂停跳转前零运动实收未确认")
        if received_gps_matches(
            cap.motion_snapshot, gps_request, cap.android_observed_ns, received
        ):
            before.append(cap)
        else:
            connector.sleep(0.2)  # 设备正常GPS回调可能每三秒一次；等待新回调，不放宽两秒门槛。
    if len(before) != 2:
        raise ValueError("暂停跳转前新鲜GPS双帧等待超时")
    last = before[-1]

    def permitted():
        return authorize() is True and 0 <= connector.clock() - last.frame.sampled_at < 2

    if not permitted() or before[0].frame.sampled_at >= last.frame.sampled_at:
        raise ValueError("暂停跳转前观察或许可过期")
    action = evidence / "paused-relocation-action.json"
    payload = {
        "original_request": original.request_id,
        "calibration_sha256": hashlib.sha256(
            json.dumps(calibration, sort_keys=True).encode()
        ).hexdigest(),
        "before": [c.frame.model_dump(mode="json") for c in before],
        "command_target": target.model_dump(),
        "attempted_once": True,
        "acknowledged": False,
        "output_state_unknown": False,
        "continued": False,
        "submitted": False,
    }
    # 不明结果也消费动作文件，不能盲重试或覆盖原始试验。
    with action.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    try:
        bridge.location_wgs84(target, authorize=permitted, command_timeout_s=1)
        payload["acknowledged"] = True
    except TimeoutExpired:
        # 官方管理器超时不等于命令未执行。只观察实际新回调，绝不重发。
        payload["output_state_unknown"] = True
    action.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    after, began = [], connector.clock()
    while connector.clock() - began < 8 and authorize() is True:
        cap = connector.capture(motion_request=gps_request)
        view = cap.frame.inspection
        if view.run_control_state != "paused" or "human_face_verification_required" in view.issues:
            break
        if (
            received_gps_matches(cap.motion_snapshot, gps_request, cap.android_observed_ns, target)
            and cap.motion_snapshot["gps_reception"]["last_received_ns"] > last.android_observed_ns
            and cap.motion_snapshot.get("raw_motion_probe", {}).get("sent_rows") == 0
        ):
            after.append(cap)
            if len(after) == 2 and after[0].frame.sampled_at < after[1].frame.sampled_at:
                break
        connector.sleep(0.2)
    result = payload | {
        "received_target_twice": len(after) == 2,
        "after": [c.frame.model_dump(mode="json") for c in after],
        "actual_receipts": [c.motion_snapshot["gps_reception"] for c in after],
        "input_observations": [
            {
                "frame_id": c.frame.frame_id,
                "android_observed_ns": c.android_observed_ns,
                "snapshot": c.motion_snapshot,
            }
            for c in after
        ],
        "record_valid_proven": False,
        "one_click_workflow_verified": False,
    }
    (evidence / "paused-relocation-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    return result
