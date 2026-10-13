"""有来源、单次有界的MuMu原始IMU诊断。没有位置输出、步数或跑步提交。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .android import DIAGNOSTIC_PACKAGE
from .driver import MotionClip
from .motion_data import ARCHIVES, SOURCE_PAGE, align_clip, file_hash, read_sensor
from .mumu import MuMuBridge
from .profiles import private_file
from .sensor_probe import INJECTION, NORMAL, sensor_mode
from .target import TARGET
from .ui import focused_component


@dataclass(frozen=True)
class RecordedProbeClip:
    payload: bytes
    rows: tuple[tuple[float, ...], ...]
    source_sha256: str

    @property
    def sha256(self):
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def duration_s(self):
        return self.rows[-1][0]


def bounded_clip(
    clip: MotionClip, seconds: int, source_sha256: str, *, bounded_stream: bool = False
) -> RecordedProbeClip:
    maximum = 150 if bounded_stream else 10
    if (
        type(bounded_stream) is not bool
        or type(seconds) is not int
        or not 1 <= seconds <= maximum
        or clip.duration < seconds
    ):
        raise ValueError("原始IMU探测只允许1–10整数秒，不循环、不填补或缩放")
    count = seconds * 50 + 1
    if len(clip.times) < count or not np.allclose(
        clip.times[:count], np.arange(count) / 50, rtol=0, atol=1e-6
    ):
        raise ValueError("输入必须已经按50Hz对齐，不能冒充进行抗混叠降采样")
    rows = tuple(
        (float(t), *(float(v) for v in values))
        for t, values in zip(clip.times[:count], clip.values[:count], strict=True)
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
    writer.writerows((f"{r[0]:.6f}", *(f"{v:.9f}" for v in r[1:])) for r in rows)
    return RecordedProbeClip(output.getvalue().encode("utf-8"), rows, source_sha256)


def prepare_recorded_probe(
    root: Path, source: Path, seconds: int, *, bounded_stream: bool = False
) -> RecordedProbeClip:
    """核对官方压缩包固定摘要，并重建全CSV，不仅相信可修改的来源JSON标签。"""
    source = private_file(root, source)
    manifest_file = private_file(root, source.with_suffix(".json"))
    try:
        if manifest_file.stat().st_size > 16 * 1024:
            raise ValueError
        if source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError
        manifest = json.loads(manifest_file.read_text("utf-8-sig"))
        duration = manifest["quality"]["duration_s"]
        if (
            manifest["dataset"] != "RealWorld HAR2016"
            or manifest["source_page"] != SOURCE_PAGE
            or manifest["activity_label"] != "running"
            or manifest["body_position"] != "waist"
            or manifest["archive_sha256"] != ARCHIVES
            or manifest["csv_file"] != source.name
            or file_hash(source) != manifest["csv_sha256"]
            or type(duration) is not int
            or not 1 <= duration <= 300
        ):
            raise ValueError
        for name, digest in ARCHIVES.items():
            archive = private_file(root, source.parent / name)
            if archive.stat().st_size > 32 * 1024 * 1024 or file_hash(archive) != digest:
                raise ValueError
        acceleration = read_sensor(source.parent / "acc_running_csv.zip", "acc_running_waist.csv")
        gyroscope = read_sensor(
            source.parent / "gyr_running_csv.zip", "Gyroscope_running_waist.csv"
        )
        aligned, _ = align_clip(acceleration, gyroscope, duration)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
        writer.writerows((f"{r[0]:.6f}", *(f"{v:.9f}" for v in r[1:])) for r in aligned)
        if output.getvalue().encode("utf-8") != source.read_bytes():
            raise ValueError
    except (ValueError, TypeError, KeyError, OSError):
        raise ValueError("公开运动来源或文件摘要未通过；不回显配置，不写入设备") from None
    return bounded_clip(
        MotionClip(source), seconds, manifest["csv_sha256"], bounded_stream=bounded_stream
    )


def match_recorded_snapshot(
    snapshot: dict, clip: RecordedProbeClip, request_id: str, *, stall_test: bool = False
) -> dict:
    result = {
        "matched_by_type": {"1": 0, "4": 0},
        "received_hz": {},
        "clip_received_complete": False,
        "clock_gap_stop_verified": False,
    }
    try:
        probe = snapshot["raw_motion_probe"]
        sampled = snapshot["elapsed_realtime_ns"]
        sent, received = probe["sent_samples"], probe["received_samples"]
        expected = 25 if stall_test else len(clip.rows)
        if (
            type(sampled) is not int
            or probe["request_id"] != request_id
            or probe["clip_sha256"] != clip.sha256
            or probe["diagnostic_only"] is not True
            or probe["initialized"] is not True
            or probe["client_closed"] is not True
            or probe["stall_test"] is not stall_test
            or probe["state"] != ("clock_gap" if stall_test else "completed")
            or type(probe["source_rows"]) is not int
            or type(probe["sent_rows"]) is not int
            or probe["source_rows"] != len(clip.rows)
            or probe["sent_rows"] != expected
            or len(sent) != expected * 2
            or not 1 <= len(received) <= 4096
        ):
            return result

        def read(value):
            kind, timestamp, vector = value["type"], value["timestamp_ns"], value["values"]
            if (
                type(kind) is not int
                or kind not in (1, 4)
                or type(timestamp) is not int
                or not 0 < timestamp <= sampled
                or not isinstance(vector, list)
                or len(vector) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
            ):
                raise ValueError
            return kind, timestamp, vector

        receivers = {}
        for value in received:
            kind, timestamp, vector = read(value)
            receivers.setdefault((kind, timestamp), []).append(vector)
        times = {1: [], 4: []}
        previous = 0
        for number, value in enumerate(sent):
            kind, timestamp, vector = read(value)
            index, channel = divmod(number, 2)
            offset = 1 if channel == 0 else 4
            source = clip.rows[index]
            if (
                kind != (1 if channel == 0 else 4)
                or type(value["source_index"]) is not int
                or value["source_index"] != index
                or type(value["source_t_s"]) not in (int, float)
                or not math.isfinite(value["source_t_s"])
                or abs(value["source_t_s"] - source[0]) > 1e-5
                or timestamp <= previous
                or any(
                    abs(a - b) > 1e-4
                    for a, b in zip(vector, source[offset : offset + 3], strict=True)
                )
            ):
                return result
            previous = timestamp
            if any(
                all(abs(a - b) <= 1e-4 for a, b in zip(vector, rv, strict=True))
                for rv in receivers.get((kind, timestamp), [])
            ):
                result["matched_by_type"][str(kind)] += 1
                times[kind].append(timestamp)
        all_matched = all(v == expected for v in result["matched_by_type"].values())
        for kind, values in times.items():
            if len(values) < 2:
                return result
            gaps = np.diff(values) / 1e9
            if np.min(gaps) < 0.018 or np.max(gaps) > 0.1:
                return result
            result["received_hz"][str(kind)] = (len(values) - 1) * 1e9 / (values[-1] - values[0])
        rate_ready = all(45 <= hz <= 55 for hz in result["received_hz"].values())
        gap = probe["maximum_gap_ns"]
        if type(gap) is not int or gap < 0:
            return result
        result["clip_received_complete"] = (
            all_matched and rate_ready and not stall_test and gap <= 1e8
        )
        result["clock_gap_stop_verified"] = all_matched and stall_test and gap > 1e8
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**result, "clip_received_complete": False, "clock_gap_stop_verified": False}
    return result


def write_motion_probe(bridge: MuMuBridge, payload: bytes, *, bounded_stream: bool = False):
    """只写自有诊断App固定文件，stdin传样本；核对摘要后原子替换，不碰认证目录。"""
    if (
        type(bounded_stream) is not bool
        or not isinstance(bridge, MuMuBridge)
        or not 0 < len(payload) <= (1024 * 1024 if bounded_stream else 128 * 1024)
    ):
        raise ValueError("只允许本项目MuMu有界诊断载荷")
    bridge.check_device()
    process = bridge.runner(
        [
            str(bridge.adb),
            "-s",
            bridge.serial,
            "shell",
            "run-as com.edurun.diagnostic sh -c 'umask 077; cat > files/motion-probe.tmp'",
        ],
        input=payload,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if process.returncode:
        raise RuntimeError("自有诊断载荷写入失败，不回显ADB参数或原始数据")
    digest = bridge.run(
        "shell", "run-as", DIAGNOSTIC_PACKAGE, "sha256sum", "files/motion-probe.tmp"
    ).split()[0]
    if digest != hashlib.sha256(payload).hexdigest():
        raise ValueError("自有诊断文件摘要不符，未替换回放文件")
    bridge.run(
        "shell",
        "run-as",
        DIAGNOSTIC_PACKAGE,
        "mv",
        "files/motion-probe.tmp",
        "files/motion-probe.csv",
    )


def replay_recorded_motion(
    bridge: MuMuBridge,
    clip: RecordedProbeClip,
    *,
    stall_test: bool = False,
    sleep: Callable = time.sleep,
    writer: Callable = write_motion_probe,
) -> dict:
    if not isinstance(bridge, MuMuBridge) or type(stall_test) is not bool:
        raise ValueError("只能使用本项目MuMu进行普通或固定250ms时钟中断实验")
    if (
        not isinstance(clip, RecordedProbeClip)
        or not 0 < len(clip.payload) <= 128 * 1024
        or not 1 <= clip.duration_s <= 10
        or len(clip.rows) != round(clip.duration_s * 50) + 1
    ):
        raise ValueError("只接受已经验证的1–10秒有界片段")
    bridge.check_device()
    if TARGET in bridge.run("shell", "ps", "-A"):
        raise ValueError("要求乐跑进程已停止，不干扰当前跑步")
    if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
        raise ValueError("未知/已有注入模式，不覆盖其他实验")
    initial = bridge.diagnostic_snapshot()
    if any(
        initial.get("injection_api", {}).get(k) != "available"
        for k in ("initDataInjection", "injectSensorData")
    ):
        raise ValueError("系统原始传感器API尚未可用，不改变模式")
    writer(bridge, clip.payload)
    request = secrets.token_hex(16)
    report = {
        "diagnostic_only": True,
        "source_csv_sha256": clip.source_sha256,
        "probe_csv_sha256": clip.sha256,
        "source_activity_label": "running",
        "duration_s": clip.duration_s,
        "stall_test": stall_test,
        "formal_runs_started": 0,
        "records_submitted": 0,
        "target_steps_verified": False,
        "user_motion_verified": False,
        "clip_received_complete": False,
        "clock_gap_stop_verified": False,
        "mode_restored_normal": False,
        "passed": False,
    }
    bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    try:
        bridge.run("shell", "dumpsys", "sensorservice", "data_injection", DIAGNOSTIC_PACKAGE)
        if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != INJECTION:
            raise ValueError("实际HAL模式未通过")
        bridge.run(
            "shell",
            "am",
            "start",
            "-n",
            f"{DIAGNOSTIC_PACKAGE}/.DiagnosticActivity",
            "--ez",
            "probe_motion_csv",
            "true",
            "--ei",
            "motion_period_us",
            "20000",
            "--es",
            "motion_probe_request",
            request,
            "--ez",
            "probe_stall_test",
            "true" if stall_test else "false",
        )
        sleep(3 if stall_test else clip.duration_s + 3)
        if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != (
            DIAGNOSTIC_PACKAGE,
            DIAGNOSTIC_PACKAGE + ".DiagnosticActivity",
        ):
            raise ValueError("当前不是自有诊断前台")
        snapshot = bridge.diagnostic_snapshot()
        report["snapshot"] = snapshot
        report.update(match_recorded_snapshot(snapshot, clip, request, stall_test=stall_test))
    except Exception:
        report["error"] = "原始运动回放未完整通过，不回显私有参数或日志"
    finally:
        try:
            bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
            bridge.run("shell", "dumpsys", "sensorservice", "enable")
            report["mode_restored_normal"] = (
                sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) == NORMAL
            )
            if report["mode_restored_normal"]:
                bridge.diagnostic_start(50)
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
            report["cleanup_error"] = "NORMAL模式或普通事件流未复核，不继续运动输出"
    verified = report["clock_gap_stop_verified"] if stall_test else report["clip_received_complete"]
    report["passed"] = (
        verified and report["mode_restored_normal"] and report.get("normal_stream_resumed", False)
    )
    return report
