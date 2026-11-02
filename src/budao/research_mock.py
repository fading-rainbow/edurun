"""Loopback-only, synthetic protocol exercise; never talks to the target service."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

PREFIX = "/mock/"
CHECKPOINTS = ("mock-a", "mock-b", "mock-c")


def signature(key: bytes, method: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    canonical = "\n".join((method, path, stamp, nonce, hashlib.sha256(body).hexdigest()))
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


class MockProtocol:
    """All distances, identity verdicts and review rules here are hypothetical fixtures."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()

    @staticmethod
    def state(session: dict) -> dict:
        result = {
            key: session[key]
            for key in (
                "phase",
                "identity",
                "distance_m",
                "elapsed_s",
                "checkpoints",
                "effective_count",
            )
        }
        result["checkpoints"] = list(session["checkpoints"])
        return result

    def handle(self, method: str, path: str, headers: dict, raw: bytes) -> tuple[int, dict]:
        with self.lock:
            return self._handle(method, path, headers, raw)

    def _handle(self, method: str, path: str, headers: dict, raw: bytes) -> tuple[int, dict]:
        if len(raw) > 16384:
            return 413, {"code": "BODY_LIMIT"}
        if method == "POST" and path == "/mock/bootstrap":
            if len(self.sessions) >= 8:
                return 429, {"code": "SESSION_LIMIT"}
            sid, key = secrets.token_hex(12), secrets.token_bytes(32)
            self.sessions[sid] = {
                "key": key,
                "nonces": set(),
                "phase": "ready",
                "identity": False,
                "distance_m": 0,
                "elapsed_s": 0,
                "checkpoints": [],
                "effective_count": 0,
            }
            return 200, {"code": "SYNTHETIC_SESSION", "session": sid, "demo_key": key.hex()}
        if not path.startswith(PREFIX):
            return 404, {"code": "MOCK_ROUTES_ONLY"}
        headers = {key.lower(): value for key, value in headers.items()}
        session = self.sessions.get(headers.get("x-lab-session", ""))
        if session is None:
            return 401, {"code": "SESSION_REQUIRED"}
        stamp, nonce = headers.get("x-lab-time", ""), headers.get("x-lab-nonce", "")
        if not stamp.isascii() or not stamp.isdigit() or len(stamp) > 12:
            return 401, {"code": "TIMESTAMP"}
        if abs(int(self.clock()) - int(stamp)) > 120:
            return 401, {"code": "TIMESTAMP"}
        expected = signature(session["key"], method, path, stamp, nonce, raw)
        supplied = headers.get("x-lab-signature", "")
        if not re.fullmatch(r"[a-f0-9]{64}", supplied) or not hmac.compare_digest(
            expected, supplied
        ):
            return 401, {"code": "SIGNATURE"}
        if not re.fullmatch(r"[a-f0-9]{32}", nonce):
            return 400, {"code": "NONCE_FORMAT"}
        if nonce in session["nonces"]:
            return 409, {"code": "REPLAY"}
        if len(session["nonces"]) >= 1024:
            return 429, {"code": "NONCE_LIMIT"}
        session["nonces"].add(nonce)
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            return 400, {"code": "JSON"}
        if not isinstance(body, dict):
            return 400, {"code": "OBJECT_REQUIRED"}

        def response(status: int, code: str) -> tuple[int, dict]:
            return status, {"code": code, "state": self.state(session)}

        if method == "GET" and path == "/mock/state":
            return response(200, "STATE")
        if method != "POST":
            return response(405, "METHOD")
        if path == "/mock/identity-fixture":
            if session["phase"] != "ready" or body != {"synthetic": True, "result": "pass"}:
                return response(412, "SYNTHETIC_IDENTITY_REQUIRED")
            session["identity"] = True
            return response(200, "IDENTITY_FIXTURE_PASSED")
        if path == "/mock/run/start":
            if not session["identity"]:
                return response(412, "IDENTITY_REQUIRED")
            if session["phase"] != "ready":
                return response(409, "WRONG_STATE")
            session["phase"] = "running"
            return response(200, "RUNNING")
        if path == "/mock/run/progress":
            if session["phase"] != "running":
                return response(409, "WRONG_STATE")
            distance, elapsed = body.get("distance_m"), body.get("elapsed_s")
            if any(
                type(v) not in (int, float) or (type(v) is float and not math.isfinite(v))
                for v in (distance, elapsed)
            ):
                return response(422, "NUMERIC_PROGRESS_REQUIRED")
            if not session["distance_m"] <= distance <= 10000 or not 0 < elapsed <= 7200:
                return response(422, "PROGRESS_RANGE")
            if elapsed < session["elapsed_s"]:
                return response(422, "PROGRESS_ORDER")
            checkpoint = body.get("checkpoint")
            completed = len(session["checkpoints"])
            if completed >= len(CHECKPOINTS) or checkpoint != CHECKPOINTS[completed]:
                return response(422, "CHECKPOINT_ORDER")
            session["distance_m"], session["elapsed_s"] = distance, elapsed
            session["checkpoints"].append(checkpoint)
            return response(200, "PROGRESS")
        if path == "/mock/run/finish":
            if session["phase"] != "running":
                return response(409, "WRONG_STATE")
            if session["distance_m"] < 1000 or len(session["checkpoints"]) != 3:
                return response(412, "INCOMPLETE")
            pace = session["elapsed_s"] * 1000 / session["distance_m"]
            if not 120 <= pace <= 900:
                return response(412, "MOCK_PACE_RANGE")
            # Client claims such as {"valid": true} never advance the accepted count.
            session["phase"] = "pending_review"
            return response(200, "PENDING_REVIEW")
        if path == "/mock/review-fixture":
            if session["phase"] != "pending_review":
                return response(409, "WRONG_STATE")
            if body.get("synthetic") is not True or body.get("decision") not in (
                "accept",
                "reject",
            ):
                return response(422, "SYNTHETIC_REVIEW_REQUIRED")
            session["phase"] = "accepted" if body["decision"] == "accept" else "rejected"
            session["effective_count"] += body["decision"] == "accept"
            return response(200, session["phase"].upper())
        return response(404, "MOCK_ROUTES_ONLY")


