"""所有写操作限定到本项目 AVD；ADB 命令无 shell 拼接。"""

from __future__ import annotations

import math
import os
import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

from .models import GeoPoint

AVD_NAME = "BudaoRun_API34_Play"
PROJECT_AVD_NAMES = frozenset({AVD_NAME, "BudaoRun_API36_Play"})
DIAGNOSTIC_PACKAGE = "com.edurun.diagnostic"


def default_adb() -> Path:
    sdk = os.environ.get("ANDROID_HOME")
    if not sdk:
        sdk = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")
    return Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")


class AndroidBridge:
    def __init__(
        self,
        serial: str,
        adb: Path | None = None,
        avd_name: str = AVD_NAME,
        runner: Callable = subprocess.run,
    ):
        if not re.fullmatch(r"emulator-\d+", serial):
            raise ValueError("写操作只能使用本项目模拟器；不会向 USB 真机注入数据")
        if avd_name not in PROJECT_AVD_NAMES:
            raise ValueError("不能借助修改参数操作其他旧 AVD")
        self.serial, self.adb, self.avd_name, self.runner = (
            serial,
            adb or default_adb(),
            avd_name,
            runner,
        )
        self.check_device()

    def run(self, *arguments: str, timeout: float = 15) -> str:
        process = self.runner(
            [str(self.adb), "-s", self.serial, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(f"ADB 操作失败：{process.stderr.strip()[:200]}")
        return process.stdout.strip()

    def check_device(self) -> None:
        if self.run("shell", "getprop", "ro.kernel.qemu") != "1":
            raise ValueError("设备没有确认是 QEMU 模拟器")
        names = self.run("emu", "avd", "name").splitlines()
        if not names:
            raise ValueError("未读取到AVD名称，拒绝写操作")
        name = names[0]
        if name != self.avd_name:
            raise ValueError(f"AVD 不匹配：必须为 {self.avd_name}")

    def install(self, apk: Path) -> None:
        self.check_device()
        if not apk.is_file() or apk.suffix.lower() != ".apk":
            raise ValueError("找不到 APK")
        self.run("install", "-r", str(apk.resolve()), timeout=180)

    def diagnostic_start(self) -> None:
        self.check_device()
        for permission in (
            "ACCESS_FINE_LOCATION",
            "ACCESS_COARSE_LOCATION",
            "ACTIVITY_RECOGNITION",
        ):
            self.run("shell", "pm", "grant", DIAGNOSTIC_PACKAGE, f"android.permission.{permission}")
        self.run("shell", "am", "start", "-n", f"{DIAGNOSTIC_PACKAGE}/.DiagnosticActivity")


class EmulatorConsole:
    """官方 console 持久连接，避免为每个25Hz传感器采样启动一个 adb 进程。"""

    def __init__(self, bridge: AndroidBridge, timeout: float = 3):
        bridge.check_device()
        port = int(bridge.serial.split("-")[1])
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.stream = self.socket.makefile("rb")
        try:
            greeting = self._response()
            if "Authentication required" in greeting:
                token = (Path.home() / ".emulator_console_auth_token").read_text("utf-8").strip()
                # 令牌只发送到 localhost；从不记录/打印。
                self.socket.sendall(f"auth {token}\n".encode())
                self._response()
        except BaseException:
            self.close()
            raise

    def _response(self) -> str:
        lines = []
        while True:
            line = self.stream.readline()
            if not line:
                raise RuntimeError("模拟器控制连接已关闭")
            text = line.decode("utf-8", "replace").strip()
            if text == "OK":
                return "\n".join(lines)
            if text.startswith("KO"):
                raise RuntimeError(f"模拟器拒绝控制命令：{text[:160]}")
            lines.append(text)

    def _command(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            raise ValueError("禁止多行控制命令")
        self.socket.sendall((command + "\n").encode("ascii"))
        return self._response()

    def sensors(self) -> str:
        return self._command("sensor status")

    def location(self, point: GeoPoint, speed_mps: float = 0, altitude_m: float = 0) -> None:
        if not math.isfinite(speed_mps) or not 0 <= speed_mps <= 10:
            raise ValueError("无效速度")
        if not math.isfinite(altitude_m):
            raise ValueError("无效海拔")
        knots = speed_mps / 0.5144444444444445
        self._command(f"geo fix {point.lon:.9f} {point.lat:.9f} {altitude_m:.2f} 7 {knots:.5f}")

    def sensor(self, name: str, values: tuple[float, float, float]) -> None:
        if name not in {"acceleration", "gyroscope"}:
            raise ValueError("仅允许已验证的三轴加速度/陀螺仪输入")
        if len(values) != 3 or not all(math.isfinite(v) and abs(v) <= 100 for v in values):
            raise ValueError("无效三轴输入")
        formatted = ":".join(f"{v:.6f}" for v in values)
        self._command(f"sensor set {name} {formatted}")

    def close(self) -> None:
        self.stream.close()
        self.socket.close()

    def __enter__(self) -> EmulatorConsole:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
