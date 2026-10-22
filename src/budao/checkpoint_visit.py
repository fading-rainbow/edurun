"""暂停访问打卡点并返回原锚点；只继承原App实际完成，不生成打卡或成绩。"""

from __future__ import annotations

import json

from pydantic import Field, model_validator

from .android import DIAGNOSTIC_PACKAGE
from .foreground_motion import ForegroundMotionClient
from .gps_reception import received_gps_matches
from .models import GeoPoint, StrictModel
from .paused_relocation import relocate_paused_once, return_to_pause_anchor_once
from .resume import resume_completed_names
from .routes import distance
from .runtime import CapturedRunFrame, StartWitness
from .sensor_probe import NORMAL, sensor_mode
from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET


class VisitReception(StrictModel):
    frame_id: str
    android_observed_ns: int = Field(gt=0, strict=True)
    snapshot: dict


class PausedVisitWitness(StrictModel):
    original: StartWitness
    origin: GeoPoint
    target: GeoPoint
    departure: tuple[CapturedRunFrame, CapturedRunFrame]
    outbound: tuple[CapturedRunFrame, CapturedRunFrame]
    completed: tuple[CapturedRunFrame, CapturedRunFrame]
    returned: tuple[CapturedRunFrame, CapturedRunFrame]
    outbound_receipts: tuple[VisitReception, VisitReception]
    returned_receipts: tuple[VisitReception, VisitReception]
    continue_action_at: float = Field(ge=0)
    pause_action_at: float = Field(ge=0)

    @model_validator(mode="after")
    def actual_visit(self):
        frames = self.departure + self.outbound + self.completed + self.returned
        old = self.departure[0].inspection
        assigned = [c for c in old.checkpoint_cards if c.state == "assigned"]
        if (
            len(assigned) != 1
            or old.completed_checkpoints not in range(3)
            or old.distance_m is None
            or old.elapsed_s is None
            or old.elapsed_s <= 0
            or not 10 <= distance(self.origin, self.target) <= 500
        ):
            raise ValueError("访问前没有唯一顺序打卡点")
        name = assigned[0].name
        previous_names = resume_completed_names(old)
        if (
            len(previous_names) != old.completed_checkpoints
            or assigned[0].number != len(previous_names) + 1
        ):
            raise ValueError("访问前打卡顺序未知")
        previous = self.original.second_running.sampled_at
        ids = set()
        for i, f in enumerate(frames):
            v = f.inspection
            if (
                f.frame_id in ids
                or f.sampled_at <= previous
                or (v.package, v.activity, v.version, v.state, v.counter_label)
                != (TARGET, RUN_ACTIVITY, "4.3.0", "metrics", "已打卡")
                or v.gps_label != GPS_READY_LABEL
                or v.distance_m is None
                or abs(v.distance_m - old.distance_m) > 10
                or v.elapsed_s is None
                or not old.elapsed_s <= v.elapsed_s <= old.elapsed_s + 30
                or any(
                    x != "start_controls_and_current_record_identity_unverified" for x in v.issues
                )
            ):
                raise ValueError("访问证据的身份、时钟、里程或实际界面未知")
            if i < 4:
                if (
                    v.run_control_state != "paused"
                    or v.elapsed_s != old.elapsed_s
                    or v.completed_checkpoints != old.completed_checkpoints
                    or v.checkpoint_cards != old.checkpoint_cards
                ):
                    raise ValueError("跳转前后未保持同一暂停记录")
            elif (
                v.completed_checkpoints != old.completed_checkpoints + 1
                or resume_completed_names(v) != previous_names + (name,)
                or v.run_control_state != ("running" if i < 6 else "paused")
            ):
                raise ValueError("没有原App顺序完成两帧及返回后的暂停")
            ids.add(f.frame_id)
            previous = f.sampled_at
        if not (
            self.outbound[-1].sampled_at < self.continue_action_at < self.completed[0].sampled_at
            and self.completed[0].inspection.elapsed_s < self.completed[1].inspection.elapsed_s
            and self.completed[-1].sampled_at < self.pause_action_at < self.returned[0].sampled_at
            and self.returned[0].inspection.elapsed_s == self.returned[1].inspection.elapsed_s
        ):
            raise ValueError("没有正常继续/完成/暂停的动作因果链")
        request = self.outbound_receipts[0].snapshot.get("request_id")
        for receipts, pair, position in (
            (self.outbound_receipts, self.outbound, self.target),
            (self.returned_receipts, self.returned, self.origin),
        ):
            for receipt, frame in zip(receipts, pair, strict=True):
                if (
                    receipt.frame_id != frame.frame_id
                    or receipt.snapshot.get("raw_motion_probe", {}).get("request_id") != request
                    or receipt.snapshot.get("raw_motion_probe", {}).get("sent_rows") != 0
                    or not received_gps_matches(
                        receipt.snapshot, request, receipt.android_observed_ns, position
                    )
                ):
                    raise ValueError("暂停访问缺少同任务零运动的新鲜双帧定位接收")
        return self


