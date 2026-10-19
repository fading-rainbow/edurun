import subprocess
import threading
import time
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


def wait_report(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        report = client.get("/v1/status", headers=headers()).json()
        if report["state"] != "checking":
            return report
        time.sleep(0.01)
    pytest.fail("Asynchronous preparation did not finish")


def test_one_click_preparation_reports_real_missing_gates_without_starting(tmp_path):
    with server(tmp_path) as (_app, client):
        result = client.post("/v1/start", headers=headers(), json={"photo_ready": True})
        assert result.status_code in {202, 409}
        report = wait_report(client)
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
        assert response.status_code in {200, 202} and "x" * 43 not in response.text
        wait_report(client)
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


def test_preparation_uses_this_live_login_result_but_still_blocks_unfinished_runtime(tmp_path):
    calls = []

    class Device:
        def check_device(self):
            pass

    app = PreparationCoordinator(
        tmp_path,
        Path(".local/missing.json"),
        bridge_factory=lambda _: Device(),
        login_preparer=lambda b: (
            calls.append(b) or {"state": "logged_in_ui", "server_auth_proven": False}
        ),
    )
    report = app.check(True)
    assert (
        len(calls) == 1
        and next(c for c in report["checks"] if c["key"] == "login")["state"] == "pass"
    )
    assert (
        not report["login_server_auth_proven"] and not report["ready"] and not report["run_started"]
    )
    assert report["records_submitted"] == 0


def test_repeated_check_does_not_queue_navigation_or_block_status(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Device:
        def check_device(self):
            pass

    def prepare(device):
        calls.append(device)
        started.set()
        release.wait(3)
        return {"state": "logged_in_ui"}

    app = PreparationCoordinator(
        tmp_path,
        Path(".local/missing.json"),
        bridge_factory=lambda _: Device(),
        login_preparer=prepare,
    )
    task = threading.Thread(target=lambda: app.check())
    task.start()
    try:
        assert started.wait(1)
        assert app.status()["state"] == "checking"
        assert app.check()["state"] == "checking"
        assert len(calls) == 1
    finally:
        release.set()
        task.join(3)
    assert app.status()["state"] == "blocked"


def test_http_check_acknowledges_before_slow_navigation_and_never_queues(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Device:
        def check_device(self):
            pass

    def prepare(device):
        calls.append(device)
        started.set()
        release.wait(3)
        return {"state": "logged_in_ui"}

    with server(tmp_path) as (app, client):
        app.coordinator.bridge_factory = lambda _: Device()
        app.coordinator.login_preparer = prepare
        try:
            first = client.post("/v1/check", headers=headers(), json={"photo_ready": True})
            assert first.status_code == 202 and first.json()["state"] == "checking"
            assert started.wait(1)
            for _ in range(3):
                repeat = client.post("/v1/start", headers=headers(), json={"photo_ready": True})
                assert repeat.status_code == 202
                assert client.get("/v1/status", headers=headers()).json()["state"] == "checking"
            assert len(calls) == 1
        finally:
            release.set()
        report = wait_report(client)
        assert next(c for c in report["checks"] if c["key"] == "login")["state"] == "pass"
        assert not report["run_started"] and report["records_submitted"] == 0


def test_native_polling_does_not_reissue_post_on_slow_or_disconnected_check():
    source = (
        Path(__file__).resolve().parents[1]
        / "android/launcher/src/com/edurun/launcher/LauncherActivity.java"
    ).read_text("utf-8")
    assert 'exchange("/v1/status", token, null)' in source
    assert source.count('exchange(path, token, new JSONObject().put("photo_ready", ready))') == 1
    assert "Thread.sleep(500)" in source
    assert "检查可能仍在进行" in source


def test_async_unexpected_failure_has_terminal_redacted_report(tmp_path, monkeypatch):
    def fail(*_args):
        raise RuntimeError("PRIVATE_TEST_ERROR")

    monkeypatch.setattr("budao.launcher.preflight_profile", fail)
    with server(tmp_path) as (_app, client):
        client.post("/v1/check", headers=headers(), json={"photo_ready": True})
        report = wait_report(client)
        assert report["state"] == "blocked" and not report["run_started"]
        assert "PRIVATE_TEST_ERROR" not in str(report)
        assert not _app.coordinator.lock.locked()


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


@pytest.mark.parametrize("size", [2049, 4096, 65536])
def test_rejected_small_oversize_body_is_drained_without_start_or_reset(tmp_path, size):
    with server(tmp_path) as (_app, client):
        for _ in range(5):
            response = client.post("/v1/start", headers=headers(), content=b"x" * size)
            assert response.status_code == 400
        assert _app.coordinator.status()["state"] == "unchecked"


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


def test_authenticated_stop_requires_empty_body_and_does_not_end_or_save_a_run(tmp_path):
    with server(tmp_path) as (app, client):
        assert client.post("/v1/stop", json={}).status_code == 403
        assert (
            client.post("/v1/stop", headers=headers(), json={"photo_ready": True}).status_code
            == 400
        )
        result = client.post("/v1/stop", headers=headers(), json={})
        assert result.status_code == 200 and app.execution.status()["state"] == "unchecked"


def test_native_stop_has_its_own_control_executor_and_long_poll_never_reposts_start():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android/launcher/src/com/edurun/launcher/LauncherActivity.java").read_text(
        "utf-8"
    )
    assert (
        "controls.execute" in source
        and 'exchange("/v1/stop", pairedToken(), new JSONObject())' in source
    )
    assert '"/v1/start".equals(path) ? 3000000 : 60000' in source
    assert '"running".equals(state)' in source and '"stopping".equals(state)' in source
    assert source.count('exchange(path, token, new JSONObject().put("photo_ready", ready))') == 1


@pytest.mark.parametrize("clock_expired", [False, True])
def test_expired_record_preparation_and_http_status_not_permanently_busy(tmp_path, clock_expired):
    from budao.resume import PAUSE_CLOCK_DETAIL

    detail = PAUSE_CLOCK_DETAIL if clock_expired else "过期记录，需用户处理"

    class Device:
        def check_device(self):
            pass

    with server(tmp_path) as (app, client):
        app.coordinator.bridge_factory = lambda _: Device()
        app.coordinator.login_preparer = lambda _b: {
            "state": "pause_clock_expired" if clock_expired else "expired_record",
            "detail": detail,
            "server_auth_proven": False,
        }
        # Check endpoint
        check_resp = client.post("/v1/check", headers=headers(), json={"photo_ready": True})
        assert check_resp.status_code in {200, 202}
        report = wait_report(client)
        assert report["state"] == "blocked"
        assert report["message"] == detail
        assert not report["ready"] and not report["run_started"]
        login_check = next(c for c in report["checks"] if c["key"] == "login")
        assert login_check["state"] == "unknown"
        assert login_check["detail"] == detail

        # Start endpoint returns 409 blocked with same message and releases slot immediately
        start_resp = client.post("/v1/start", headers=headers(), json={"photo_ready": True})
        assert start_resp.status_code in {202, 409}
        start_report = wait_report(client)
        assert start_report["state"] == "blocked"
        assert start_report["message"] == detail
        assert not start_report["run_started"]
        assert not app.execution.slot.locked()


GENERIC_LOGIN_DETAIL = "当前客户端登录界面未确认；已有会话/必要认证不被覆盖"
EXPIRED_DETAIL = "过期记录，需用户处理"
CLOCK_DETAIL = "暂停证据时钟已失效，需用户处理"


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        ({"state": "expired_record", "detail": EXPIRED_DETAIL}, EXPIRED_DETAIL),
        ({"state": "pause_clock_expired", "detail": CLOCK_DETAIL}, CLOCK_DETAIL),
        ({"state": "pause_clock_expired", "detail": "private / secret"}, GENERIC_LOGIN_DETAIL),
        ({"state": "pause_clock_expired", "detail": CLOCK_DETAIL + " "}, GENERIC_LOGIN_DETAIL),
        ({"state": "unknown", "detail": CLOCK_DETAIL}, GENERIC_LOGIN_DETAIL),
        ({"state": "expired_record", "detail": "unexpected text"}, GENERIC_LOGIN_DETAIL),
        ({"state": "expired_record", "detail": EXPIRED_DETAIL + " "}, GENERIC_LOGIN_DETAIL),
        ({"state": "unknown", "detail": "PRIVATE_TEST_TOKEN account=abc"}, GENERIC_LOGIN_DETAIL),
        (
            {"state": "unknown", "detail": r"C:\Users\private\.local\profile.json"},
            GENERIC_LOGIN_DETAIL,
        ),
        (
            {"state": "expired_record", "detail": "ValueError: /home/x/secret"},
            GENERIC_LOGIN_DETAIL,
        ),
        ({"state": "expired_record", "detail": None}, GENERIC_LOGIN_DETAIL),
        ({"state": "expired_record", "detail": 123}, GENERIC_LOGIN_DETAIL),
        ({"state": "expired_record", "detail": ["x"]}, GENERIC_LOGIN_DETAIL),
        ({"state": "unknown", "detail": EXPIRED_DETAIL}, GENERIC_LOGIN_DETAIL),
        ({"state": "logged_in_ui", "detail": EXPIRED_DETAIL}, None),
        ({"state": "logged_in_ui", "detail": r"D:\private\path"}, None),
    ],
)
def test_login_detail_is_whitelisted_in_public_report(tmp_path, login, expected):
    class Device:
        def check_device(self):
            pass

    app = PreparationCoordinator(
        tmp_path,
        Path(".local/missing.json"),
        bridge_factory=lambda _: Device(),
        login_preparer=lambda _b: login,
    )
    report = app.check(True)
    check = next(c for c in report["checks"] if c["key"] == "login")
    if expected is None:
        assert check["state"] == "pass"
        assert check["detail"] == "本次已登录界面已核对；服务端授权仍由实际开始流程复核"
    else:
        assert check["state"] == "unknown"
        assert check["detail"] == expected
    text = str(report)
    for leaked in ("PRIVATE_TEST_TOKEN", "private", "unexpected text", "ValueError", "secret"):
        assert leaked not in text
