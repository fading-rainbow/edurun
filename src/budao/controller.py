"""业务状态机：观察不可靠时暂停；只有明确打卡状态才能推进序号。"""

from __future__ import annotations

from .models import Decision, GeoPoint, Observation
from .routes import distance


class RunController:
    def __init__(
        self,
        package: str = "com.lptiyu.tanke",
        minimum_distance_m: float = 1500,
        maximum_frame_age_s: float = 5,
    ):
        if minimum_distance_m <= 0 or maximum_frame_age_s <= 0:
            raise ValueError("里程和帧有效期必须大于0")
        self.package = package
        self.minimum_distance_m = minimum_distance_m
        self.maximum_frame_age_s = maximum_frame_age_s
        self.completed = 0
        self.active: GeoPoint | None = None
        self.arrived = False
        self.candidate: tuple[int, GeoPoint, str] | None = None
        self.submitted_record_id: str | None = None
        self.validated = False
        self.distance_m = 0.0
        self.last_time = -float("inf")

    def mark_arrived(self) -> None:
        if self.active is None:
            raise ValueError("没有当前目标")
        self.arrived = True

    def mark_submitted(self, record_id: str) -> None:
        if self.completed != 3 or self.distance_m < self.minimum_distance_m or not record_id:
            raise ValueError("三点及App里程尚未达标，或缺少本次记录ID")
        self.submitted_record_id = record_id

    def step(self, observation: Observation, now: float) -> Decision:
        o = observation
        if o.package != self.package or o.screen in {"ad", "unknown"} or o.confidence < 0.8:
            self.candidate = None
            return Decision(action="pause", reason="广告、跳转、未知界面或低置信度")
        if not 0 <= now - o.sampled_at <= self.maximum_frame_age_s:
            return Decision(action="pause", reason="观察帧过期或时间异常")
        if o.sampled_at < self.last_time:
            return Decision(action="pause", reason="观察时间倒退")
        self.last_time = o.sampled_at
        if self.submitted_record_id:
            if o.screen == "summary" and o.record_valid and o.record_id == self.submitted_record_id:
                self.validated = True
                return Decision(action="validated", reason="本次记录已明确有效")
            return Decision(action="wait", reason="等待本次记录有效状态，不重复提交")
        if o.screen != "running":
            return Decision(action="pause", reason="没有确认正在运行的界面")
        if o.distance_m + 10 < self.distance_m:
            return Decision(action="pause", reason="App里程明显回退，可能进入其他会话")
        if o.completed_checkpoints < self.completed:
            return Decision(action="pause", reason="打卡状态回退，可能进入其他会话")
        if o.completed_checkpoints > self.completed:
            if o.completed_checkpoints != self.completed + 1 or not self.arrived:
                return Decision(action="pause", reason="未按逐点到达流程观察到打卡确认")
            self.completed += 1
            self.active = None
            self.arrived = False
            self.candidate = None
        self.distance_m = max(self.distance_m, o.distance_m)
        if self.completed == 3 and self.distance_m >= self.minimum_distance_m:
            return Decision(action="finish_ready", reason="三点及App里程达标；尚未提交")
        if self.active is not None:
            if self.arrived:
                return Decision(action="wait", reason="已到达，但App尚未确认打卡")
            return Decision(action="navigate", reason="前往当前目标", target=self.active)
        if o.checkpoint_number is None or o.checkpoint is None:
            self.candidate = None
            return Decision(action="base_route", reason="按基线路线等待新打卡点")
        if o.checkpoint_number != self.completed + 1:
            self.candidate = None
            return Decision(action="pause", reason="目标序号异常，不前往后续或旧点")
        candidate = self.candidate
        if (
            candidate
            and candidate[0] == o.checkpoint_number
            and candidate[2] != o.frame_id
            and distance(candidate[1], o.checkpoint) <= 5
        ):
            self.active = o.checkpoint
            self.candidate = None
            return Decision(action="navigate", reason="两帧一致确认新目标", target=self.active)
        self.candidate = o.checkpoint_number, o.checkpoint, o.frame_id
        return Decision(action="wait", reason="等待独立第二帧确认目标")
