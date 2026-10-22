import json
from pathlib import Path

import pytest
from test_raw_motion import Device as BaseDevice
from test_raw_motion import clip, packet

from budao.android import DIAGNOSTIC_PACKAGE
from budao.foreground_motion import (
    OBSERVATION_COMMAND,
    RECEIVER,
    SERVICE,
    ForegroundMotionClient,
    cross_foreground_probe,
    receiver_observation,
)
from budao.sensor_probe import INJECTION


class Device(BaseDevice):
    def __init__(self, clip, *, wrong_focus=False, expired=False, same_uid=False):
        super().__init__(clip)
        self.wrong_focus, self.expired, self.same_uid = wrong_focus, expired, same_uid
        self.batches = 0
        self.done = False
        self.started = False

    def run(self, *args):
        if args == ("shell", OBSERVATION_COMMAND):
            self.calls.append(args)
            self.batches += 1
            focus = "foreign/.Page" if self.wrong_focus else "com.edurun.receiver/.ReceiverActivity"
            rx = {
                "package": RECEIVER,
                "request_id": self.request,
                "registered": True,
                "elapsed_realtime_ns": 30_000_000_000,
                "event_count": 100,
                "uid": 10045,
                "capture_done": self.done,
            }
            tx = None
            if self.started:
                p = packet(self.clip, request=self.request)
                if self.expired:
                    q = p["raw_motion_probe"]
                    q.update(
                        state="lease_expired",
                        lease_required=True,
                        sent_rows=25,
                        sent_samples=q["sent_samples"][:50],
                        lease_deadline_ns=13_000_000_000,
                    )
                elif self.batches < 5:
                    p["raw_motion_probe"]["state"] = "running"
                tx = {
                    **p,
                    "package": DIAGNOSTIC_PACKAGE,
                    "request_id": self.request,
                    "uid": 10045 if self.same_uid else 10041,
                }
                if self.done:
                    rx["received_samples"] = p["raw_motion_probe"]["received_samples"]
            return (
                "mCurrentFocus=Window{123 u0 "
                + focus
                + "}\n__EDURUN_RX__\n"
                + json.dumps(rx)
                + "\n__EDURUN_SVC__\n"
                + json.dumps(tx)
            )
        if args[:3] == ("shell", "am", "start"):
            self.calls.append(args)
            self.request = args[args.index("receiver_request") + 1]
            self.done = "capture_done" in args
            return ""
        if args[:3] in (
            ("shell", "am", "startservice"),
            ("shell", "am", "start-foreground-service"),
        ):
            self.calls.append(args)
            if DIAGNOSTIC_PACKAGE + ".MOTION_START" in args:
                self.started = True
            return ""
        return super().run(*args)


def test_foreground_client_requires_prepare_and_cannot_duplicate_start(tmp_path):
    c = clip(tmp_path)
    d = Device(c)
    client = ForegroundMotionClient(d)
    with pytest.raises(ValueError, match="准备"):
        client.start(12_000_000_000)
    client.prepare(c)
    d.mode = INJECTION
    client.start(12_000_000_000)
    with pytest.raises(ValueError, match="重复"):
        client.start(12_000_000_001)
    client.renew(12_100_000_000)
    with pytest.raises(ValueError, match="纳秒"):
        client.renew(True)
    client.stop()
    assert ("--ei", "motion_lease_ms", "2000") == d.calls[-1][-3:]


@pytest.mark.parametrize("prepared_only", [False, True])
def test_stop_is_idempotent_after_ack_and_never_restarts_force_stopped_service(
    tmp_path, prepared_only
):
    c = clip(tmp_path)
    d = Device(c)
    client = ForegroundMotionClient(d)
    client.preload_requested = prepared_only
    client.started = not prepared_only
    client.stop()
    d.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    count = len(d.calls)
    client.stop()
    assert len(d.calls) == count and client.stop_acknowledged
    assert sum(DIAGNOSTIC_PACKAGE + ".MOTION_STOP" in call for call in d.calls) == 1
    for action in (
        lambda: client.prepare(c),
        lambda: client.preload(c),
        lambda: client.start(12_000_000_000),
        lambda: client.renew(12_000_000_000),
        client.renewal_observation_command,
    ):
        with pytest.raises(ValueError, match="停止"):
            action()
    assert len(d.calls) == count


def test_uncertain_stop_is_not_retried_or_reported_as_acknowledged(tmp_path, monkeypatch):
    client = ForegroundMotionClient(Device(clip(tmp_path)))
    client.started = True
    calls = []

    def timeout(action):
        calls.append(action)
        raise TimeoutError("PRIVATE_STOP_RESULT_UNKNOWN")

    monkeypatch.setattr(client, "_command", timeout)
    with pytest.raises(TimeoutError):
        client.stop()
    with pytest.raises(RuntimeError, match="不重复"):
        client.stop()
    assert calls == ["STOP"] and client.stop_attempted and not client.stop_acknowledged
    with pytest.raises(ValueError, match="停止"):
        client.renew(12_000_000_000)


