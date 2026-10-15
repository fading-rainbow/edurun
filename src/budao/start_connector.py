"""实际零状态/像素/单次开始动作→两帧因果证据；不移动、不上传或重试旧会话。"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from .foreground_motion import ForegroundMotionClient
from .gps_reception import received_gps_matches
from .models import GeoPoint
from .mumu import MuMuBridge
from .runtime import CapturedRunFrame, StartWitness
from .start_button import StartButtonTemplate
from .target import (
    GPS_READY_LABEL,
    RUN_ACTIVITY,
    TARGET,
    TargetInspection,
    face_verification_required,
    parse_checkpoint_cards,
    parse_run_control_state,
    parse_run_metrics,
)
from .ui import AppUI, focused_component

CAPTURE_GUARD = "dumpsys window displays; echo __EDURUN_PACKAGE__; dumpsys package com.lptiyu.tanke"
CAPTURE_MOTION_TAIL = (
    "dumpsys window displays; echo __EDURUN_CLOCK__; cat /proc/uptime; "
    "echo __EDURUN_MOTION__; run-as com.edurun.diagnostic cat files/motion-service.json; "
    "echo __EDURUN_AFTER_CLOCK__; cat /proc/uptime"
)


class GPSReadinessTimeout(ValueError):
    """固定就绪失败类型；不能把设备/账号异常原文发回公共报告。"""


@dataclass(frozen=True)
class RunCapture:
    frame: CapturedRunFrame
    hierarchy: str
    screenshot: Image.Image | None
    android_observed_ns: int | None = None
    motion_snapshot: dict | None = None


class StartConnector:
    def __init__(
        self,
        bridge: MuMuBridge,
        template: StartButtonTemplate,
        *,
        ui_factory: Callable = AppUI,
        clock: Callable = time.monotonic,
        sleep: Callable = time.sleep,
    ):
        if not isinstance(bridge, MuMuBridge):
            raise ValueError("开始连接器只能使用本项目独立MuMu")
        self.bridge, self.template, self.ui_factory = bridge, template, ui_factory
        self.clock, self.sleep, self.ui = clock, sleep, None
        self.used_requests: set[str] = set()
        self.last_action_request: str | None = None

    def capture(
        self,
        *,
        pixels: bool = False,
        motion_request: str | None = None,
        renew_motion: ForegroundMotionClient | None = None,
        authorize_motion: Callable[[TargetInspection], bool] | None = None,
        gps_position: GeoPoint | None = None,
    ) -> RunCapture:
        if motion_request is not None and (
            not isinstance(motion_request, str) or not re.fullmatch(r"[a-f0-9]{32}", motion_request)
        ):
            raise ValueError("运动观察只接受本次独立任务标记，不接受命令或认证信息")
        if renew_motion is not None and (
            not isinstance(renew_motion, ForegroundMotionClient)
            or renew_motion.bridge is not self.bridge
            or motion_request != renew_motion.request
            or not callable(authorize_motion)
            or pixels
        ):
            raise ValueError("合并续期需要同一设备任务、明确帧判断，不能与慢速像素读取混用")
        if gps_position is not None and (
            not isinstance(gps_position, GeoPoint) or renew_motion is None
        ):
            raise ValueError("接收定位守卫须绑定同一运动任务和实际已确认位置")
        expected = (TARGET, RUN_ACTIVITY)
        # 同一守卫内批量只读，减少每帧重复管理器查询；没有省略设备、版本或焦点检查。
        guard = self.bridge.run("shell", CAPTURE_GUARD)
        focus, marker, meta = guard.partition("__EDURUN_PACKAGE__")
        if focused_component(focus) != expected:
            raise ValueError("当前不是目标运行页；不读取登录/密码/摄像头界面")
        if (
            not marker
            or not re.search(r"^\s*versionName=4\.3\.0\s*$", meta, re.M)
            or not re.search(r"^\s*versionCode=230\b", meta, re.M)
        ):
            raise ValueError("目标版本尚未采集，不读取或点击控件")
        if self.ui is None:
            self.ui = self.ui_factory(self.bridge)
            # 初次连接UI服务可能较慢，连接完成后重新核对；已连接帧不重复构造。
            if (
                focused_component(self.bridge.run("shell", "dumpsys", "window", "displays"))
                != expected
            ):
                raise ValueError("读取前前台发生变化")
        tree = self.ui.device.dump_hierarchy()
        sampled = self.clock()
        if len(tree) > 2 * 1024 * 1024 or "<!DOCTYPE" in tree.upper() or "<!ENTITY" in tree.upper():
            raise ValueError("未知界面树")
        face_required = face_verification_required(tree)
        image = self.ui.screenshot() if pixels and not face_required else None
        if face_required:
            view = TargetInspection(
                package=TARGET,
                activity=RUN_ACTIVITY,
                version="4.3.0",
                issues=("human_face_verification_required",),
            )
        else:
            metrics = parse_run_metrics(tree)
            try:
                cards = parse_checkpoint_cards(tree)
            except ValueError:
                cards = ()
            view = TargetInspection(
                package=TARGET,
                activity=RUN_ACTIVITY,
                version="4.3.0",
                state="metrics",
                checkpoint_cards=cards,
                run_control_state=parse_run_control_state(tree),
                **metrics,
            )
        android_ns, motion = None, None
        if motion_request is None:
            tail = self.bridge.run("shell", "dumpsys", "window", "displays")
        else:
            tail_command = CAPTURE_MOTION_TAIL
            if renew_motion is not None:
                if (
                    view.state == "metrics"
                    and view.run_control_state in {"running", "paused"}
                    and (
                        view.gps_label == GPS_READY_LABEL
                        or (gps_position is not None and view.gps_label == "GPS信号弱")
                    )
                    and bool(view.checkpoint_cards)
                    and 0 <= self.clock() - sampled < 2
                    and authorize_motion(view) is True
                ):
                    tail_command = renew_motion.renewal_observation_command(
                        gps_position=gps_position
                    )
            if tail_command == CAPTURE_MOTION_TAIL:
                value = self.bridge.run("shell", tail_command)
            else:
                value = self.bridge.run(
                    "shell", tail_command, authorize=lambda: 0 <= self.clock() - sampled < 2
                )
            tail, clock_marker, rest = value.partition("__EDURUN_CLOCK__")
            uptime, motion_marker, state = rest.partition("__EDURUN_MOTION__")
            if (
                not clock_marker
                or not motion_marker
                or not re.fullmatch(r"\s*\d+\.\d{2}\s+\d+\.\d{2}\s*", uptime)
            ):
                raise ValueError("共享观察缺少实际Android单调时钟；不续期")
            seconds, decimals = uptime.split()[0].split(".")
            android_ns = int(seconds) * 1_000_000_000 + int(decimals) * 10_000_000
            state, after_marker, after_clock = state.partition("__EDURUN_AFTER_CLOCK__")
            if after_marker:
                if not re.fullmatch(r"\s*\d+\.\d{2}\s+\d+\.\d{2}\s*", after_clock):
                    raise ValueError("接收定位结束时钟格式未知")
                seconds, decimals = after_clock.split()[0].split(".")
                after_ns = int(seconds) * 1_000_000_000 + int(decimals) * 10_000_000
                if not 0 <= after_ns - android_ns <= 2_000_000_000:
                    raise ValueError("接收定位结束时钟倒退或读取过慢")
                android_ns = after_ns
            elif gps_position is not None:
                raise ValueError("接收定位缺少实际结束时钟；不授权位置输出")
            try:
                candidate = json.loads(state) if len(state) <= 2 * 1024 * 1024 else None
                if (
                    isinstance(candidate, dict)
                    and candidate.get("package") == "com.edurun.diagnostic"
                    and candidate.get("request_id") == motion_request
                ):
                    motion = candidate
            except ValueError:
                pass  # 无状态/旧任务不是本次运动已启动证据。
        if focused_component(tail) != expected:
            raise ValueError("读取过程中前台发生变化，不接受背景控件")
        return RunCapture(
            CapturedRunFrame(frame_id=secrets.token_hex(16), sampled_at=sampled, inspection=view),
            tree,
            image,
            android_ns,
            motion,
        )

    def prepare_begin(
        self,
        request_id: str,
        authorize: Callable[[], bool],
        *,
        gps_position: GeoPoint | None = None,
        refresh_gps: Callable | None = None,
    ) -> RunCapture:
        """只等待真实零状态和定位就绪，不消费开始请求、不点击或输出运动。"""
        if (
            not re.fullmatch(r"[a-f0-9]{32}", request_id)
            or not callable(authorize)
            or (gps_position is not None and not isinstance(gps_position, GeoPoint))
            or (refresh_gps is not None and (gps_position is None or not callable(refresh_gps)))
        ):
            raise ValueError("开始准备需要明确任务、同位置和许可")
        deadline = self.clock() + 10
        while self.clock() < deadline:
            if authorize() is not True:
                raise ValueError("本次运行准备条件/用户开始动作尚未确认")
            if refresh_gps is not None:
                refresh_gps(lambda: self.clock() < deadline and authorize() is True)
            before = self.capture(
                pixels=True, **({"motion_request": request_id} if gps_position is not None else {})
            )
            v = before.frame.inspection
            if (
                len(v.checkpoint_cards) != 1
                or v.checkpoint_cards[0].state != "pending"
                or not self.template.matches(v, before.screenshot, before.hierarchy)
            ):
                raise ValueError("当前不是已核对的正式零状态/开始按钮；未点击")
            received = gps_position is None or received_gps_matches(
                before.motion_snapshot, request_id, before.android_observed_ns, gps_position
            )
            if v.gps_label == GPS_READY_LABEL and received:
                if self.clock() >= deadline or authorize() is not True:
                    raise ValueError("开始准备观察或许可已经过期；未点击")
                return before
            if v.gps_label not in {GPS_READY_LABEL, "GPS信号弱"}:
                raise ValueError("当前定位状态未知；未点击")
            self.sleep(0.2)
        raise GPSReadinessTimeout("开始前GPS就绪与新任务实收未同时确认；未点击")

    def begin_once(
        self,
        request_id: str,
        authorize: Callable[[], bool],
        *,
        gps_position: GeoPoint | None = None,
        refresh_gps: Callable | None = None,
    ) -> StartWitness:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id) or request_id in self.used_requests:
            raise ValueError("开始请求无效或已经消费；不自动重复点击/接管旧会话")
        self.used_requests.add(request_id)
        if not callable(authorize) or authorize() is not True:
            raise ValueError("本次运行准备条件/用户开始动作尚未确认")
        if focused_component(self.bridge.run("shell", "dumpsys", "window", "displays")) != (
            TARGET,
            RUN_ACTIVITY,
        ):
            raise ValueError("动作前前台改变；未点击")
        before = self.prepare_begin(
            request_id, authorize, gps_position=gps_position, refresh_gps=refresh_gps
        )
        v = before.frame.inspection
        if (
            v.gps_label != GPS_READY_LABEL
            or len(v.checkpoint_cards) != 1
            or v.checkpoint_cards[0].state != "pending"
            or not self.template.matches(v, before.screenshot, before.hierarchy)
        ):
            raise ValueError("当前不是已核对的正式零状态/开始按钮；未点击")
        # capture在像素读取后，已通过受完整设备守卫的尾查询再次核对唯一前台；
        # 不在新鲜帧后重复同一设备/前台查询，避免仅重复IO耗尽两秒许可。
        now = self.clock()
        if not 0 <= now - before.frame.sampled_at <= 2 or authorize() is not True:
            raise ValueError("动作前准备许可或观察已过期；未点击")
        button = self.ui.device(resourceId=TARGET + ":id/tv_start_pause_continue_run")
        if button.count != 1:
            raise ValueError("开始控件不唯一；未点击")
        if not 0 <= self.clock() - before.frame.sampled_at <= 2 or authorize() is not True:
            raise ValueError("读取控件后许可或观察已过期；未点击")
        action_at = self.clock()
        self.last_action_request = request_id  # 点击报错也可能已生效；仅用于保守清理。
        button.click()  # 实测4.3.0开始为一次点击，暂停才是长按；失败不重发。
        first = None
        deadline = before.frame.sampled_at + 15
        while self.clock() < deadline:
            self.sleep(0.25)
            frame = self.capture().frame
            view = frame.inspection
            if "human_face_verification_required" in view.issues:
                raise ValueError("开始后需要本人验证；未授权移动或上传")
            if (
                view.run_control_state != "running"
                or not view.elapsed_s
                or view.gps_label != GPS_READY_LABEL
            ):
                continue
            if first is None:
                first = frame
            elif view.elapsed_s > first.inspection.elapsed_s:
                return StartWitness(
                    request_id=request_id,
                    serial=self.bridge.serial,
                    action_at=action_at,
                    start_ui_verified=True,
                    before=before.frame,
                    first_running=first,
                    second_running=frame,
                )
        raise ValueError("单次点击后缺少独立两帧开始证据；不重试、不移动或上传")
