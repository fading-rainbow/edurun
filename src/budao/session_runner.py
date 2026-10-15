"""现有开始、卡片调度、GPS与有界IMU组件的单次编排；没有结束/保存操作。"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable

from .android import DIAGNOSTIC_PACKAGE
from .foreground_motion import ForegroundMotionClient
from .gps_reception import received_gps_matches
from .raw_motion import RecordedProbeClip
from .runtime import RuntimeDispatcher
from .sensor_probe import INJECTION, NORMAL, sensor_mode
from .start_connector import GPSReadinessTimeout, StartConnector
from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET, TargetInspection
from .ui import KNOWN_AD_COMPONENT, dismiss_known_ad, focused_component


class SessionRunner:
    """只开始一份新会话；已有记录、失败开始或到期任务不被自动重试/接管。"""

    def __init__(
        self,
        connector: StartConnector,
        dispatcher: RuntimeDispatcher,
        motion: ForegroundMotionClient,
        clip: RecordedProbeClip,
        *,
        maximum_s: float = 900,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        prepare_start: Callable | None = None,
        refresh_gps: Callable | None = None,
        refresh_started_gps: Callable | None = None,
        wait_for_human: bool = False,
        resume_start: Callable | None = None,
        resolve_checkpoint: Callable | None = None,
        rollover_enabled: bool = False,
    ):
        if (
            not isinstance(connector, StartConnector)
            or not isinstance(dispatcher, RuntimeDispatcher)
            or not isinstance(motion, ForegroundMotionClient)
            or not isinstance(clip, RecordedProbeClip)
            or connector.bridge is not motion.bridge
            or dispatcher.worker.bridge is not motion.bridge
            or dispatcher.pace_range_s_per_km is None
            or not dispatcher.asynchronous_renewals
            or motion.bounded_stream is not True
            or type(maximum_s) not in (int, float)
            or not math.isfinite(maximum_s)
            or type(rollover_enabled) is not bool
            or (rollover_enabled and clip.duration_s * motion.replay_passes < 900)
            or not 0
            < maximum_s
            <= (1800 if rollover_enabled else min(900, clip.duration_s * motion.replay_passes))
            or dispatcher.started
            or dispatcher.worker.thread is not None
            or motion.started
            or motion.preload_requested
        ):
            raise ValueError("单次编排需要同设备新组件、明确配速/异步续期及有界实采片段")
        self.connector, self.dispatcher, self.motion, self.clip = (
            connector,
            dispatcher,
            motion,
            clip,
        )
        self.maximum_s, self.clock, self.sleep = maximum_s, clock, sleep
        if any(
            value is not None and not callable(value)
            for value in (prepare_start, refresh_gps, refresh_started_gps)
        ):
            raise ValueError("入口与同位置刷新必须是本机回调")
        self.prepare_start, self.refresh_gps = prepare_start, refresh_gps
        self.refresh_started_gps = refresh_started_gps
        if type(wait_for_human) is not bool:
            raise ValueError("本人等待选项必须明确")
        self.wait_for_human, self.original_request = wait_for_human, motion.request
        if resume_start is not None and not callable(resume_start):
            raise ValueError("续跑必须为本机绑定的专用回调")
        self.resume_start = resume_start
        if resolve_checkpoint is not None and not callable(resolve_checkpoint):
            raise ValueError("地图恢复必须是本机绑定的回调")
        self.resolve_checkpoint = resolve_checkpoint
        self.rollover_enabled = rollover_enabled
        self.used = False
        self.started = False
        self.lock = threading.Lock()

    def _eligible_motion(self, view: TargetInspection) -> bool:
        """只判断控件/顺序，不构造可用于驱动的观察帧或开始证据。"""
        c = self.dispatcher.controller
        cards = view.checkpoint_cards
        names = tuple(card.name for card in cards if card.state == "completed")
        allowed = getattr(
            self.dispatcher.observer, "allowed_names", self.dispatcher.observer.confirmed_names
        )
        if (
            view.state != "metrics"
            or view.run_control_state != "running"
            or view.gps_label != GPS_READY_LABEL
            or view.counter_label != "已打卡"
            or view.elapsed_s is None
            or view.elapsed_s <= 0
            or view.distance_m is None
            or not cards
            or view.completed_checkpoints not in {c.completed, c.completed + 1}
            or names[: c.completed] != c.completed_names
            or any(
                issue != "start_controls_and_current_record_identity_unverified"
                for issue in view.issues
            )
            or any(card.state != "pending" and card.name not in allowed for card in cards)
        ):
            return False
        if view.completed_checkpoints == c.completed + 1:
            return c.active_name is not None and names[-1] == c.active_name
        assigned = [card for card in cards if card.state == "assigned"]
        return c.active_name is None or (len(assigned) == 1 and assigned[0].name == c.active_name)

    def run(
        self, authorize: Callable[[], bool], publish: Callable[[dict], None], cancel=None
    ) -> dict:
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise ValueError("停止信号必须是本机Event，不能来自请求对象")
        if (
            not callable(authorize)
            or not callable(publish)
            or not self.lock.acquire(blocking=False)
        ):
            raise ValueError("需要明确单次开始许可及状态接收，不能重复运行")
        if self.used:
            self.lock.release()
            raise ValueError("本编排已经消费；失败/停止后也不能重试")
        self.used = True
        cancel = cancel if cancel is not None else threading.Event()
        report = {
            "state": "preparing",
            "message": "正在提前核验有界输入；尚未开始",
            "run_started": False,
            "records_submitted": 0,
            "record_valid_proven": False,
            "one_click_workflow_verified": False,
        }
        mode_changed = False
        try:
            publish(dict(report))
            if cancel.is_set() or authorize() is not True:
                raise ValueError("开始未获许可")
            if sensor_mode(self.motion.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
                raise ValueError("其他输入或未知模式尚未清理")
            if self.prepare_start is not None:
                self.prepare_start(lambda: not cancel.is_set() and authorize() is True)
            self.motion.prepare(self.clip)
            self.motion.preload(self.clip)
            report.update(
                message="正在等待原App定位就绪与本任务同位置实收；尚未点击开始",
                preparation_phase="gps_readiness",
            )
            publish(dict(report))
            gps_deadline = self.clock() + 10
            while self.resume_start is None and not cancel.is_set() and self.clock() < gps_deadline:
                if self.refresh_gps is not None:
                    self.refresh_gps(lambda: not cancel.is_set() and authorize() is True)
                cap = self.connector.capture(motion_request=self.motion.request)
                if cap.frame.inspection.gps_label == GPS_READY_LABEL and received_gps_matches(
                    cap.motion_snapshot,
                    self.motion.request,
                    cap.android_observed_ns,
                    self.dispatcher.worker.engine.position,
                ):
                    break
                self.sleep(0.1)
            else:
                if self.resume_start is not None:
                    pass  # Current received position is checked by the recovery factory.
                else:
                    raise GPSReadinessTimeout("开始前定位就绪与当前同位置接收未同时确认；不点击")
            start = self.resume_start or self.connector.begin_once
            witness = start(
                self.motion.request,
                lambda: not cancel.is_set() and authorize() is True,
                **(
                    {
                        "gps_position": self.dispatcher.worker.engine.position,
                        "refresh_gps": self.refresh_gps,
                    }
                    if self.resume_start is None
                    else {}
                ),
            )
            report.pop("preparation_phase", None)
            report["run_started"] = True
            self.started = True
            report.update(state="starting", message="原App新开始已确认，正在连接有界输入")
            publish(dict(report))
            if cancel.is_set():
                raise ValueError("新开始后收到停止请求；不打开输入模式")
            if self.resume_start is None:
                self.dispatcher.start(witness)
            else:
                self.dispatcher.resume(witness)
            mode_changed = True  # 超时也可能已经切换，清理时读取实际模式，不猜ACK结果。
            self.motion.bridge.run(
                "shell", "dumpsys", "sensorservice", "data_injection", DIAGNOSTIC_PACKAGE
            )
            if (
                sensor_mode(self.motion.bridge.run("shell", "dumpsys", "sensorservice"))
                != INJECTION
            ):
                raise ValueError("未确认自有输入模式")
            if self.refresh_started_gps is not None:
                self.refresh_started_gps(
                    witness, lambda: not cancel.is_set() and authorize() is True
                )
            initial = self.connector.capture(motion_request=self.motion.request)
            if not self._eligible_motion(initial.frame.inspection) or cancel.is_set():
                raise ValueError("开始后没有本次允许观察")
            self.motion.start(initial.android_observed_ns)
            deadline = self.clock() + self.maximum_s
            window_deadline = self.clock() + 600
            while not cancel.is_set() and self.clock() < deadline:
                if self.rollover_enabled and self.clock() >= window_deadline:
                    report.update(state="recovering", message="有界输入片段换窗；暂停并重新核对")
                    publish(dict(report))
                    waited = self._human_wait(cancel, authorize, pause_record=True)
                    if waited is None:
                        report.update(
                            state="paused",
                            message=(
                                "收到停止请求或达到有界运行时限"
                                if cancel.is_set()
                                else "换窗后运行页未确认；保留记录"
                            ),
                        )
                        break
                    deadline += waited
                    window_deadline = self.clock() + 600
                    continue
                try:
                    cap = self.connector.capture(
                        motion_request=self.motion.request,
                        renew_motion=self.motion,
                        authorize_motion=self._eligible_motion,
                    )
                except ValueError:
                    focus = focused_component(
                        self.motion.bridge.run("shell", "dumpsys", "window", "displays")
                    )
                    if focus != KNOWN_AD_COMPONENT:
                        raise
                    report.update(state="recovering", message="已知广告出现；停止输入并核对恢复")
                    publish(dict(report))
                    waited = self._human_wait(cancel, authorize)
                    if waited is None:
                        report.update(
                            state="paused",
                            message=(
                                "收到停止请求或达到有界运行时限"
                                if cancel.is_set()
                                else "广告后运行页未确认；保留记录"
                            ),
                        )
                        break
                    deadline += waited
                    window_deadline = self.clock() + 600
                    continue
                view = cap.frame.inspection
                if "human_face_verification_required" in view.issues:
                    report.update(
                        state="verification_required", message="原App要求本人验证；输入停止"
                    )
                    if self.wait_for_human:
                        publish(dict(report))
                        waited = self._human_wait(cancel, authorize)
                        if waited is not None:
                            deadline += waited
                            window_deadline = self.clock() + 600
                            continue
                    break
                unknown = [
                    card
                    for card in view.checkpoint_cards
                    if card.state == "assigned"
                    and card.name
                    not in getattr(
                        self.dispatcher.observer,
                        "allowed_names",
                        self.dispatcher.observer.confirmed_names,
                    )
                ]
                if unknown and self.resolve_checkpoint is not None:
                    report.update(state="recovering", message="未知打卡点；暂停输入后标定地图")
                    publish(dict(report))
                    waited = self._human_wait(
                        cancel, authorize, map_recovery=self.resolve_checkpoint
                    )
                    if waited is None:
                        report.update(
                            state="paused",
                            message=(
                                "收到停止请求或达到有界运行时限"
                                if cancel.is_set()
                                else "地图恢复未通过；保留记录"
                            ),
                        )
                        break
                    deadline += waited
                    window_deadline = self.clock() + 600
                    continue
                sender = cap.motion_snapshot
                if (
                    not sender
                    or sender["raw_motion_probe"]["request_id"] != self.motion.request
                    or sender["raw_motion_probe"]["clip_sha256"] != self.clip.sha256
                    or sender["raw_motion_probe"]["bounded_stream"]
                    is not self.motion.bounded_stream
                    or sender["raw_motion_probe"]["replay_passes"] != self.motion.replay_passes
                    or sender["raw_motion_probe"]["state"] not in {"running", "initializing"}
                ):
                    raise ValueError("本次输入来源/许可没有续接；不重启")
                decision = self.dispatcher.update(cap.frame)
                report.update(
                    state="running",
                    message="按当前卡片与声明通道运行；尚未提交",
                    distance_m=view.distance_m,
                    elapsed_s=view.elapsed_s,
                    completed_checkpoints=view.completed_checkpoints,
                )
                if decision.action == "finish_ready":
                    report.update(
                        state="finish_ready", message="三点、里程及配速已确认；等待正常结束"
                    )
                    break
                if decision.action == "pause":
                    report.update(state="paused", message="会话、控件或路线未通过；输入停止")
                    break
                publish(dict(report))
                self.sleep(0.05)
            else:
                report.update(state="paused", message="收到停止请求或达到有界运行时限")
        except Exception as error:
            self.private_failure = {"type": type(error).__name__, "detail": str(error)}
            report.pop("preparation_phase", None)
            if isinstance(error, GPSReadinessTimeout):
                report["preparation_phase"] = "gps_timeout"
            report.update(
                state="paused" if cancel.is_set() else "error",
                message=(
                    "定位就绪等待超时；未点击开始，未提交记录"
                    if isinstance(error, GPSReadinessTimeout)
                    else "单次编排中断；不重复开始，不回显私有异常"
                ),
            )
        finally:
            try:
                self.dispatcher.stop()
            except Exception:
                report["cleanup_unconfirmed"] = True
            try:
                self.motion.stop()
            except Exception:
                report["cleanup_unconfirmed"] = True
            try:
                if mode_changed:
                    state = json.loads(
                        self.motion.bridge.run(
                            "shell",
                            "run-as",
                            DIAGNOSTIC_PACKAGE,
                            "cat",
                            "files/motion-service.json",
                        )
                    )
                    if state.get("request_id") != self.motion.request:
                        raise ValueError("模式所有者已经变化")
                    actual_mode = sensor_mode(
                        self.motion.bridge.run("shell", "dumpsys", "sensorservice")
                    )
                    if actual_mode == INJECTION:
                        self.motion.bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
                        self.motion.bridge.run("shell", "dumpsys", "sensorservice", "enable")
                    elif actual_mode != NORMAL:
                        raise ValueError("实际模式不是本任务客户端，不覆盖")
                report["mode_restored_normal"] = (
                    sensor_mode(self.motion.bridge.run("shell", "dumpsys", "sensorservice"))
                    == NORMAL
                )
            except Exception:
                report["cleanup_unconfirmed"] = True
            report["start_action_attempted"] = (
                self.connector.last_action_request == self.original_request
            )
            if self.connector.last_action_request == self.original_request:
                try:
                    cap = self.connector.capture()
                    current = cap.frame.inspection
                    if "human_face_verification_required" in current.issues and not cancel.is_set():
                        report.update(
                            state="verification_required",
                            message="原App要求本人验证；自动输入已停止",
                        )
                    if current.run_control_state == "running":
                        button = self.connector.ui.device(
                            resourceId=TARGET + ":id/tv_start_pause_continue_run"
                        )
                        if button.count != 1:
                            raise ValueError("暂停控件不唯一")
                        self.connector.bridge.check_device()
                        if not 0 <= self.clock() - cap.frame.sampled_at <= 2:
                            raise ValueError("暂停前观察已经过期")
                        button.long_click(duration=2)
                        report["app_pause_confirmed"] = (
                            self.connector.capture().frame.inspection.run_control_state == "paused"
                        )
                    elif current.run_control_state == "paused":
                        report["app_pause_confirmed"] = True
                except Exception:
                    report["app_pause_unconfirmed"] = True
            self.lock.release()
        publish(dict(report))
        return report

    def _human_wait(self, cancel, authorize, *, map_recovery=None, pause_record=False):
        """等待用户正常验证，不点击验证、读取摄像头或提供照片。只续接自己的新会话。"""
        began, bridge = self.clock(), self.motion.bridge
        self.dispatcher.worker.pause("本人验证；不移动")
        self.motion.stop()
        owner = json.loads(
            bridge.run("shell", "run-as", DIAGNOSTIC_PACKAGE, "cat", "files/motion-service.json")
        )
        if owner.get("request_id") != self.motion.request:
            raise ValueError("运动所有者已改变")
        bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
        bridge.run("shell", "dumpsys", "sensorservice", "enable")
        if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
            raise ValueError("本人等待时正常模式未确认")
        if map_recovery is not None or pause_record:
            cap = self.connector.capture()
            if cap.frame.inspection.run_control_state == "running":
                button = self.connector.ui.device(
                    resourceId=TARGET + ":id/tv_start_pause_continue_run"
                )
                if button.count != 1 or not 0 <= self.clock() - cap.frame.sampled_at < 2:
                    raise ValueError("地图恢复前暂停控件或时效未知")
                bridge.check_device()
                button.long_click(duration=2)
                self.sleep(0.2)
            if map_recovery is not None:
                try:
                    map_recovery(authorize)
                except ValueError:
                    return None
        resumed = False
        while not cancel.is_set() and authorize() is True and self.clock() - began < 600:
            focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
            if focus == KNOWN_AD_COMPONENT:
                dismiss_known_ad(bridge, self.connector.ui, authorize=authorize)
                self.sleep(0.2)
                continue
            if focus == (TARGET, RUN_ACTIVITY):
                cap = self.connector.capture()
                view = cap.frame.inspection
                if "human_face_verification_required" not in view.issues:
                    if view.run_control_state == "paused" and not resumed:
                        button = self.connector.ui.device(
                            resourceId=TARGET + ":id/tv_start_pause_continue_run"
                        )
                        if button.count != 1:
                            raise ValueError("原App继续控件未知")
                        bridge.check_device()
                        if not 0 <= self.clock() - cap.frame.sampled_at <= 2:
                            raise ValueError("继续前观察已过期")
                        button.click()
                        resumed = True
                    elif self._eligible_motion(view):
                        self.motion = ForegroundMotionClient(
                            bridge, bounded_stream=True, replay_passes=6
                        )
                        self.motion.prepare(self.clip)
                        self.motion.preload(self.clip)
                        bridge.run(
                            "shell",
                            "dumpsys",
                            "sensorservice",
                            "data_injection",
                            DIAGNOSTIC_PACKAGE,
                        )
                        if (
                            sensor_mode(bridge.run("shell", "dumpsys", "sensorservice"))
                            != INJECTION
                        ):
                            raise ValueError("新有界输入模式未确认")
                        fresh = self.connector.capture(motion_request=self.motion.request)
                        if not self._eligible_motion(fresh.frame.inspection):
                            raise ValueError("本人验证后运行页未确认")
                        self.motion.start(fresh.android_observed_ns)
                        return self.clock() - began
            self.sleep(0.3)
        return None
