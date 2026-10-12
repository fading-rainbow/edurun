"""组合观察、顺序卡片状态机、GIS与MuMu时钟线程。没有开始按钮或上传点击。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from pydantic import Field, model_validator

from .card_observer import NamedCardObserver
from .controller import RunController
from .models import Decision, GeoPoint, StrictModel
from .mumu_driver import MuMuPositionWorker
from .routes import RoutePlanner, distance
from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET, TargetInspection


class CapturedRunFrame(StrictModel):
    frame_id: str = Field(min_length=1, max_length=128)
    sampled_at: float = Field(ge=0)
    inspection: TargetInspection


class StartWitness(StrictModel):
    """真实开始适配器须提供的因果证据，不得用旧截图/按钮组合自动填写。"""

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    serial: str = Field(pattern=r"^127\.0\.0\.1:[1-9]\d{3,4}$")
    action_at: float = Field(ge=0)
    # 当前UI文字没有可访问字段；实际开始文字/非倒计时证据尚须适配器采集。
    start_ui_verified: bool = False
    before: CapturedRunFrame
    first_running: CapturedRunFrame
    second_running: CapturedRunFrame

    @model_validator(mode="after")
    def causal_beginning(self):
        frames = (self.before, self.first_running, self.second_running)
        if (
            self.start_ui_verified is not True
            or len({f.frame_id for f in frames}) != 3
            or not self.before.sampled_at < self.action_at < self.first_running.sampled_at
            or not self.first_running.sampled_at < self.second_running.sampled_at
            or self.second_running.sampled_at - self.before.sampled_at > 15
        ):
            raise ValueError("缺少独立的新会话开始因果证据；不从旧运行页推断开始")
        for frame in frames:
            v = frame.inspection
            if (
                v.package != TARGET
                or v.activity != RUN_ACTIVITY
                or v.version != "4.3.0"
                or v.state != "metrics"
                or v.counter_label != "已打卡"
                or v.gps_label != GPS_READY_LABEL
                or v.completed_checkpoints != 0
                or v.distance_m is None
                or not 0 <= v.distance_m <= 25
                or len(v.checkpoint_cards) != 1
                or v.checkpoint_cards[0].number != 1
                or v.checkpoint_cards[0].state != "pending"
                or any(
                    issue != "start_controls_and_current_record_identity_unverified"
                    for issue in v.issues
                )
            ):
                raise ValueError("开始前后不是一致的正式初始会话")
        before, first, second = (f.inspection for f in frames)
        if (
            before.elapsed_s != 0
            or before.distance_m != 0
            or before.run_control_state != "not_started_or_countdown"
            or first.run_control_state != "running"
            or second.run_control_state != "running"
            or first.elapsed_s is None
            or second.elapsed_s is None
            or not 0 < first.elapsed_s < second.elapsed_s <= 10
            or not first.distance_m <= second.distance_m
        ):
            raise ValueError("未观察到零状态→新开始→递增计时的独立两帧")
        return self


class RuntimeDispatcher:
    def __init__(
        self,
        planner: RoutePlanner,
        observer: NamedCardObserver,
        worker: MuMuPositionWorker,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(worker, MuMuPositionWorker) or worker.engine.planner is not planner:
            raise ValueError("调度器须与同一个受守卫的MuMu路网/位置核心配对")
        self.planner, self.observer, self.worker, self.clock = planner, observer, worker, clock
        self.controller = RunController(
            minimum_distance_m=planner.config.minimum_distance_m + planner.config.margin_m,
            maximum_frame_age_s=2,
            named_card_mode=True,
        )
        self.started = False
        self.finish_ready = False
        self.route_key: tuple[str, GeoPoint | None] | None = None

    def start(self, witness: StartWitness) -> None:
        if not isinstance(witness, StartWitness):
            raise ValueError("缺少本次新会话的开始证据")
        # frozen模型的model_copy(update=...)仍可能跳过校验，不能因此绕过开始证据。
        witness = StartWitness.model_validate(witness.model_dump())
        now = self.clock()
        if (
            self.started
            or witness.serial != self.worker.bridge.serial
            or not math.isfinite(now)
            or not 0 <= now - witness.second_running.sampled_at <= 2
        ):
            raise ValueError("开始证据过期/设备不匹配/会话已启动，拒绝接管旧会话")
        self.worker.start()
        if not 0 <= self.clock() - witness.second_running.sampled_at <= 2:
            self.worker.stop()
            raise ValueError("线程启动检查耗时过长；开始证据已过期，未授权移动")
        self.controller.last_time = witness.second_running.sampled_at
        self.controller.last_elapsed_s = witness.second_running.inspection.elapsed_s
        self.controller.distance_m = witness.second_running.inspection.distance_m
        self.controller.seen_named_frames.update(
            f.frame_id for f in (witness.before, witness.first_running, witness.second_running)
        )
        self.started = True

    def update(self, frame: CapturedRunFrame) -> Decision:
        if not self.started:
            self.worker.pause("尚未确认本次新会话开始")
            return Decision(action="pause", reason="当前会话开始关卡未通过")
        if "human_face_verification_required" in frame.inspection.issues:
            self.worker.pause("需要本人验证，不能用照片就绪或旧成绩替代验证结果")
            return Decision(action="pause", reason="等待本人完成App要求的验证")
        if self.finish_ready:
            self.worker.pause("里程/三点已确认；未上传")
            return Decision(action="finish_ready", reason="等待正常结束/提交确认，不继续移动")
        observation = self.observer.observe(frame.inspection, frame.sampled_at, frame.frame_id)
        decision = self.controller.step(observation, self.clock())
        if decision.action not in {"navigate", "base_route"}:
            self.worker.pause(decision.reason)
            self.finish_ready = decision.action == "finish_ready"
            return decision
        try:
            state = self.worker.check()
            position = state.position
            key = decision.action, decision.target
            if decision.action == "navigate" and distance(position, decision.target) <= 0.05:
                self.controller.mark_arrived()  # 仅几何到达，完成仍需App卡片确认两帧。
                self.worker.pause("接近位置已到达，等待App打卡确认")
                return Decision(action="wait", reason="几何到达不等于打卡成功")
            if self.route_key == key and not state.path_done:
                self.worker.renew(frame.sampled_at)
                return decision
            if decision.action == "navigate":
                planned = self.planner.plan(position, decision.target)
                path = (position,) + planned[1:]
                repeat = False
            elif distance(position, self.planner.config.baseline[0]) <= 0.05:
                path = (position,) + self.planner.config.baseline[1:]
                repeat = True
            else:
                # 先沿已声明通道连接等待路线，不能在换路线时跳回起点。
                planned = self.planner.plan(position, self.planner.config.baseline[0])
                path = (position,) + planned[1:]
                repeat = False
            self.worker.set_path(path, frame.sampled_at, repeat=repeat)
            self.route_key = key
            return decision
        except (ValueError, RuntimeError):
            self.worker.pause("路网/驱动/观察许可未通过，等待重新核对")
            return Decision(action="pause", reason="路线不连通或驱动/新观察未就绪")

    def stop(self):
        self.worker.stop()
        self.started = False
