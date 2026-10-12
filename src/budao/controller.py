"""业务状态机：观察不可靠时暂停；只有明确打卡状态才能推进序号。"""

from __future__ import annotations

import math

from .models import Decision, GeoPoint, Observation
from .routes import distance


class RunController:
    def __init__(
        self,
        package: str = "com.lptiyu.tanke",
        minimum_distance_m: float = 1500,
        maximum_frame_age_s: float = 5,
        *,
        named_card_mode: bool = False,
    ):
        if (
            not math.isfinite(minimum_distance_m)
            or not math.isfinite(maximum_frame_age_s)
            or minimum_distance_m <= 0
            or maximum_frame_age_s <= 0
            or type(named_card_mode) is not bool
        ):
            raise ValueError("里程和帧有效期必须大于0")
        self.package = package
        self.minimum_distance_m = minimum_distance_m
        self.maximum_frame_age_s = maximum_frame_age_s
        self.named_card_mode = named_card_mode
        self.completed = 0
        self.active: GeoPoint | None = None
        self.arrived = False
        self.candidate: tuple[int, GeoPoint, str] | None = None
        self.submitted_record_id: str | None = None
        self.validated = False
        self.distance_m = 0.0
        self.last_time = -float("inf")
        self.completed_names: tuple[str, ...] = ()
        self.active_name: str | None = None
        self.candidate_name: str | None = None
        self.candidate_time = -math.inf
        self.completion_candidate: tuple[int, tuple[str, ...], str] | None = None
        self.finish_candidate: str | None = None
        self.finish_confirmed = False
        self.last_elapsed_s = 0
        self.seen_named_frames: set[str] = set()

    def _discard_pending(self) -> None:
        self.candidate = None
        self.candidate_name = None
        self.completion_candidate = None
        self.finish_candidate = None
        self.finish_confirmed = False

    def mark_arrived(self) -> None:
        if self.active is None:
            raise ValueError("没有当前目标")
        self.arrived = True

    def mark_submitted(self, record_id: str) -> None:
        if (
            self.completed != 3
            or self.distance_m < self.minimum_distance_m
            or not record_id
            or (self.named_card_mode and not self.finish_confirmed)
        ):
            raise ValueError("三点及App里程尚未达标，或缺少本次记录ID")
        self.submitted_record_id = record_id

    def step(self, observation: Observation, now: float) -> Decision:
        o = observation
        if o.package != self.package or o.screen in {"ad", "unknown"} or o.confidence < 0.8:
            self._discard_pending()
            return Decision(action="pause", reason="广告、跳转、未知界面或低置信度")
        if not 0 <= now - o.sampled_at <= self.maximum_frame_age_s:
            self._discard_pending()
            return Decision(action="pause", reason="观察帧过期或时间异常")
        if o.sampled_at < self.last_time:
            self._discard_pending()
            return Decision(action="pause", reason="观察时间倒退")
        if self.named_card_mode and not self.submitted_record_id:
            if (
                o.sampled_at <= self.last_time
                or not o.frame_id
                or o.frame_id in self.seen_named_frames
                or len(self.seen_named_frames) >= 12000
                or o.elapsed_s is None
                or o.elapsed_s <= 0
                or o.elapsed_s < self.last_elapsed_s
                or o.completed_checkpoint_names is None
                or len(o.completed_checkpoint_names) != o.completed_checkpoints
                or any(not name or name != name.strip() for name in o.completed_checkpoint_names)
            ):
                self._discard_pending()
                return Decision(
                    action="pause", reason="正式卡片帧重复、时间/用时异常或完成名称缺失"
                )
            self.seen_named_frames.add(o.frame_id)
            self.last_elapsed_s = o.elapsed_s
        self.last_time = o.sampled_at
        if self.submitted_record_id:
            if o.screen == "summary" and o.record_valid and o.record_id == self.submitted_record_id:
                self.validated = True
                return Decision(action="validated", reason="本次记录已明确有效")
            return Decision(action="wait", reason="等待本次记录有效状态，不重复提交")
        if o.screen != "running":
            self._discard_pending()
            return Decision(action="pause", reason="没有确认正在运行的界面")
        if o.distance_m + 10 < self.distance_m:
            self._discard_pending()
            return Decision(action="pause", reason="App里程明显回退，可能进入其他会话")
        if o.completed_checkpoints < self.completed:
            self._discard_pending()
            return Decision(action="pause", reason="打卡状态回退，可能进入其他会话")
        if o.completed_checkpoints > self.completed:
            if self.named_card_mode:
                names = o.completed_checkpoint_names
                if (
                    o.completed_checkpoints != self.completed + 1
                    or self.active_name is None
                    or names[:-1] != self.completed_names
                    or names[-1] != self.active_name
                ):
                    self._discard_pending()
                    return Decision(
                        action="pause", reason="完成卡片与本次已确认目标或历史顺序不一致"
                    )
                confirmation = self.completion_candidate
                if confirmation is None or confirmation[:2] != (o.completed_checkpoints, names):
                    self.completion_candidate = o.completed_checkpoints, names, o.frame_id
                    self.distance_m = max(self.distance_m, o.distance_m)
                    return Decision(
                        action="wait", reason="等待独立第二帧确认App打卡，不要求走到标记中心"
                    )
                self.completed_names = names
                self.completion_candidate = None
            elif o.completed_checkpoints != self.completed + 1 or not self.arrived:
                return Decision(action="pause", reason="未按逐点到达流程观察到打卡确认")
            self.completed += 1
            self.active = None
            self.active_name = None
            self.arrived = False
            self.candidate = None
        elif self.named_card_mode:
            self.completion_candidate = None
            if o.completed_checkpoint_names != self.completed_names:
                self._discard_pending()
                return Decision(action="pause", reason="已完成卡片名称改变，拒绝套用其他会话")
        self.distance_m = max(self.distance_m, o.distance_m)
        if self.named_card_mode and o.distance_m < self.minimum_distance_m:
            self.finish_candidate = None
            self.finish_confirmed = False
        if (
            self.completed == 3
            and self.distance_m >= self.minimum_distance_m
            and (not self.named_card_mode or o.distance_m >= self.minimum_distance_m)
        ):
            if self.named_card_mode and self.finish_candidate is None:
                self.finish_candidate = o.frame_id
                return Decision(action="wait", reason="三点已确认，等待第二帧复核App最低里程")
            self.finish_confirmed = True
            return Decision(action="finish_ready", reason="三点及App里程达标；尚未提交")
        if self.active is not None:
            if self.named_card_mode and (
                o.checkpoint_name != self.active_name
                or o.checkpoint_number != self.completed + 1
                or o.checkpoint is None
                or distance(o.checkpoint, self.active) > 5
            ):
                self._discard_pending()
                return Decision(action="pause", reason="当前已分配目标改变，不沿旧路线继续")
            if self.arrived:
                return Decision(action="wait", reason="已到达，但App尚未确认打卡")
            return Decision(action="navigate", reason="前往当前目标", target=self.active)
        if o.checkpoint_number is None or o.checkpoint is None:
            self.candidate = None
            return Decision(action="base_route", reason="按基线路线等待新打卡点")
        if o.checkpoint_number != self.completed + 1:
            self.candidate = None
            return Decision(action="pause", reason="目标序号异常，不前往后续或旧点")
        if self.named_card_mode and not o.checkpoint_name:
            self._discard_pending()
            return Decision(action="pause", reason="目标缺少完整正式卡片名称")
        candidate = self.candidate
        if (
            candidate
            and candidate[0] == o.checkpoint_number
            and candidate[2] != o.frame_id
            and distance(candidate[1], o.checkpoint) <= 5
            and (
                not self.named_card_mode
                or (self.candidate_name == o.checkpoint_name and o.sampled_at > self.candidate_time)
            )
        ):
            self.active = o.checkpoint
            self.active_name = o.checkpoint_name
            self.candidate = None
            return Decision(action="navigate", reason="两帧一致确认新目标", target=self.active)
        self.candidate = o.checkpoint_number, o.checkpoint, o.frame_id
        self.candidate_name, self.candidate_time = o.checkpoint_name, o.sampled_at
        return Decision(action="wait", reason="等待独立第二帧确认目标")
