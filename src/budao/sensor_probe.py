"""只在归属守卫后的独立MuMu诊断有界原始传感器输入；不开始或提交跑步。"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable

from .android import DIAGNOSTIC_PACKAGE
from .mumu import MuMuBridge
from .target import TARGET
from .ui import focused_component

NORMAL = "NORMAL"
INJECTION = "DATA_INJECTION : " + DIAGNOSTIC_PACKAGE


def sensor_mode(text: str) -> str | None:
    """只认SensorService顶层模式，不把某个普通客户端的NORMAL当系统已恢复。"""
    matches = re.findall(r"^Mode\s*:\s*([^\r\n]+)", text, re.MULTILINE)
    return matches[0].strip() if len(matches) == 1 else None


def match_probe_samples(snapshot: dict) -> dict:
    """ACK不是接收：时间戳精确对应，允许实测加速度量化误差0.0001m/s²。"""
    result = {"matched_by_type": {"1": 0, "4": 0}, "raw_receiver_verified": False}
    try:
        probe = snapshot["injection_probe"]
        sampled = snapshot["elapsed_realtime_ns"]
        if (
            type(sampled) is not int
            or probe["synthetic_diagnostic_only"] is not True
            or probe["initialized"] is not True
            or probe["client_closed"] is not True
            or probe["state"] != "completed"
        ):
            return result
        sent, received = probe["sent_samples"], probe["received_samples"]
        if len(sent) != 50 or not 1 <= len(received) <= 1024:
            return result

        def sample(value):
            kind, timestamp, vector = value["type"], value["timestamp_ns"], value["values"]
            if (
                type(kind) is not int
                or kind not in (1, 4)
                or type(timestamp) is not int
                or not 0 < timestamp <= sampled
                or len(vector) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
            ):
                raise ValueError
            return kind, timestamp, vector

        sent = [sample(s) for s in sent]
        received = [sample(s) for s in received]
        if any(a[1] >= b[1] for a, b in zip(sent, sent[1:], strict=False)):
            return result
        if not 0.8e9 <= sent[-1][1] - sent[0][1] <= 3e9:
            return result
        if any(sum(s[0] == kind for s in sent) != 25 for kind in (1, 4)):
            return result
        for kind, timestamp, vector in sent:
            if kind == 1 and (
                abs(vector[0] - 0.1) > 1e-5
                or not 9.2999 <= vector[1] <= 10.3001
                or abs(vector[2] - 0.2) > 1e-5
            ):
                return result
            if kind == 4 and (vector[:2] != [0, 0] or abs(vector[2]) > 0.020001):
                return result
            if any(
                rkind == kind
                and rt == timestamp
                and all(abs(a - b) <= 0.0001 for a, b in zip(vector, rv, strict=True))
                for rkind, rt, rv in received
            ):
                result["matched_by_type"][str(kind)] += 1
        result["raw_receiver_verified"] = all(v >= 20 for v in result["matched_by_type"].values())
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"matched_by_type": {"1": 0, "4": 0}, "raw_receiver_verified": False}
    return result


def probe_mumu_sensors(bridge: MuMuBridge, *, sleep: Callable = time.sleep) -> dict:
    """一秒25Hz测试，仅自身诊断客户端。finally关闭客户端后恢复HAL并实际复核。"""
    if not isinstance(bridge, MuMuBridge):
        raise ValueError("原始传感器诊断只能使用本项目MuMu适配器")
    bridge.check_device()
    if TARGET in bridge.run("shell", "ps", "-A"):
        raise ValueError("传感器探测要求乐跑进程已停止，不干扰当前会话")
    if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
        raise ValueError("当前不是已确认的NORMAL模式，不覆盖其他传感器实验")
    snapshot = bridge.diagnostic_snapshot()
    api = snapshot.get("injection_api", {})
    report = {
        "diagnostic_only": True,
        "synthetic_signal": True,
        "formal_runs_started": 0,
        "records_submitted": 0,
        "mode_entered": False,
        "mode_restored_normal": False,
        "raw_receiver_verified": False,
        "natural_gait_verified": False,
        "target_steps_verified": False,
        "passed": False,
    }
    if any(api.get(key) != "available" for key in ("initDataInjection", "injectSensorData")):
        return {**report, "state": "api_not_accessible", "mode_restored_normal": True}
    # 保留普通诊断权限，但要新建Activity，避免am start把extra交给没有onNewIntent的旧实例。
    bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    try:
        bridge.run("shell", "dumpsys", "sensorservice", "data_injection", DIAGNOSTIC_PACKAGE)
        mode = sensor_mode(bridge.run("shell", "dumpsys", "sensorservice"))
        report["mode_entered"] = mode == INJECTION
        report["state"] = "hal_not_supported"
        if report["mode_entered"]:
            bridge.run(
                "shell",
                "am",
                "start",
                "-n",
                f"{DIAGNOSTIC_PACKAGE}/.DiagnosticActivity",
                "--ez",
                "probe_injection",
                "true",
            )
            sleep(3)
            focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
            if focus != (DIAGNOSTIC_PACKAGE, DIAGNOSTIC_PACKAGE + ".DiagnosticActivity"):
                raise ValueError("诊断客户端不在前台")
            snapshot = bridge.diagnostic_snapshot()
            report["snapshot"] = snapshot
            report.update(match_probe_samples(snapshot))
            report["state"] = (
                "received" if report["raw_receiver_verified"] else "receiver_unverified"
            )
    except Exception:
        report["state"] = "probe_failed"  # 不回显ADB参数、系统日志或认证材料。
    finally:
        # 本机实测：仅initDataInjection(false)+enable仍可能留在DATA_INJECTION空白名单。
        # 先关闭唯一自身客户端，再enable；必须读顶层模式，而不是普通客户端Operating Mode。
        try:
            bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
            bridge.run("shell", "dumpsys", "sensorservice", "enable")
            mode = sensor_mode(bridge.run("shell", "dumpsys", "sensorservice"))
            report["mode_restored_normal"] = mode == NORMAL
            if report["mode_restored_normal"]:
                bridge.diagnostic_start()
                sleep(1)
                first = bridge.diagnostic_snapshot()
                sleep(0.5)
                second = bridge.diagnostic_snapshot()
                report["normal_stream_resumed"] = all(
                    any(
                        b["type"] == kind
                        and b["event_count"] > a["event_count"]
                        and b["last_timestamp_ns"] > a["last_timestamp_ns"]
                        for a in first["sensors"]
                        if a["type"] == kind
                        for b in second["sensors"]
                    )
                    for kind in (1, 4)
                )
                report["standard_step_sensors_present"] = any(
                    s["type"] in (18, 19) for s in second["sensors"]
                )
        except Exception:
            report["cleanup_error"] = "正常模式或普通事件流未能复核；需先检查，不继续移动"
    report["passed"] = (
        report["raw_receiver_verified"]
        and report["mode_restored_normal"]
        and report.get("normal_stream_resumed", False)
    )
    return report
