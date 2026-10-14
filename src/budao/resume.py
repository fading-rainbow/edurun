"""Explicit recovery of this assistant's paused, zero-checkpoint session only."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import Field, model_validator

from .models import StrictModel
from .runtime import CapturedRunFrame, StartWitness
from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET
from .ui import focused_component


class ResumeWitness(StrictModel):
    original: StartWitness
    saved_pause: CapturedRunFrame
    first_paused: CapturedRunFrame
    second_paused: CapturedRunFrame
    action_at: float = Field(ge=0)
    first_running: CapturedRunFrame
    second_running: CapturedRunFrame

    @property
    def frames(self):
        return (self.first_paused, self.second_paused, self.first_running, self.second_running)

    @model_validator(mode="after")
    def consistent_session(self):
        saved = self.saved_pause.inspection
        if (
            saved.package != TARGET
            or saved.activity != RUN_ACTIVITY
            or saved.version != "4.3.0"
            or saved.state != "metrics"
            or saved.elapsed_s is None
            or saved.elapsed_s <= 0
            or saved.distance_m is None
            or self.original.second_running.sampled_at >= self.saved_pause.sampled_at
        ):
            raise ValueError("暂停凭据不是原生开始后的本次运行页")
        previous = self.saved_pause.sampled_at
        ids = {self.saved_pause.frame_id}
        for frame in self.frames:
            v = frame.inspection
            if (
                frame.frame_id in ids
                or frame.sampled_at <= previous
                or v.package != TARGET
                or v.activity != RUN_ACTIVITY
                or v.version != "4.3.0"
                or v.state != "metrics"
                or v.gps_label != GPS_READY_LABEL
                or v.counter_label != "已打卡"
                or v.completed_checkpoints != 0
                or v.distance_m != saved.distance_m
                or v.checkpoint_cards != saved.checkpoint_cards
                or v.elapsed_s is None
                or v.elapsed_s < saved.elapsed_s
                or any(
                    i != "start_controls_and_current_record_identity_unverified" for i in v.issues
                )
            ):
                raise ValueError("续跑只接受原生开始证据绑定的同一零打卡暂停记录")
            ids.add(frame.frame_id)
            previous = frame.sampled_at
        if (
            saved.run_control_state != "paused"
            or saved.completed_checkpoints != 0
            or len(saved.checkpoint_cards) != 1
            or saved.checkpoint_cards[0].state not in {"pending", "assigned"}
            or any(
                f.inspection.run_control_state != "paused"
                or f.inspection.elapsed_s != saved.elapsed_s
                for f in self.frames[:2]
            )
            or any(f.inspection.run_control_state != "running" for f in self.frames[2:])
            or not self.second_paused.sampled_at < self.action_at < self.first_running.sampled_at
            or not self.first_running.inspection.elapsed_s
            < self.second_running.inspection.elapsed_s
            <= saved.elapsed_s + 10
            or self.second_running.sampled_at - self.first_paused.sampled_at > 15
        ):
            raise ValueError("没有暂停两帧→一次继续→递增用时两帧的因果证据")
        return self


def resume_once(connector, original, saved, stamp: datetime, authorize: Callable):
    """No new Start, GPS movement, biometric handling, or submission here."""
    b = connector.bridge
    if not isinstance(original, StartWitness) or not isinstance(saved, CapturedRunFrame):
        raise ValueError("续跑只接受已验证的本机凭据")
    original = StartWitness.model_validate(original.model_dump())
    saved = CapturedRunFrame.model_validate(saved.model_dump())
    if b.run("shell", "date", "+%F").strip() != stamp.strftime("%Y-%m-%d"):
        raise ValueError("旧会话跨日，不续跑")
    if original.request_id in connector.used_requests or authorize() is not True:
        raise ValueError("续跑请求已消费或未授权")
    first, second = connector.capture().frame, connector.capture().frame
    # Check all immutable pause fields before any action. Running evidence is
    # deliberately NOT synthesized to run the full witness validator early.
    for frame in (first, second):
        view, old = frame.inspection, saved.inspection
        if (
            frame.sampled_at <= saved.sampled_at
            or view.run_control_state != "paused"
            or view.elapsed_s != old.elapsed_s
            or view.distance_m != old.distance_m
            or view.completed_checkpoints != 0
            or view.checkpoint_cards != old.checkpoint_cards
            or view.gps_label != GPS_READY_LABEL
            or any(
                i != "start_controls_and_current_record_identity_unverified" for i in view.issues
            )
        ):
            raise ValueError("当前记录不匹配本次原生暂停凭据；未继续")
    b.check_device()
    if (
        not 0 <= connector.clock() - second.sampled_at < 2
        or authorize() is not True
        or focused_component(b.run("shell", "dumpsys", "window", "displays"))
        != (TARGET, RUN_ACTIVITY)
    ):
        raise ValueError("继续前新观察或前台已过期")
    button = connector.ui.device(resourceId=TARGET + ":id/tv_start_pause_continue_run")
    if button.count != 1:
        raise ValueError("继续控件不唯一")
    connector.used_requests.add(original.request_id)
    marker: Path = connector.evidence / "resume-action-attempted.json"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write('{"normal_continue_attempted":true,"retry_allowed":false}')
    connector.last_action_request = original.request_id
    action_at = connector.clock()
    button.click()
    running = None
    while connector.clock() - first.sampled_at < 15:
        connector.sleep(0.2)
        frame = connector.capture().frame
        if frame.inspection.run_control_state == "running":
            if running is None:
                running = frame
            elif frame.inspection.elapsed_s > running.inspection.elapsed_s:
                proof = ResumeWitness(
                    original=original,
                    saved_pause=saved,
                    first_paused=first,
                    second_paused=second,
                    action_at=action_at,
                    first_running=running,
                    second_running=frame,
                )
                (connector.evidence / "resume-witness.json").write_text(
                    proof.model_dump_json(indent=2), "utf-8"
                )
                return proof
    raise ValueError("继续动作后尚未确认本次递增用时；不重试")