@contextmanager
def local_mock():
    engine = MockProtocol()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # Never persist HTTP headers, demo keys, nonces or request bodies.

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 16384:
                    status, result = 413, {"code": "BODY_LIMIT"}
                else:
                    raw = self.rfile.read(size)
                    status, result = engine.handle(self.command, self.path, dict(self.headers), raw)
            except (ValueError, TimeoutError):
                status, result = 400, {"code": "REQUEST"}
            payload = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def run_mock_demo() -> dict:
    """Actual loopback HTTP requests, generated fake credentials, no production URL."""
    with local_mock() as url, httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
        bootstrap = client.post("/mock/bootstrap").json()
        key, sid = bytes.fromhex(bootstrap["demo_key"]), bootstrap["session"]
        events = []

        def send(path, body=None, *, nonce=None, stamp=None):
            raw = json.dumps(body or {}, sort_keys=True, separators=(",", ":")).encode()
            nonce, stamp = nonce or secrets.token_hex(16), stamp or str(int(time.time()))
            headers = {
                "X-Lab-Session": sid,
                "X-Lab-Time": stamp,
                "X-Lab-Nonce": nonce,
                "X-Lab-Signature": signature(key, "POST", path, stamp, nonce, raw),
            }
            response = client.post(path, content=raw, headers=headers)
            result = response.json()
            events.append(
                {"path": path, "http_status": response.status_code, "code": result["code"]}
            )
            return result

        missing_identity = send("/mock/run/start")["code"]
        send("/mock/identity-fixture", {"synthetic": True, "result": "pass"})
        reused_nonce = secrets.token_hex(16)
        send("/mock/run/start", nonce=reused_nonce)
        replay = send("/mock/run/start", nonce=reused_nonce)["code"]
        incomplete = send("/mock/run/finish", {"valid": True})["code"]
        for point, distance, elapsed in zip(
            CHECKPOINTS, (300, 650, 1000), (180, 390, 600), strict=True
        ):
            send(
                "/mock/run/progress",
                {"checkpoint": point, "distance_m": distance, "elapsed_s": elapsed},
            )
        pending = send("/mock/run/finish", {"valid": True})
        accepted = send("/mock/review-fixture", {"synthetic": True, "decision": "accept"})
        duplicate = send("/mock/review-fixture", {"synthetic": True, "decision": "accept"})
        passed = (
            missing_identity == "IDENTITY_REQUIRED"
            and replay == "REPLAY"
            and incomplete == "INCOMPLETE"
            and pending["state"]["effective_count"] == 0
            and accepted["state"]["effective_count"] == 1
            and duplicate["code"] == "WRONG_STATE"
            and duplicate["state"]["effective_count"] == 1
        )
        return {
            "passed": passed,
            "synthetic_only": True,
            "loopback_only": True,
            "events": events,
            "pending_review_effective_count": 0,
            "accepted_effective_count": accepted["state"]["effective_count"],
            "real_app_records_created": 0,
            "demo_secrets_returned": False,
            "limitations": [
                "Hypothetical protocol and thresholds, not recovered target endpoints.",
                "Identity and review endpoints are synthetic fixtures, not biometric validation.",
            ],
        }