def test_batched_foreground_observation_is_only_guarded_owned_files(tmp_path):
    d = Device(clip(tmp_path))
    client = ForegroundMotionClient(d)
    d.request = client.request
    rx, tx = receiver_observation(d, client.request)
    assert rx["registered"] and tx is None
    assert d.calls == [("shell", OBSERVATION_COMMAND)]
    assert "run-as com.edurun.receiver" in OBSERVATION_COMMAND
    assert "com.lptiyu.tanke" not in OBSERVATION_COMMAND
    d.wrong_focus = True
    with pytest.raises(ValueError, match="不续期"):
        receiver_observation(d, client.request)


def test_independent_foreground_receiving_and_mode_restore(tmp_path):
    c = clip(tmp_path)
    d = Device(c)
    r = cross_foreground_probe(d, c, sleep=lambda _: None, clock=iter(range(100)).__next__)
    assert r["passed"] and r["clip_received_complete"]
    assert r["renewals"] > 0
    assert r["sender"]["uid"] != r["receiver"]["uid"]
    assert not r["target_foreground_motion_verified"] and not r["target_steps_verified"]
    assert r["formal_runs_started"] == r["records_submitted"] == 0
    assert all(
        SERVICE in call
        for call in d.calls
        if "startservice" in call or "start-foreground-service" in call
    )


def test_withheld_observation_cannot_renew_and_expiry_not_full_replay(tmp_path):
    c = clip(tmp_path, seconds=3)
    d = Device(c, expired=True)
    r = cross_foreground_probe(
        d, c, withhold_renewal=True, sleep=lambda _: None, clock=iter(range(100)).__next__
    )
    assert r["passed"] and r["lease_expiry_stop_verified"]
    assert r["renewals"] == 0
    assert not r.get("clip_received_complete", False)
    assert not any(DIAGNOSTIC_PACKAGE + ".MOTION_RENEW" in call for call in d.calls)


def test_same_uid_is_not_independent_and_still_restored(tmp_path):
    c = clip(tmp_path)
    d = Device(c, same_uid=True)
    r = cross_foreground_probe(d, c, sleep=lambda _: None, clock=iter(range(100)).__next__)
    assert not r["passed"] and r["mode_restored_normal"]
    assert r["normal_stream_resumed"]


def test_native_service_stops_nonsticky_and_protects_lease_and_duplicate_nonce():
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionService.java"
    ).read_text("utf-8")
    probe = (root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionProbe.java").read_text(
        "utf-8"
    )
    manifest = (root / "android/diagnostic/AndroidManifest.xml").read_text("utf-8")
    assert "android.permission.ACCESS_BACKGROUND_LOCATION" in manifest
    assert "START_NOT_STICKY" in source and "startForeground" in source
    assert 'android:permission="android.permission.DUMP"' in manifest
    assert "motion-last-request.txt" in source
    assert "now >= leaseDeadlineNs" in probe and "ttlMs > 2000" in probe
    assert "observedNs <= lastObservationNs" in probe
    assert "now-observedNs > 1_000_000_000L" in probe
    assert "leaseDeadlineNs = observedNs + ttlMs*1_000_000L" in probe
    assert "!active || !leaseRequired || !requestId.equals(request)" in probe
    assert 'finish("lease_expired")' in probe


def test_running_snapshot_keeps_audit_prefixes_out_of_the_send_thread_lock_copy():
    root = Path(__file__).resolve().parents[1]
    probe = (root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionProbe.java").read_text(
        "utf-8"
    )
    assert "if (boundedStream && !active)" in probe
    assert 'report.remove("sent_audit_prefix")' in probe
    assert 'report.remove("received_audit_prefix")' in probe
    # 仅减少活跃快照复制；原始审计缓存/停止后输出、100ms质量守卫不取消。
    assert 'report.put("sent_audit_prefix", sent)' in probe
    assert 'report.put("received_audit_prefix", received)' in probe
    assert "gap > 100_000_000L" in probe and 'finish("clock_gap")' in probe


def test_receiver_has_no_network_location_injection_or_user_auth_access():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android/receiver/src/com/edurun/receiver/ReceiverActivity.java").read_text(
        "utf-8"
    )
    manifest = (root / "android/receiver/AndroidManifest.xml").read_text("utf-8")
    assert "onSensorChanged" in source and "Process.myUid()" in source
    assert "captureDone" in source and "events.length() >= 8192" in source
    assert "INTERNET" not in manifest and "LOCATION" not in manifest
    assert "injectSensorData" not in source and "TYPE_STEP_COUNTER" not in source


def test_merged_renewal_preserves_nanoseconds_as_decimal_not_32bit_shell_arithmetic(tmp_path):
    d = Device(clip(tmp_path))
    c = ForegroundMotionClient(d)
    with pytest.raises(ValueError, match="未开始"):
        c.renewal_observation_command()
    c.started = True
    command = c.renewal_observation_command()
    assert "${seconds}${fraction}0000000" in command and "$(( " not in command
    assert "grep -c" in command and "motion_lease_ms 2000" in command


def test_gps_guard_revalidates_copied_model_before_shell_interpolation(tmp_path):
    from budao.models import GeoPoint

    c = ForegroundMotionClient(Device(clip(tmp_path)))
    c.started = True
    p = GeoPoint(lon=120, lat=30)
    for value in (
        p.model_copy(update={"lon": "120; echo UNTRUSTED"}),
        p.model_copy(update={"lat": 90}),
    ):
        with pytest.raises(ValueError):
            c.renewal_observation_command(gps_position=value)
