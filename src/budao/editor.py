"""只绑定loopback的路线编辑器；实际配置仅保存到忽略的.local目录。"""

from __future__ import annotations

import json
import secrets
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from .models import RouteConfig
from .routes import RoutePlanner, path_length


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 8765, output: Path = Path(".local/routes")):
        if ".local" not in output.resolve().parts:
            raise ValueError("实际路线只能保存到.local目录")
        self.output, self.token = output.resolve(), secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), EditorHandler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorServer

    def log_message(self, *_args) -> None:
        pass  # 不把实际坐标、文件内容或运行配置写进公共日志。

    def send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def json(self, status: int, value: dict) -> None:
        self.send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def host_allowed(self) -> bool:
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def do_GET(self) -> None:
        if not self.host_allowed():
            self.json(403, {"error": "仅接受本机127.0.0.1地址"})
            return
        resources = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/editor.js": ("editor.js", "application/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/vendor/leaflet.js": ("vendor/leaflet.js", "application/javascript"),
            "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css"),
        }
        if self.path not in resources:
            self.json(404, {"error": "资源不存在"})
            return
        name, content_type = resources[self.path]
        data = files("budao").joinpath("web", name).read_bytes()
        if name == "index.html":
            data = data.replace(b"__LOCAL_TOKEN__", self.server.token.encode())
        self.send(200, data, content_type)

    def do_POST(self) -> None:
        # Windows上带未读请求体关闭socket可能发送RST，吞掉403响应。
        # 在有界长度和超时下先读取，拒绝请求也能给浏览器明确结果。
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 512 * 1024:
                raise ValueError("配置大小必须在512KiB以内")
            self.connection.settimeout(5)
            body = self.rfile.read(size)
        except (ValueError, OSError) as error:
            self.json(400, {"error": str(error)[:200]})
            return
        if (
            not self.host_allowed()
            or self.headers.get("Origin") != self.server.origin
            or self.headers.get("X-Local-Token") != self.server.token
            or self.headers.get("Content-Type") != "application/json"
        ):
            self.json(403, {"error": "拒绝非本机编辑页面的请求"})
            return
        if self.path not in {"/api/validate", "/api/save"}:
            self.json(404, {"error": "接口不存在"})
            return
        try:
            config = RouteConfig.model_validate_json(body)
            point_count = (
                len(config.region) + len(config.baseline) + sum(map(len, config.corridors))
            )
            if point_count > 2000 or len(config.corridors) > 100:
                raise ValueError("小区域编辑最多2000点、100条通道")
            planner = RoutePlanner(config)
            result = {
                "valid": True,
                "baseline_metres": round(path_length(config.baseline), 2),
                "closed_baseline": config.baseline[0] == config.baseline[-1],
                "nodes": planner.graph.number_of_nodes(),
                "real_records_submitted": 0,
            }
            if self.path == "/api/save":
                self.server.output.mkdir(parents=True, exist_ok=True)
                path = self.server.output / f"edited-{uuid.uuid4().hex}.json"
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(config.model_dump_json(indent=2))
                result["saved_local_path"] = str(path)
            self.json(200, result)
        except (ValueError, OSError) as error:
            self.json(400, {"error": str(error)[:500]})


def serve_editor(port: int = 8765) -> None:
    with EditorServer(port) as server:
        print(f"路线编辑器：{server.origin}；只保存.local，不调用ADB/模型", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
