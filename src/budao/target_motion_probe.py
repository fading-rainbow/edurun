"""目标暂停页前台保持的短IMU诊断；不开始、移动、生成步数或提交记录。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from .android import DIAGNOSTIC_PACKAGE
from .foreground_motion import RECEIVER, ForegroundMotionClient
from .mumu import MuMuBridge
from .raw_motion import RecordedProbeClip, match_recorded_snapshot
from .sensor_probe import INJECTION, NORMAL, sensor_mode
from .start_connector import StartConnector

RX_SERVICE = RECEIVER + "/.ReceiverService"


def target_foreground_motion_probe(
    bridge: MuMuBridge,
    clip: RecordedProbeClip,
    *,
    clock: Callable = time.monotonic,
    sleep: Callable = time.sleep,
    capture_factory: Callable = StartConnector,
) -> dict:
    """单次最多10秒，在独立UID后台接收；逐帧前台+版本+时钟持续续约。"""
    if not isinstance(bridge, MuMuBridge) or not isinstance(clip, RecordedProbeClip):
        raise ValueError("只能操作本项目MuMu")
    if not 1 <= clip.duration_s <= 10:
        raise ValueError("前台保持诊断只允许1–10秒单次片段")
    capture = capture_factory(bridge, None)
    first = capture.capture().frame.inspection
    if first.run_control_state != "paused":
        raise ValueError("目标不在明确暂停状态，不执行运动诊断或接管倒计时")
    if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
        raise ValueError("当前不是正常传感器模式，不覆盖其他输入")
    client = ForegroundMotionClient(bridge)
    client.prepare(clip)
    report = {
        "diagnostic_only": True,
        "target_foreground_preserved": True,
        "target_steps_verified": False,
        "formal_runs_started": 0,
        "records_submitted": 0,
        "passed": False,
        "renewals": 0,
        "observations": [],
    }

    def receiver_command(action):
        bridge.run(
            "shell",
            "am",
            "start-foreground-service" if action == "START" else "startservice",
            "-n",
            RX_SERVICE,
            "-a",
            RECEIVER + ".RECEIVER_" + action,
            "--es",
            "receiver_request",
            client.request,
            "--ei",
            "receiver_seconds",
            "15",
        )

    def receiver_state():
        value = json.loads(
            bridge.run("shell", "run-as", RECEIVER, "cat", "files/receiver-service.json")
        )
        if (
            value.get("package") != RECEIVER
            or value.get("request_id") != client.request
            or value.get("registered") is not True
            or type(value.get("uid")) is not int
        ):
            raise ValueError("本次独立接收状态尚未核实")
        return value

    mode_owned = False
    try:
        receiver_command("START")
        sleep(0.3)
        rx = receiver_state()
        if rx["event_count"] <= 0:
            raise ValueError("独立消费者未实际收到普通事件")
        bridge.run("shell", "dumpsys", "sensorservice", "data_injection", DIAGNOSTIC_PACKAGE)
        mode_owned = True
        if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != INJECTION:
            raise ValueError("实际注入模式未通过")
        initial = capture.capture(motion_request=client.request)
        if (
            initial.frame.inspection.run_control_state != first.run_control_state
            or not 0 <= clock() - initial.frame.sampled_at <= 2
        ):
            raise ValueError("目标准备/暂停状态变化，不开始诊断")
        client.start(initial.android_observed_ns)
        deadline = clock() + clip.duration_s + 8
        tx = None
        while clock() < deadline:
            sampled = capture.capture(motion_request=client.request)
            view = sampled.frame.inspection
            if (
                view.run_control_state != first.run_control_state
                or view.elapsed_s != first.elapsed_s
                or view.distance_m != first.distance_m
                or "human_face_verification_required" in view.issues
                or not 0 <= clock() - sampled.frame.sampled_at <= 2
            ):
                raise ValueError("目标状态发生变化，不续期")
            tx = sampled.motion_snapshot
            if tx is None:
                raise ValueError("没有本次运动服务状态，不续期")
            state = tx["raw_motion_probe"]["state"]
            report["observations"].append(
                {
                    "capture_age_s": clock() - sampled.frame.sampled_at,
                    "motion_state": state,
                    "sent_rows": tx["raw_motion_probe"]["sent_rows"],
                }
            )
            if state not in {"running", "initializing"}:
                break
            # 不在观察和续期之间插入额外接收/时钟RPC；时钟来自同一帧尾批次。
            client.renew(sampled.android_observed_ns)
            report["renewals"] += 1
            sleep(0.1)
        receiver_command("STOP")
        sleep(0.3)
        rx = receiver_state()
        if (
            tx is None
            or type(tx.get("uid")) is not int
            or tx["uid"] == rx["uid"]
            or rx["capture_done"] is not True
        ):
            raise ValueError("没有独立UID终态接收证据")
        report.update(sender=tx, receiver=rx)
        combined = {
            "elapsed_realtime_ns": rx["elapsed_realtime_ns"],
            "raw_motion_probe": {
                **tx["raw_motion_probe"],
                "received_samples": rx["received_samples"],
            },
        }
        report.update(match_recorded_snapshot(combined, clip, client.request))
    except Exception:
        report["error"] = "前台/时钟/输入或接收诊断未通过；不回显原始内容"
    finally:
        # 任意一个停止ACK失败，仍尝试回收其余资源/NORMAL复核。
        for stop in (client.stop, lambda: receiver_command("STOP")):
            try:
                stop()
            except Exception:
                report["cleanup_unconfirmed"] = True
        try:
            if mode_owned:
                bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
                bridge.run("shell", "dumpsys", "sensorservice", "enable")
            report["mode_restored_normal"] = (
                sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) == NORMAL
            )
        except Exception:
            report["cleanup_unconfirmed"] = True
        try:
            final = capture.capture().frame.inspection
            report["target_foreground_preserved"] = (
                final.run_control_state == first.run_control_state
            )
        except Exception:
            report["target_foreground_preserved"] = False
            report["cleanup_unconfirmed"] = True
    report["passed"] = bool(
        report.get("clip_received_complete")
        and report.get("mode_restored_normal")
        and report["target_foreground_preserved"]
        and not report.get("cleanup_unconfirmed")
    )
    return report
