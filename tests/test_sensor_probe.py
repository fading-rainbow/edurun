import copy
from pathlib import Path

import pytest

from budao.android import DIAGNOSTIC_PACKAGE
from budao.mumu import MuMuBridge
from budao.sensor_probe import INJECTION, match_probe_samples, probe_mumu_sensors, sensor_mode


def packet():
    sent = []
    for index in range(25):
        for kind in (1, 4):
            sent.append(
                {
                    "type": kind,
                    "timestamp_ns": 1_000_000_000 + index * 40_000_000 + kind,
                    "values": [0.1, 9.8 + (index % 2) * 0.5, 0.2] if kind == 1 else [0, 0, 0.02],
                }
            )
    return {
        "elapsed_realtime_ns": 3_000_000_000,
        "injection_probe": {
            "synthetic_diagnostic_only": True,
            "initialized": True,
            "client_closed": True,
            "state": "completed",
            "sent_samples": sent,
            "received_samples": copy.deepcopy(sent),
        },
    }


def test_samples_need_real_timestamp_and_value_match_not_ack():
    snap = packet()
    assert match_probe_samples(snap)["raw_receiver_verified"]
    for sample in snap["injection_probe"]["received_samples"]:
        sample["timestamp_ns"] += 1
    assert not match_probe_samples(snap)["raw_receiver_verified"]


def test_actual_accelerometer_quantization_tolerance_is_bounded():
    snap = packet()
    for sample in snap["injection_probe"]["received_samples"]:
        sample["values"][0] += 0.00001241
    assert match_probe_samples(snap)["raw_receiver_verified"]
    for sample in snap["injection_probe"]["received_samples"]:
        sample["values"][0] += 0.001
    assert not match_probe_samples(snap)["raw_receiver_verified"]


def test_duplicate_events_do_not_increase_unique_sent_matches():
    snap = packet()
    snap["injection_probe"]["received_samples"] = [
        snap["injection_probe"]["received_samples"][0]
    ] * 100
    result = match_probe_samples(snap)
    assert result["matched_by_type"] == {"1": 1, "4": 0}
    assert not result["raw_receiver_verified"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "secret", None])
def test_bad_vectors_cannot_pass(value):
    snap = packet()
    snap["injection_probe"]["sent_samples"][0]["values"][0] = value
    assert not match_probe_samples(snap)["raw_receiver_verified"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("initialized", False),
        ("client_closed", False),
        ("state", "send_rejected"),
        ("synthetic_diagnostic_only", False),
    ],
)
def test_incomplete_probe_is_not_verified(key, value):
    snap = packet()
    snap["injection_probe"][key] = value
    assert not match_probe_samples(snap)["raw_receiver_verified"]


def test_only_top_level_mode_counts_for_restore():
    assert sensor_mode("Mode : DATA_INJECTION : \n\tOperating Mode: NORMAL\n") != "NORMAL"
    assert sensor_mode("\tOperating Mode: NORMAL\n") is None
    assert sensor_mode("Mode : NORMAL\nMode : NORMAL\n") is None
    assert sensor_mode("Mode : NORMAL\n\tOperating Mode: DATA_INJECTION\n") == "NORMAL"


class Device(MuMuBridge):
    def __init__(self, *, fail=False, target=False, restore=True, supported=True):
        self.calls = []
        self.mode = "NORMAL"
        self.snapshots = 0
        self.fail, self.target, self.restore, self.supported = fail, target, restore, supported

    def check_device(self):
        self.calls.append(("guard",))

    def run(self, *args):
        self.calls.append(args)
        if args == ("shell", "ps", "-A"):
            return "com.lptiyu.tanke:push" if self.target else "com.edurun.diagnostic"
        if args[:3] == ("shell", "dumpsys", "sensorservice"):
            if len(args) == 3:
                return "Mode : " + self.mode + "\n\tOperating Mode: NORMAL\n"
            if args[3] == "data_injection":
                self.mode = INJECTION
            elif self.restore:
                self.mode = "NORMAL"
        if args[:3] == ("shell", "am", "start") and self.fail:
            raise RuntimeError("Private test argument")
        if args == ("shell", "dumpsys", "window", "displays"):
            return "mCurrentFocus=Window{123 u0 com.edurun.diagnostic/.DiagnosticActivity}"
        return ""

    def diagnostic_snapshot(self):
        self.snapshots += 1
        if self.snapshots == 1:
            return {
                "injection_api": {
                    "initDataInjection": "available" if self.supported else "not_accessible",
                    "injectSensorData": "available",
                }
            }
        if self.mode == INJECTION:
            return packet()
        return {
            "sensors": [
                {
                    "type": kind,
                    "event_count": self.snapshots,
                    "last_timestamp_ns": self.snapshots,
                }
                for kind in (1, 4)
            ]
        }

    def diagnostic_start(self):
        self.calls.append(("diagnostic_start",))


def test_success_is_raw_only_not_steps_natural_gait_or_run():
    device = Device()
    report = probe_mumu_sensors(device, sleep=lambda _: None)
    assert report["passed"] and report["normal_stream_resumed"]
    assert not report["target_steps_verified"] and not report["natural_gait_verified"]
    assert report["records_submitted"] == report["formal_runs_started"] == 0
    enable = device.calls.index(("shell", "dumpsys", "sensorservice", "enable"))
    assert device.calls[enable - 1] == ("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    assert not any("location" in call or "com.lptiyu.tanke" in call for call in device.calls)


def test_failure_still_closes_own_producer_before_enable():
    device = Device(fail=True)
    report = probe_mumu_sensors(device, sleep=lambda _: None)
    assert not report["passed"] and report["mode_restored_normal"]
    assert report["state"] == "probe_failed" and "Private test" not in str(report)
    assert device.mode == "NORMAL"


def test_client_normal_does_not_mask_failed_hal_restore():
    report = probe_mumu_sensors(Device(restore=False), sleep=lambda _: None)
    assert report["raw_receiver_verified"]
    assert not report["mode_restored_normal"] and not report["passed"]


def test_target_running_and_hidden_api_denied_stop_before_mode_write():
    device = Device(target=True)
    with pytest.raises(ValueError, match="进程已停止"):
        probe_mumu_sensors(device)
    device = Device(supported=False)
    report = probe_mumu_sensors(device)
    assert report["state"] == "api_not_accessible" and not report["passed"]
    assert not any("data_injection" in call or "force-stop" in call for call in device.calls)
    with pytest.raises(ValueError, match="MuMu适配器"):
        probe_mumu_sensors(object())


def test_native_probe_is_opt_in_bounded_and_does_not_change_hidden_api_policy():
    source = (
        Path(__file__).resolve().parents[1]
        / "android/diagnostic/src/com/edurun/diagnostic/DiagnosticActivity.java"
    ).read_text("utf-8")
    assert 'getBooleanExtra("probe_injection", false)' in source
    assert "injectionTick >= 25" in source and "handler.postDelayed(this, 40)" in source
    assert "receivedProbeSamples.length() < 1024" in source
    assert "setAccessible" not in source and "hidden_api_policy" not in source
    assert 'finishInjectionProbe("activity_destroyed")' in source