def _visit_page(view, *, require_gps=True):
    return (
        (view.package, view.activity, view.version, view.state, view.counter_label)
        == (TARGET, RUN_ACTIVITY, "4.3.0", "metrics", "已打卡")
        and (not require_gps or view.gps_label == GPS_READY_LABEL)
        and all(
            issue == "start_controls_and_current_record_identity_unverified"
            for issue in view.issues
        )
    )


def observe_checkpoint_once(connector, calibration, authorize, *, gps_request, evidence):
    """在已实际到达的暂停页继续最多10秒，确认两帧真实打卡后再正常暂停。"""
    old = CapturedRunFrame.model_validate(calibration["paused_frame"]).inspection
    target = GeoPoint.model_validate(calibration["command_target"])
    ready, began = [], connector.clock()
    while connector.clock() - began < 10 and authorize() is True:
        cap = connector.capture(motion_request=gps_request)
        v = cap.frame.inspection
        if (
            not _visit_page(v)
            or v.run_control_state != "paused"
            or v.checkpoint_cards != old.checkpoint_cards
            or (v.distance_m, v.elapsed_s, v.completed_checkpoints)
            != (old.distance_m, old.elapsed_s, old.completed_checkpoints)
        ):
            raise ValueError("打卡观察前暂停记录改变")
        if received_gps_matches(cap.motion_snapshot, gps_request, cap.android_observed_ns, target):
            if cap.motion_snapshot.get("raw_motion_probe", {}).get("sent_rows") != 0:
                raise ValueError("暂停访问期间不得发送运动")
            ready.append(cap)
            if len(ready) == 2:
                break
        connector.sleep(0.2)
    if len(ready) != 2:
        raise ValueError("目标点实收等待超时，不继续")
    button = connector.ui.device(resourceId=TARGET + ":id/tv_start_pause_continue_run")
    connector.bridge.check_device()
    if (
        button.count != 1
        or authorize() is not True
        or not 0 <= connector.clock() - ready[-1].frame.sampled_at < 2
    ):
        raise ValueError("继续前控件或新鲜许可未知")
    action_at = connector.clock()
    with (evidence / "continue-action.json").open("x", encoding="utf-8") as f:
        json.dump({"attempted_once": True, "action_at": action_at, "new_start": False}, f)
    observed = []
    pause_at = None
    final = None
    done = []
    try:
        button.click()
        began = connector.clock()
        while connector.clock() - began < 10 and authorize() is True:
            cap = connector.capture(motion_request=gps_request)
            v = cap.frame.inspection
            observed.append(cap.frame.model_dump(mode="json"))
            if "human_face_verification_required" in v.issues:
                raise ValueError("等待本人正常验证")
            if (
                not _visit_page(v)
                or v.run_control_state != "running"
                or v.distance_m is None
                or abs(v.distance_m - old.distance_m) > 10
                or v.elapsed_s is None
                or not old.elapsed_s <= v.elapsed_s <= old.elapsed_s + 30
                or cap.motion_snapshot is None
                or cap.motion_snapshot.get("request_id") != gps_request
                or cap.motion_snapshot.get("raw_motion_probe", {}).get("request_id") != gps_request
                or cap.motion_snapshot.get("raw_motion_probe", {}).get("sent_rows") != 0
            ):
                raise ValueError("跳距、运行页或零运动证据不一致")
            names = resume_completed_names(v)
            expected = tuple(c.name for c in old.checkpoint_cards if c.state == "completed") + (
                calibration["name"],
            )
            if v.completed_checkpoints == old.completed_checkpoints + 1 and names == expected:
                done.append(cap.frame)
                done = done[-2:]
                if len(done) == 2 and done[-1].inspection.elapsed_s > done[-2].inspection.elapsed_s:
                    break
            else:
                done = []
            connector.sleep(0.2)
    finally:
        cap = connector.capture()
        v = cap.frame.inspection
        if v.run_control_state == "running" and _visit_page(v, require_gps=False):
            connector.bridge.check_device()
            if (
                button.count != 1
                or authorize() is not True
                or not 0 <= connector.clock() - cap.frame.sampled_at < 2
            ):
                raise ValueError("访问后暂停许可未知")
            pause_at = connector.clock()
            with (evidence / "pause-action.json").open("x", encoding="utf-8") as f:
                json.dump({"attempted_once": True, "action_at": pause_at}, f)
            button.long_click(duration=2)
        final = connector.capture().frame
    if len(done) != 2 or pause_at is None or final.inspection.run_control_state != "paused":
        raise ValueError("原App未独立确认打卡并暂停，不返回或更新计数")
    result = {
        "observed": observed,
        "final": final.model_dump(mode="json"),
        "motion_started": False,
        "submitted": False,
        "continue_action_at": action_at,
        "pause_action_at": pause_at,
    }
    (evidence / "checkpoint-observation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    return result


class PausedCheckpointVisitor:
    def __init__(self, recovery, clip, start_day, *, on_observer=None):
        self.recovery, self.clip, self.start_day = recovery, clip, start_day
        self.on_observer = on_observer

    def visit(self, authorize):
        c, d = self.recovery.connector, self.recovery.dispatcher
        calibration = self.recovery.resolve(authorize, calibration_only=True)
        if calibration is None:
            raise ValueError("未取得独立中心标定")
        directory = c.evidence / f"paused-visit-{calibration['number']}"
        directory.mkdir()  # 同一序号不可重复发送访问动作。
        outbound_dir, returned_dir = directory / "outbound", directory / "return"
        outbound_dir.mkdir()
        returned_dir.mkdir()
        original = StartWitness.model_validate_json(
            (c.evidence / "start-witness.json").read_text("utf-8-sig")
        )
        motion = ForegroundMotionClient(c.bridge, bounded_stream=True, replay_passes=1)
        c.exclude_route_calibration = True
        try:
            motion.prepare(self.clip)
            if self.on_observer is not None:
                self.on_observer(motion)  # 正常清理也必须绑定最新的自有服务nonce。
            motion.preload(self.clip)
            out = relocate_paused_once(
                c,
                original,
                calibration,
                d.planner,
                outbound_dir,
                authorize,
                gps_request=motion.request,
                start_day=self.start_day(),
            )
            if not out["received_target_twice"]:
                raise ValueError("外跳实际接收未确认，不继续")
            visit = observe_checkpoint_once(
                c, calibration, authorize, gps_request=motion.request, evidence=directory
            )
            returned = return_to_pause_anchor_once(
                c,
                original,
                calibration,
                visit,
                d.planner,
                returned_dir,
                authorize,
                gps_request=motion.request,
                start_day=self.start_day(),
            )
            if not returned["received_target_twice"]:
                raise ValueError("返回原锚点实收未确认，不恢复路线")
            completed = [
                f
                for f in visit["observed"]
                if f["inspection"]["completed_checkpoints"] == calibration["number"]
            ][-2:]
            proof = PausedVisitWitness(
                original=original,
                origin=calibration["command_current"],
                target=calibration["command_target"],
                departure=out["before"],
                outbound=out["after"],
                completed=completed,
                returned=returned["after"],
                outbound_receipts=out["input_observations"],
                returned_receipts=returned["input_observations"],
                continue_action_at=visit["continue_action_at"],
                pause_action_at=visit["pause_action_at"],
            )
            (directory / "visit-witness.json").write_text(proof.model_dump_json(indent=2), "utf-8")
            d.accept_paused_visit(proof)
        finally:
            try:
                motion.stop()
                if motion.preload_requested:
                    owner = json.loads(
                        c.bridge.run(
                            "shell",
                            "run-as",
                            DIAGNOSTIC_PACKAGE,
                            "cat",
                            "files/motion-service.json",
                        )
                    )
                    if (
                        owner.get("request_id") != motion.request
                        or sensor_mode(c.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL
                    ):
                        raise ValueError("GPS观察服务归属或正常模式未知，不替换其他任务")
                    # STOP ACK不等于250ms快照线程已销毁；仅关闭已核对的自有观察服务，
                    # 否则紧接的新nonce PREPARE可能被旧probe拒绝。绝不重复STOP或START。
                    c.bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
            finally:
                c.exclude_route_calibration = False
