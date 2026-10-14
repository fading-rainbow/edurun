"""模拟器启动App的本机配对接口。当前只做真实准备检查，不伪造运行/登录成功。"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth_ui import prepare_current_login
from .execution import ExecutionCoordinator
from .mumu import MuMuBridge
from .profiles import preflight_profile, private_file


class PreparationCoordinator:
    def __init__(
        self,
        root: Path,
        profile: Path,
        *,
        bridge_factory=MuMuBridge,
        login_preparer=prepare_current_login,
        execution_preflight=None,
    ):
        self.root, self.profile, self.bridge_factory = root.resolve(), profile, bridge_factory
        self.login_preparer = login_preparer
        self.execution_preflight = execution_preflight
        self.lock = threading.Lock()
        self.last_report = {"state": "unchecked", "message": "尚未检查准备条件", "checks": []}

    def check(self, photo_ready: bool = False) -> dict:
        if not self.lock.acquire(blocking=False):
            return self.status()
        return self._check_locked(photo_ready)

    def begin_check(self, photo_ready: bool = False) -> dict:
        """立即确认后台观察任务；重复请求不排队，也不重复导航。"""
        if not self.lock.acquire(blocking=False):
            return self.status()
        task = threading.Thread(target=self._check_locked, args=(photo_ready,), daemon=True)
        try:
            task.start()
        except Exception:
            self.lock.release()
            raise
        return self.status()

    def _check_locked(self, photo_ready: bool) -> dict:
        try:
            checks = []

            def item(key, title, state, detail):
                checks.append({"key": key, "title": title, "state": state, "detail": detail})

            route = None
            bridge = None
            try:
                route = preflight_profile(self.profile, self.root)
                complete = route["catalog_complete"] and route["operator_roads_verified"]
                item(
                    "route",
                    "场地与全部打卡点",
                    "pass" if complete else "not_ready",
                    "目录与声明通道已检查"
                    if complete
                    else (
                        f"可规划历史接近位置{route['app_confirmed_approaches']}个；"
                        "候选总数/通道尚未全部核实"
                    ),
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
                bridge = None
                item("device", "独立模拟器与权限", "not_ready", "当前实例离线或设备守卫未通过")
            login = {"state": "unknown"}
            if bridge is not None:
                try:
                    login = self.login_preparer(bridge)
                except Exception:
                    pass
            visible_login = login.get("state") == "logged_in_ui"
            expired_login = (
                login.get("state") == "expired_record"
                and login.get("detail") == "过期记录，需用户处理"
            )
            item(
                "login",
                "乐跑QQ／微信授权",
                "pass" if visible_login else "unknown",
                "本次已登录界面已核对；服务端授权仍由实际开始流程复核"
                if visible_login
                else (
                    "过期记录，需用户处理"
                    if expired_login
                    else "当前客户端登录界面未确认；已有会话/必要认证不被覆盖"
                ),
            )
            item("motion", "自然运动输入", "not_ready", "MuMu自然步频尚未实际验收")
            item("runtime", "完整自动流程", "not_ready", "开始、观察、驱动与成绩核验尚未完整接通")
            if self.execution_preflight is not None and route is not None and bridge is not None:
                overrides = self.execution_preflight(route, bridge)
                if not isinstance(overrides, dict) or set(overrides) != {
                    "route",
                    "motion",
                    "runtime",
                }:
                    raise ValueError("本机执行预检结构未知")
                for check in checks:
                    if check["key"] in overrides:
                        state, detail = overrides[check["key"]]
                        if state not in {"pass", "not_ready"} or not isinstance(detail, str):
                            raise ValueError("本机执行预检结果未知")
                        check.update(state=state, detail=detail)
                        check["title"] = {
                            "route": "已确认点的有限试运行",
                            "motion": "三轴输入链路",
                            "runtime": "有界执行编排",
                        }[check["key"]]
            ready = all(check["state"] == "pass" for check in checks)
            expired = any(check.get("detail") == "过期记录，需用户处理" for check in checks)
            self.last_report = {
                "state": "ready" if ready else "blocked",
                "message": (
                    "本次有界执行准备通过，尚未开始"
                    if ready
                    else ("过期记录，需用户处理" if expired else "准备条件尚未全部通过，未开始乐跑")
                ),
                "checks": checks,
                "ready": ready,
                "run_started": False,
                "records_submitted": 0,
                "needs_human_verification_if_requested": True,
                "login_server_auth_proven": False,
            }
            return self.last_report
        except Exception:
            # 异步任务异常也要产生终态；不让客户端永远checking或回显原始异常。
            self.last_report = {
                "state": "blocked",
                "message": "准备检查未完成；未开始乐跑，请稍后重新检查",
                "checks": [],
                "ready": False,
                "run_started": False,
                "records_submitted": 0,
            }
            return self.last_report
        finally:
            self.lock.release()

    def status(self) -> dict:
        if self.lock.locked():
            return {
                "state": "checking",
                "message": "正在核对当前客户端，请等待本次结果",
                "checks": [],
                "ready": False,
                "run_started": False,
                "records_submitted": 0,
            }
        return self.last_report


class LauncherServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        coordinator: PreparationCoordinator,
        token: str,
        port: int = 8766,
        *,
        runner_factory=None,
    ):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
            raise ValueError("启动助手需要本机配对密钥，不接受空密钥")
        self.coordinator, self.token = coordinator, token
        self.execution = ExecutionCoordinator(coordinator, runner_factory)
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
            self.reply(200, self.server.execution.status())
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
        if self.path not in {"/v1/check", "/v1/start", "/v1/stop"}:
            self.reply(404, {"message": "接口不存在"})
            return
        try:
            data = json.loads(body)
            if (
                self.headers.get("Content-Type") != "application/json"
                or not isinstance(data, dict)
                or (
                    set(data) != set()
                    if self.path == "/v1/stop"
                    else set(data) != {"photo_ready"} or type(data["photo_ready"]) is not bool
                )
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            self.reply(400, {"message": "请求结构不符合要求；不回显输入"})
            return
        # 登录准备可正常打开客户端；未通过运行条件时不开始跑步、位置输出或上传。
        if self.path == "/v1/stop":
            report = self.server.execution.stop()
            self.reply(202 if report["state"] == "stopping" else 200, report)
            return
        report = self.server.execution.begin(
            start=self.path == "/v1/start", photo_ready=data["photo_ready"]
        )
        active = report["state"] in {
            "checking",
            "preparing",
            "starting",
            "running",
            "stopping",
            "finishing",
            "verification_required",
            "recovering",
        }
        code = 202 if active else (200 if self.path == "/v1/check" else 409)
        self.reply(code, report)


def serve_launcher(
    root: Path,
    profile: Path,
    config_file: Path,
    port: int = 8766,
    *,
    runtime_file: Path | None = None,
):
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
    runtime = None
    if runtime_file is not None:
        from .live_runtime import LiveRuntime

        runtime = LiveRuntime(root, profile, runtime_file)
    coordinator = PreparationCoordinator(
        root,
        profile,
        login_preparer=runtime.prepare_login if runtime else prepare_current_login,
        execution_preflight=runtime.preflight if runtime else None,
    )
    with LauncherServer(
        coordinator, token, port, runner_factory=runtime.build if runtime else None
    ) as server:
        server.serve_forever()
