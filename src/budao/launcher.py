"""模拟器启动App的本机配对接口。当前只做真实准备检查，不伪造运行/登录成功。"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .mumu import MuMuBridge
from .profiles import preflight_profile, private_file


class PreparationCoordinator:
    def __init__(self, root: Path, profile: Path, *, bridge_factory=MuMuBridge):
        self.root, self.profile, self.bridge_factory = root.resolve(), profile, bridge_factory
        self.lock = threading.Lock()
        self.last_report = {"state": "unchecked", "message": "尚未检查准备条件", "checks": []}

    def check(self, photo_ready: bool = False) -> dict:
        with self.lock:
            checks = []

            def item(key, title, state, detail):
                checks.append({"key": key, "title": title, "state": state, "detail": detail})

            route = None
            try:
                route = preflight_profile(self.profile, self.root)
                complete = route["catalog_complete"] and route["operator_roads_verified"]
                item(
                    "route",
                    "场地与全部打卡点",
                    "pass" if complete else "not_ready",
                    "目录与声明通道已检查" if complete else "点目录或通道尚未全部核实",
                )
            except (ValueError, OSError):
                item("route", "场地与全部打卡点", "not_ready", "独立场地配置未就绪")
            photo = photo_ready or bool(route and route["selfie_file_available"])
            item(
                "photo",
                "本人照片已准备",
                "pass" if photo else "not_ready",
                "仅检查文件就绪，不代表本人验证通过" if photo else "请在助手中选择本人的照片",
            )
            try:
                bridge = self.bridge_factory(self.root / ".local/setup/mumu-owned-instance.json")
                bridge.check_device()
                item("device", "独立模拟器与权限", "pass", "实例归属、root关闭、实际uid/SDK已核对")
            except Exception:
                # 超时、传输/JSON错误均不能冒充设备通过，也不回显原始异常。
                item("device", "独立模拟器与权限", "not_ready", "当前实例离线或设备守卫未通过")
            # 不把QQ/微信已安装、旧登录截图或旧有效记录当成当前OAuth成功。
            item("login", "乐跑QQ／微信授权", "unknown", "当前登录与授权状态尚未接入实时核验")
            item("motion", "自然运动输入", "not_ready", "MuMu自然步频尚未实际验收")
            item("runtime", "完整自动流程", "not_ready", "开始、观察、驱动与成绩核验尚未完整接通")
            self.last_report = {
                "state": "blocked",
                "message": "准备条件尚未全部通过，未开始乐跑",
                "checks": checks,
                "ready": False,
                "run_started": False,
                "records_submitted": 0,
                "needs_human_verification_if_requested": True,
            }
            return self.last_report

    def status(self) -> dict:
        with self.lock:
            return self.last_report


class LauncherServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, coordinator: PreparationCoordinator, token: str, port: int = 8766):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
            raise ValueError("启动助手需要本机配对密钥，不接受空密钥")
        self.coordinator, self.token = coordinator, token
        super().__init__(("127.0.0.1", port), LauncherHandler)


class LauncherHandler(BaseHTTPRequestHandler):
    server: LauncherServer

    def log_message(self, *_args):
        pass  # 不记录配对密钥、照片、路线或账号。

    def reply(self, status: int, value: dict):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        tokens = self.headers.get_all("X-Launcher-Token") or []
        return (
            self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
            and self.headers.get("Origin") is None
            and len(tokens) == 1
            and secrets.compare_digest(tokens[0], self.server.token)
        )

    def do_GET(self):
        if not self.authorized():
            self.reply(403, {"message": "本机配对未通过"})
        elif self.path == "/v1/status":
            self.reply(200, self.server.coordinator.status())
        else:
            self.reply(404, {"message": "接口不存在"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            transfer = self.headers.get("Transfer-Encoding")
            if not 0 < size <= 2048 or transfer is not None:
                # Windows上带未读请求体直接关闭会RST，丢掉本来已发送的400。
                # 只限时丢弃小型、已声明长度的拒绝体；不解析，也不无限吞大请求。
                if transfer is None and 0 < size <= 65536:
                    self.connection.settimeout(1)
                    self.rfile.read(size)
                raise ValueError
            self.connection.settimeout(5)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError
        except (OSError, ValueError):
            self.reply(400, {"message": "请求格式不符合要求"})
            return
        if not self.authorized():
            self.reply(403, {"message": "本机配对未通过"})
            return
        if self.path not in {"/v1/check", "/v1/start"}:
            self.reply(404, {"message": "接口不存在"})
            return
        try:
            data = json.loads(body)
            if (
                self.headers.get("Content-Type") != "application/json"
                or not isinstance(data, dict)
                or set(data) != {"photo_ready"}
                or type(data["photo_ready"]) is not bool
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            self.reply(400, {"message": "请求结构不符合要求；不回显输入"})
            return
        # start不是模拟成功：未通过实际检查时不启动App、不产生位置输出或上传。
        report = self.server.coordinator.check(data["photo_ready"])
        self.reply(200 if self.path == "/v1/check" else 409, report)


def serve_launcher(root: Path, profile: Path, config_file: Path, port: int = 8766):
    if port != 8766:
        raise ValueError("当前原生客户端固定使用8766本机反向端口")
    config_file = private_file(root, config_file, must_exist=False)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if config_file.exists():
        try:
            value = json.loads(config_file.read_text("utf-8-sig"))
            token = value["token"]
            if set(value) != {"token"}:
                raise ValueError
        except (ValueError, OSError, TypeError, KeyError):
            raise ValueError("本机配对配置无效；不回显内容") from None
    else:
        token = secrets.token_urlsafe(32)
        with config_file.open("x", encoding="utf-8") as stream:
            json.dump({"token": token}, stream)
    with LauncherServer(PreparationCoordinator(root, profile), token, port) as server:
        server.serve_forever()
