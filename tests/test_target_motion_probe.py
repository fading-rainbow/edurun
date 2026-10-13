import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_raw_motion import Device as BaseDevice
from test_raw_motion import clip, packet

from budao.android import DIAGNOSTIC_PACKAGE
from budao.foreground_motion import RECEIVER
from budao.target_motion_probe import RX_SERVICE, target_foreground_motion_probe


class Device(BaseDevice):
    def __init__(self, source, *, same_uid=False, stop_fail=False):
        super().__init__(source)
        self.same_uid, self.stop_fail = same_uid, stop_fail
        self.started, self.done = False, False
        self.loops = 0

    def run(self, *args):
        if args[:3] in {
            ("shell", "am", "start-foreground-service"),
            ("shell", "am", "startservice"),
        }:
            self.calls.append(args)
            if RECEIVER + ".RECEIVER_START" in args:
                self.request = args[args.index("receiver_request") + 1]
            if DIAGNOSTIC_PACKAGE + ".MOTION_START" in args:
                self.started = True
            if RECEIVER + ".RECEIVER_STOP" in args:
                self.done = True
            if self.stop_fail and DIAGNOSTIC_PACKAGE + ".MOTION_STOP" in args:
                raise RuntimeError("PRIVATE_ERROR")
            return ""
        if args == ("shell", "run-as", RECEIVER, "cat", "files/receiver-service.json"):
            self.calls.append(args)
            return json.dumps(
                {
                    "package": RECEIVER,
                    "request_id": self.request,
                    "uid": 10045,
                    "registered": True,
                    "event_count": 100,
                    "capture_done": self.done,
                    "elapsed_realtime_ns": 30_000_000_000,
                    "received_samples": packet(self.clip, request=self.request)["raw_motion_probe"][
                        "received_samples"
                    ],
                }
            )
        return super().run(*args)


def capture_factory(device, _template):
    def capture(**_kwargs):
        tx = None
        if device.started:
            device.loops += 1
            tx = {
                **packet(device.clip, request=device.request),
                "uid": 10045 if device.same_uid else 10041,
            }
            if device.loops == 1:
                tx["raw_motion_probe"]["state"] = "running"
        view = SimpleNamespace(run_control_state="paused", distance_m=0, elapsed_s=16, issues=())
        return SimpleNamespace(
            frame=SimpleNamespace(inspection=view, sampled_at=12.0),
            android_observed_ns=12_000_000_000,
            motion_snapshot=tx,
        )

    return SimpleNamespace(capture=capture)


def run(device, source, factory=capture_factory):
    return target_foreground_motion_probe(
        device, source, capture_factory=factory, clock=lambda: 12.0, sleep=lambda _: None
    )


def test_target_stays_foreground_while_independent_uid_receives(tmp_path):
    source = clip(tmp_path)
    device = Device(source)
    result = run(device, source)
    assert result["passed"] and result["clip_received_complete"]
    assert result["renewals"] == 1 and result["target_foreground_preserved"]
    assert not result["target_steps_verified"]
    assert result["records_submitted"] == result["formal_runs_started"] == 0
    assert not any(call[:3] == ("shell", "am", "start") for call in device.calls)
    assert any(RX_SERVICE in call for call in device.calls)


def test_identical_uid_never_proves_cross_receiving(tmp_path):
    source = clip(tmp_path)
    result = run(Device(source, same_uid=True), source)
    assert not result["passed"] and result["mode_restored_normal"]


def test_failed_stop_does_not_skip_mode_cleanup(tmp_path):
    source = clip(tmp_path)
    device = Device(source, stop_fail=True)
    result = run(device, source)
    assert not result["passed"] and result["mode_restored_normal"]
    assert result["cleanup_unconfirmed"] and "PRIVATE_ERROR" not in str(result)


@pytest.mark.parametrize("control", ["running", "unknown", "not_started_or_countdown"])
def test_running_or_ambiguous_countdown_does_not_touch_sensor_mode(tmp_path, control):
    source = clip(tmp_path)
    device = Device(source)

    def factory(*_):
        return SimpleNamespace(
            capture=lambda: SimpleNamespace(
                frame=SimpleNamespace(inspection=SimpleNamespace(run_control_state=control))
            )
        )

    with pytest.raises(ValueError, match="暂停"):
        run(device, source, factory)
    assert device.calls == []


def test_native_receiver_service_is_bounded_read_only_and_does_not_restart():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android/receiver/src/com/edurun/receiver/ReceiverService.java").read_text(
        "utf-8"
    )
    manifest = (root / "android/receiver/AndroidManifest.xml").read_text("utf-8")
    assert "seconds > 15" in source and "START_NOT_STICKY" in source
    assert "startForeground" in source and "events.length() >= 8192" in source
    assert "manager.unregisterListener(this)" in source
    assert "injectSensorData" not in source and "TYPE_STEP_COUNTER" not in source
    assert "INTERNET" not in manifest and "LOCATION" not in manifest
    assert 'android:permission="android.permission.DUMP"' in manifest


def test_cli_target_probe_preserves_existing_evidence_before_any_device_query(
    tmp_path, monkeypatch
):
    import sys

    from budao import cli

    monkeypatch.chdir(tmp_path)
    out = tmp_path / ".local/result.json"
    out.parent.mkdir()
    out.write_text("old-proof")
    monkeypatch.setattr(cli, "MuMuBridge", lambda *_: pytest.fail("Must not query a device"))
    monkeypatch.setattr(sys, "argv", ["budao", "probe-mumu-target-motion", "--output", str(out)])
    assert cli.main() == 2 and out.read_text() == "old-proof"
