"""独立 MuMu 对照设备。保留原 QEMU 白名单；不复制账号、不接入跑步驱动。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

from .android import DIAGNOSTIC_PACKAGE, AndroidBridge, default_adb

PLAYER_NAME = "BudaoRun_MuMu"
# 本任务官方下载、实测 Valid NetEase 签名的6.8.4.0管理器；升级需重新核验。
MANAGER_SHA256 = "2eff3e9f096b2cddfc4a86955555e636c6e853eba5f54b0bf8b9f2441a1d9dbb"


class MuMuBridge(AndroidBridge):
    def __init__(self, receipt: Path, adb: Path | None = None, runner=subprocess.run):
        if ".local" not in receipt.resolve().parts:
            raise ValueError("MuMu所有权凭据只能保存在本项目.local目录")
        self.receipt = json.loads(receipt.read_text("utf-8-sig"))
        r = self.receipt
        if not isinstance(r, dict):
            raise ValueError("MuMu所有权凭据必须是JSON对象")
        if (
            r.get("name") != PLAYER_NAME
            or type(r.get("index")) is not int
            or not 1 <= r["index"] <= 127
            or type(r.get("created_timestamp")) is not int
            or r["created_timestamp"] <= 0
            or r.get("android_version") != "12.0"
            or r.get("provisioned_by_this_task") is not True
            or r.get("old_login_data_imported") is not False
            or r.get("manager_sha256") != MANAGER_SHA256
        ):
            raise ValueError("不是本任务创建的空白MuMu对照实例")
        self.serial = r["serial"]
        if not isinstance(self.serial, str) or not re.fullmatch(
            r"127\.0\.0\.1:[1-9]\d{3,4}", self.serial
        ):
            raise ValueError("MuMu只允许管理器确认的本机ADB端口，不允许USB/远程设备")
        self.port = int(self.serial.split(":")[1])
        if self.port > 65535:
            raise ValueError("无效ADB端口")
        self.root = Path(r["install_root"])
        if not self.root.is_absolute() or str(self.root).startswith("\\\\"):
            raise ValueError("不允许相对安装路径或网络共享")
        self.root = self.root.resolve(strict=True)
        self.manager = self.root / "nx_main" / "MuMuManager.exe"
        self.vm_directory = self.root / "vms" / f"MuMuPlayer-12.0-{r['index']}"
        if (
            Path(r["manager"]).resolve(strict=True) != self.manager
            or Path(r["vm_directory"]).resolve(strict=True) != self.vm_directory
            or self.manager.resolve(strict=True) != self.manager
            or self.vm_directory.resolve(strict=True) != self.vm_directory
        ):
            raise ValueError("实例路径不在独立安装根目录，或使用了重定向")
        with self.manager.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != MANAGER_SHA256:
                raise ValueError("MuMu管理器不是已验证的签名版本")
        self.fingerprint = self.manager.stat()
        self.adb, self.runner, self.avd_name = adb or default_adb(), runner, PLAYER_NAME
        self.check_device()

    def _manager_read(self, *args: str) -> dict:
        stat = self.manager.stat()
        if (stat.st_size, stat.st_mtime_ns) != (
            self.fingerprint.st_size,
            self.fingerprint.st_mtime_ns,
        ):
            raise ValueError("管理器已变化；必须重新验证，不能继续控制")
        result = self.runner(
            [str(self.manager), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("MuMu管理器查询失败")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("errcode", 0) != 0:
            raise RuntimeError("MuMu管理器未返回可信设备信息")
        return value

    def check_device(self) -> None:
        r = self.receipt
        metadata = json.loads((self.vm_directory / "configs/extra_config.json").read_text("utf-8"))
        if (
            metadata.get("playerName") != PLAYER_NAME
            or metadata.get("createTime") != r["created_timestamp"]
            or metadata.get("series") != "12.0"
            or metadata.get("importFilePath") != ""
        ):
            raise ValueError("本地实例身份或导入状态不匹配")
        info = self._manager_read("info", "--vmindex", str(r["index"]))
        expected = {
            "name": PLAYER_NAME,
            "index": str(r["index"]),
            "created_timestamp": r["created_timestamp"],
            "android_version": "12.0",
            "adb_host_ip": "127.0.0.1",
            "adb_port": self.port,
            "is_android_started": True,
            "is_process_started": True,
            "error_code": 0,
            "launch_err_code": 0,
        }
        if any(info.get(k) != v for k, v in expected.items()):
            raise ValueError("MuMu设备离线、启动失败或所有权/ADB端口不匹配")
        settings = self._manager_read(
            "setting", "--vmindex", str(r["index"]), "--key", "root_permission"
        )
        if settings.get("root_permission") != "false":
            raise ValueError("对照设备必须保持官方root关闭状态")
        uid = AndroidBridge.run(self, "shell", "id", "-u")
        sdk = AndroidBridge.run(self, "shell", "getprop", "ro.build.version.sdk")
        if uid != "2000" or sdk != "32":
            raise ValueError("实际ADB权限或Android版本不符合对照环境")

    def run(self, *arguments: str, timeout: float = 15) -> str:
        if not arguments or arguments[0] in {"emu", "connect", "disconnect", "kill-server"}:
            raise ValueError("不能把MuMu适配器用于QEMU控制台或全局ADB控制")
        self.check_device()
        return AndroidBridge.run(self, *arguments, timeout=timeout)

    def diagnostic_snapshot(self) -> dict:
        # 只读取本项目可调试App；绝不读取QQ、乐跑或系统账号数据目录。
        value = json.loads(
            self.run("shell", "run-as", DIAGNOSTIC_PACKAGE, "cat", "files/diagnostics.json")
        )
        if not isinstance(value, dict) or value.get("package") != DIAGNOSTIC_PACKAGE:
            raise ValueError("诊断快照来源不匹配")
        uptime = float(self.run("shell", "cat", "/proc/uptime").split()[0])
        sampled = value.get("elapsed_realtime_ns")
        if (
            not math.isfinite(uptime)
            or type(sampled) is not int
            or not 0 <= uptime * 1_000_000_000 - sampled <= 10_000_000_000
        ):
            raise ValueError("诊断快照过期或属于上一次启动；不能当作当前设备数据")
        return value
