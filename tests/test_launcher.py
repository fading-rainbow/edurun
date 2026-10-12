import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from budao.launcher import LauncherServer, PreparationCoordinator


def unavailable(*_args, **_kwargs):
    raise ValueError("Generic simulated device failure")


@contextmanager
def server(tmp_path):
    coordinator = PreparationCoordinator(
        tmp_path, Path(".local/profiles/current/profile.json"), bridge_factory=unavailable
    )
    with LauncherServer(coordinator, "x" * 43, 0) as app:
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{app.server_port}", trust_env=False
            ) as client:
                yield app, client
        finally:
            app.shutdown()
            thread.join(3)


def headers():
    return {"X-Launcher-Token": "x" * 43}


def test_one_click_preparation_reports_real_missing_gates_without_starting(tmp_path):
    with server(tmp_path) as (_app, client):
        result = client.post("/v1/start", headers=headers(), json={"photo_ready": True})
        assert result.status_code == 409
        report = result.json()
        assert not report["ready"] and not report["run_started"]
        assert report["records_submitted"] == 0
        checks = {c["key"]: c for c in report["checks"]}
        assert checks["photo"]["state"] == "pass"
        assert checks["login"]["state"] == "unknown"
        assert checks["device"]["state"] == "not_ready"
        status = client.get("/v1/status", headers=headers()).json()
        assert status == report


def test_pairing_required_no_secrets_returned(tmp_path):
    with server(tmp_path) as (_app, client):
        assert client.get("/v1/status").status_code == 403
        assert client.post("/v1/start", json={"photo_ready": True}).status_code == 403
        response = client.post("/v1/check", headers=headers(), json={"photo_ready": False})
        assert response.status_code == 200 and "x" * 43 not in response.text
        assert (
            client.get("/.local/launcher/current/service.json", headers=headers()).status_code
            == 404
        )


def test_device_query_timeout_becomes_not_ready_without_echo(tmp_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("PRIVATE_TEST_ARGUMENT", 15)

    coordinator = PreparationCoordinator(
        tmp_path, Path(".local/missing.json"), bridge_factory=timeout
    )
    report = coordinator.check()
    assert "PRIVATE_TEST_ARGUMENT" not in str(report)
    assert next(c for c in report["checks"] if c["key"] == "device")["state"] == "not_ready"


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1"])
def test_browser_origin_cannot_trigger_start(tmp_path, origin):
    with server(tmp_path) as (_app, client):
        assert (
            client.post(
                "/v1/start", headers={**headers(), "Origin": origin}, json={"photo_ready": True}
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    "body",
    [{}, {"photo_ready": 1}, {"photo_ready": "true"}, [], {"password": "HIDDEN_TEST_SECRET"}],
)
def test_unknown_inputs_rejected_without_echo(tmp_path, body):
    with server(tmp_path) as (_app, client):
        response = client.post("/v1/start", headers=headers(), json=body)
        assert response.status_code == 400 and "HIDDEN_TEST_SECRET" not in response.text


def test_wrong_host_and_large_requests_rejected(tmp_path):
    with server(tmp_path) as (_app, client):
        assert (
            client.get("/v1/status", headers={**headers(), "Host": "evil.example"}).status_code
            == 403
        )
        assert client.post("/v1/start", headers=headers(), content=b"x" * 2049).status_code == 400


def test_native_client_is_loopback_only_and_does_not_handle_account_credentials():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android/launcher/src/com/edurun/launcher/LauncherActivity.java").read_text(
        "utf-8"
    )
    manifest = (root / "android/launcher/AndroidManifest.xml").read_text("utf-8")
    assert 'new URL("http://127.0.0.1:8766" + path)' in source
    assert "connection.setInstanceFollowRedirects(false)" in source
    assert 'new JSONObject().put("photo_ready", ready)' in source
    assert "Intent.ACTION_OPEN_DOCUMENT" in source
    assert 'android:allowBackup="false"' in manifest
    assert "android.permission.INTERNET" in manifest
    assert "android.permission.CAMERA" not in manifest
    assert "TYPE_STEP_COUNTER" not in source
    assert not any(s in source for s in ["token.542576.xyz", "com.tencent.mobileqq", "password"])


def test_cli_exposes_preparation_service_without_default_device_start(monkeypatch):
    import sys

    from budao import cli

    calls = []
    monkeypatch.setattr(cli, "serve_launcher", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["budao", "launcher-service"])
    assert cli.main() == 0
    assert calls[0][1:] == (
        Path(".local/profiles/current/profile.json"),
        Path(".local/launcher/current/service.json"),
    )
