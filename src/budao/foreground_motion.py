"""前台IMU服务的两秒许可与独立前台接收实验；不是目标步频或真实跑步验收。"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable

from .android import DIAGNOSTIC_PACKAGE
from .models import GeoPoint
from .mumu import MuMuBridge
from .raw_motion import RecordedProbeClip, match_recorded_snapshot, write_motion_probe
from .sensor_probe import INJECTION, NORMAL, sensor_mode
from .target import RUN_ACTIVITY, TARGET
from .ui import focused_component

RECEIVER = "com.edurun.receiver"
RECEIVER_ACTIVITY = RECEIVER + ".ReceiverActivity"
SERVICE = DIAGNOSTIC_PACKAGE + "/.RawMotionService"
# 固定只读命令，不接受用户包名、文件名或shell插值。一次守卫覆盖整批观察，缩短许可延迟。
OBSERVATION_COMMAND = (
    "dumpsys window displays; echo __EDURUN_RX__; "
    "run-as com.edurun.receiver cat files/receiver-state.json; echo __EDURUN_SVC__; "
    "run-as com.edurun.diagnostic cat files/motion-service.json"
)


class ForegroundMotionClient:
    def __init__(
        self,
        bridge: MuMuBridge,
        request_id: str | None = None,
        *,
        bounded_stream=False,
        replay_passes=1,
    ):
        if not isinstance(bridge, MuMuBridge):
            raise ValueError("前台运动服务只能使用本项目MuMu")
        import re

        self.request = request_id or secrets.token_hex(16)
        if not re.fullmatch(r"[a-f0-9]{32}", self.request):
            raise ValueError("需要新的独立任务标记，不接受认证信息")
        self.bridge, self.started, self.prepared = bridge, False, False
        if (
            type(bounded_stream) is not bool
            or type(replay_passes) is not int
            or not 1 <= replay_passes <= 6
            or (not bounded_stream and replay_passes != 1)
        ):
            raise ValueError("只允许明确的最多6遍有界片段回放")
        self.bounded_stream, self.replay_passes = bounded_stream, replay_passes
        self.native_prepared = False
        self.preload_requested = False
        self.stop_attempted, self.stop_acknowledged = False, False

    def _require_open(self):
        if self.stop_attempted:
            raise ValueError("本任务已经请求停止，不能重新准备、开始或续期")

    def _command(self, action: str, observed_ns: int = 0, *, start: bool = False):
        if type(observed_ns) is not int or observed_ns < 0:
            raise ValueError("观察时钟必须是Android实际单调纳秒，不接受布尔或浮点")
        self.bridge.run(
            "shell",
            "am",
            "start-foreground-service" if start else "startservice",
            "-n",
            SERVICE,
            "-a",
            DIAGNOSTIC_PACKAGE + ".MOTION_" + action,
            "--es",
            "motion_probe_request",
            self.request,
            "--el",
            "motion_observed_ns",
            str(observed_ns),
            "--ei",
            "motion_replay_passes",
            str(self.replay_passes),
            "--ez",
            "motion_bounded_stream",
            "true" if self.bounded_stream else "false",
            "--ei",
            "motion_lease_ms",
            "2000",
        )

    def prepare(self, clip: RecordedProbeClip):
        self._require_open()
        if self.started or self.preload_requested:
            raise ValueError("活动任务不能替换运动数据")
        if sensor_mode(self.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
            raise ValueError("只在正常模式提前准备，不占用运行许可窗口")
        if self.bounded_stream:
            write_motion_probe(self.bridge, clip.payload, bounded_stream=True)
        else:
            write_motion_probe(self.bridge, clip.payload)
        self.prepared = True

    def preload(self, clip: RecordedProbeClip, *, clock=time.monotonic, sleep=time.sleep):
        """正常模式中提前解析长片段；没有运动租约、注入或目标UI动作。"""
        self._require_open()
        if not self.bounded_stream or not self.prepared or self.started or self.preload_requested:
            raise ValueError("只允许已写入的新长片段任务预加载一次")
        if sensor_mode(self.bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
            raise ValueError("只能在正常模式提前解析运动数据")
        self.preload_requested = True  # 不明结果也不能重复PREPARE；STOP仍能取消该nonce。
        self._command("PREPARE", start=True)
        deadline = clock() + 10
        while clock() < deadline:
            value = self.bridge.run(
                "shell", "run-as", DIAGNOSTIC_PACKAGE, "cat", "files/motion-service.json"
            )
            try:
                state = json.loads(value)
                probe = state["raw_motion_probe"]
                if state["package"] == DIAGNOSTIC_PACKAGE and state["request_id"] == self.request:
                    if probe["state"] != "prepared":
                        raise ValueError("原生任务未就绪，不开始运动")
                    if (
                        probe["request_id"] != self.request
                        or probe["clip_sha256"] != clip.sha256
                        or probe["source_rows"] != len(clip.rows)
                        or probe["bounded_stream"] is not True
                        or probe["replay_passes"] != self.replay_passes
                        or probe["sent_rows"] != 0
                        or probe["lease_required"] is not False
                    ):
                        raise ValueError("原生预加载身份、数据或零输出状态不匹配")
                    self.native_prepared = True
                    return
            except (KeyError, TypeError, json.JSONDecodeError):
                pass  # 未出现本次状态，不能把旧nonce或命令ACK当作准备完成。
            sleep(0.1)
        raise ValueError("未获取本次原生预加载证据，不开始运动")

    def start(self, observed_ns: int):
        self._require_open()
        if self.started:
            raise ValueError("同一任务不能重复开始；过期后也不能自动重启")
        if not self.prepared:
            raise ValueError("必须在观察前完成有界载荷准备")
        if self.bounded_stream and not self.native_prepared:
            raise ValueError("长片段必须在正常模式预加载，冷启动不能消耗运动许可")
        # HAL模式由编排入口核对；native init仍会核对模式/白名单。
        # 不在最新观察与START之间重写文件，或插入多次设备查询消耗两秒许可。
        # ACK只表示命令被接受；真正运行仍需独立nonce/状态/回调证明。
        self.started = True
        self._command("START", observed_ns, start=True)

    def renew(self, observed_ns: int):
        self._require_open()
        if not self.started:
            raise ValueError("未开始的任务不能续期")
        self._command("RENEW", observed_ns)

    def renewal_observation_command(self, *, gps_position: GeoPoint | None = None) -> str:
        """用于已解析的允许帧的尾查询。shell再次核对唯一焦点后才续期。"""
        self._require_open()
        if not self.started:
            raise ValueError("未开始的运动任务不能生成续期观察")
        if gps_position is not None and not isinstance(gps_position, GeoPoint):
            raise ValueError("接收定位守卫只能使用已验证的GeoPoint")
        if gps_position is not None:
            # model_copy/update可跳过Pydantic校验，不能因此把任意字符串插入shell。
            gps_position = GeoPoint.model_validate(gps_position.model_dump(warnings=False))
        gps_guard = (
            " --ez motion_require_received_gps true"
            f" --ef motion_expected_lat {gps_position.lat}"
            f" --ef motion_expected_lon {gps_position.lon}"
            if gps_position is not None
            else ""
        )
        # 只插入构造时验证过的hex32和有界整数；没有调用方shell、路径或认证内容。
        pattern = (
            r"^[[:space:]]*mCurrentFocus=Window\{[^{}]*[[:space:]]"
            + TARGET.replace(".", r"\.")
            + "/"
            + RUN_ACTIVITY.replace(".", r"\.")
            + r"\}$"
        )
        return (
            'window=$(dumpsys window displays); printf "%s\\n" "$window"; '
            'if [ "$(printf "%s\\n" "$window" | grep -c "^[[:space:]]*mCurrentFocus=")" = 1 ] '
            f'&& printf "%s\\n" "$window" | grep -Eq \'{pattern}\'; then '
            "read -r up idle < /proc/uptime; echo __EDURUN_CLOCK__; "
            'printf "%s %s\\n" "$up" "$idle"; '
            'if printf "%s\\n" "$up" | grep -Eq "^[0-9]+[.][0-9]{2}$"; then '
            "seconds=${up%.*}; fraction=${up#*.}; "
            # Android mksh may use signed 32-bit arithmetic. Concatenation preserves
            # the actual two-decimal uptime exactly, without overflow or a guessed clock.
            'observed="${seconds}${fraction}0000000"; '
            f"am startservice -n {SERVICE} -a {DIAGNOSTIC_PACKAGE}.MOTION_RENEW "
            f"--es motion_probe_request {self.request} --el motion_observed_ns $observed "
            f"--ei motion_replay_passes {self.replay_passes} --ez motion_bounded_stream "
            f"{'true' if self.bounded_stream else 'false'} --ei motion_lease_ms 2000"
            f"{gps_guard} >/dev/null; "
            "fi; echo __EDURUN_MOTION__; "
            "run-as com.edurun.diagnostic cat files/motion-service.json; "
            "echo __EDURUN_AFTER_CLOCK__; cat /proc/uptime; fi"
        )

    def stop(self):
        if self.started or self.preload_requested:
            if self.stop_attempted:
                if not self.stop_acknowledged:
                    raise RuntimeError("本次停止结果未确认，不重复发送STOP")
                return
            # 人工等待已STOP并force-stop后，最终清理不能再唤起服务。
            # 超时也先消费本次STOP，后续清理保留未知结果而不是盲重试。
            self.stop_attempted = True
            self._command("STOP")
            self.stop_acknowledged = True  # 仅命令ACK；模式恢复仍由编排独立核对。


def receiver_observation(bridge: MuMuBridge, request: str) -> tuple[dict, dict | None]:
    value = bridge.run("shell", OBSERVATION_COMMAND)
    try:
        focus, rest = value.split("__EDURUN_RX__", 1)
        receiver, service = rest.split("__EDURUN_SVC__", 1)
        if focused_component(focus) != (RECEIVER, RECEIVER_ACTIVITY):
            raise ValueError
        rx = json.loads(receiver)
        if (
            rx["package"] != RECEIVER
            or rx["request_id"] != request
            or rx["registered"] is not True
            or type(rx["elapsed_realtime_ns"]) is not int
        ):
            raise ValueError
        try:
            tx = json.loads(service)
            if (
                not isinstance(tx, dict)
                or tx.get("package") != DIAGNOSTIC_PACKAGE
                or tx.get("request_id") != request
            ):
                tx = None  # 旧状态不能当作本次服务已经开始。
        except ValueError:
            tx = None
        return rx, tx
    except (ValueError, TypeError, KeyError):
        raise ValueError("独立前台观察未通过，不续期，不回显界面或原始输出") from None


def cross_foreground_probe(
    bridge: MuMuBridge,
    clip: RecordedProbeClip,
    *,
    withhold_renewal: bool = False,
    sleep: Callable = time.sleep,
    clock: Callable = time.monotonic,
) -> dict:
    if not isinstance(bridge, MuMuBridge) or type(withhold_renewal) is not bool:
        raise ValueError("只允许独立MuMu的跨UID诊断")
    if withhold_renewal and clip.duration_s < 3:
        raise ValueError("到期实验至少需要三秒输入，不能把提前播完当作到期保护")
    if TARGET in bridge.run("shell", "ps", "-A"):
        raise ValueError("跨UID实验要求乐跑进程停止，不干扰跑步或广告")
    if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != NORMAL:
        raise ValueError("未知或已有注入模式，不覆盖其他实验")
    bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    bridge.run("shell", "am", "force-stop", RECEIVER)
    client = ForegroundMotionClient(bridge)
    request = client.request
    client.prepare(clip)
    bridge.run(
        "shell",
        "am",
        "start",
        "-n",
        RECEIVER + "/.ReceiverActivity",
        "--es",
        "receiver_request",
        request,
    )
    sleep(0.6)
    try:
        before, _ = receiver_observation(bridge, request)
        if before["event_count"] <= 0:
            raise ValueError("进入模式前独立消费者未实际收到普通事件")
    except Exception:
        bridge.run("shell", "am", "force-stop", RECEIVER)
        raise
    report = {
        "diagnostic_only": True,
        "consumer_registered_before_mode": True,
        "withhold_renewal": withhold_renewal,
        "source_csv_sha256": clip.source_sha256,
        "probe_csv_sha256": clip.sha256,
        "formal_runs_started": 0,
        "records_submitted": 0,
        "target_foreground_motion_verified": False,
        "target_steps_verified": False,
        "passed": False,
        "renewals": 0,
    }
    try:
        bridge.run("shell", "dumpsys", "sensorservice", "data_injection", DIAGNOSTIC_PACKAGE)
        if sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) != INJECTION:
            raise ValueError("实际模式没有进入已核对的自有注入客户端")
        rx, _ = receiver_observation(bridge, request)
        client.start(rx["elapsed_realtime_ns"])
        deadline = clock() + clip.duration_s + 8
        tx = None
        while clock() < deadline:
            sleep(0.1)
            rx, tx = receiver_observation(bridge, request)
            if tx and tx["raw_motion_probe"].get("state") not in ("running", "initializing"):
                break
            if not withhold_renewal:
                client.renew(rx["elapsed_realtime_ns"])
                report["renewals"] += 1
        if not tx:
            raise ValueError("本次前台服务尚无因果状态证据")
        bridge.run(
            "shell",
            "am",
            "start",
            "-n",
            RECEIVER + "/.ReceiverActivity",
            "--es",
            "receiver_request",
            request,
            "--ez",
            "capture_done",
            "true",
        )
        sleep(0.25)
        rx, latest = receiver_observation(bridge, request)
        tx = latest or tx
        if (
            type(rx["uid"]) is not int
            or type(tx["uid"]) is not int
            or rx["uid"] == tx["uid"]
            or rx["capture_done"] is not True
        ):
            raise ValueError("必须是已完成采集的独立UID，而不是自回环")
        report.update(sender=tx, receiver=rx)
        combined = {
            "elapsed_realtime_ns": rx["elapsed_realtime_ns"],
            "raw_motion_probe": {
                **tx["raw_motion_probe"],
                "received_samples": rx["received_samples"],
            },
        }
        if withhold_renewal:
            p = tx["raw_motion_probe"]
            sent = p["sent_samples"]
            expiry = p["lease_deadline_ns"]
            report["lease_expiry_stop_verified"] = (
                p["state"] == "lease_expired"
                and p["lease_required"] is True
                and p["client_closed"] is True
                and 0 < p["sent_rows"] < len(clip.rows)
                and all(v["timestamp_ns"] < expiry for v in sent)
            )
        else:
            report.update(match_recorded_snapshot(combined, clip, request))
    except Exception:
        report["error"] = "跨UID/前台/许可诊断尚未通过；不回显私有数据"
    finally:
        try:
            bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
            bridge.run("shell", "dumpsys", "sensorservice", "enable")
            report["mode_restored_normal"] = (
                sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) == NORMAL
            )
            bridge.run("shell", "am", "force-stop", RECEIVER)
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
        except Exception:
            report["cleanup_error"] = "正常模式或普通事件流未复核，不继续运动输出"
    verified = (
        report.get("lease_expiry_stop_verified", False)
        if withhold_renewal
        else report.get("clip_received_complete", False)
    )
    report["passed"] = (
        verified
        and report.get("mode_restored_normal", False)
        and report.get("normal_stream_resumed", False)
    )
    return report
