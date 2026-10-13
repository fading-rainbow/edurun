from copy import deepcopy
from pathlib import Path

import pytest

from budao.gps_reception import received_gps_matches
from budao.models import GeoPoint

POSITION = GeoPoint(lon=120, lat=30)
REQUEST = "e" * 32
NOW = 4_000_000_000


def packet():
    return {
        "package": "com.edurun.diagnostic",
        "request_id": REQUEST,
        "elapsed_realtime_ns": 3_800_000_000,
        "gps_reception": {
            "registered": True,
            "provider": "gps",
            "task_started_ns": 1_000_000_000,
            "callback_count": 3,
            "first_received_ns": 1_500_000_000,
            "last_received_ns": 3_700_000_000,
            "latitude": POSITION.lat,
            "longitude": POSITION.lon,
            "has_accuracy": True,
            "accuracy_m": 2.5,
            "is_mock": False,
            # Emulator Location clock is retained as reported, not used or rewritten.
            "location_elapsed_ns": 1_900_000_000_000_000_000,
        },
    }


def test_actual_callback_reception_is_not_an_inference_from_location_clock_or_mock_flag():
    p = packet()
    before = deepcopy(p)
    assert received_gps_matches(p, REQUEST, NOW, POSITION)
    assert p == before
    p["gps_reception"]["is_mock"] = True
    assert received_gps_matches(p, REQUEST, NOW, POSITION)


@pytest.mark.parametrize(
    "change",
    [
        {"registered": False},
        {"provider": "network"},
        {"has_accuracy": False},
        {"callback_count": 1},
        {"callback_count": True},
        {"task_started_ns": 2_000_000_000},
        {"first_received_ns": 3_700_000_000},
        {"last_received_ns": 1_600_000_000},
        {"last_received_ns": 4_100_000_000},
        {"accuracy_m": 5.01},
        {"accuracy_m": -1},
        {"accuracy_m": float("nan")},
        {"latitude": True},
        {"latitude": float("inf")},
        {"longitude": 121},
        {"is_mock": 0},
    ],
)
def test_unknown_stale_inaccurate_or_mismatched_reception_does_not_pass(change):
    p = packet()
    p["gps_reception"].update(change)
    assert not received_gps_matches(p, REQUEST, NOW, POSITION)


def test_request_package_time_and_missing_fields_fail_closed():
    for value in (None, {}, [], {"package": "foreign"}):
        assert not received_gps_matches(value, REQUEST, NOW, POSITION)
    for key, value in (
        ("request_id", "f" * 32),
        ("package", "foreign"),
        ("elapsed_realtime_ns", 3_600_000_000),
        ("elapsed_realtime_ns", 4_100_000_000),
    ):
        p = packet()
        p[key] = value
        assert not received_gps_matches(p, REQUEST, NOW, POSITION)
    assert not received_gps_matches(packet(), REQUEST, True, POSITION)
    assert not received_gps_matches(packet(), REQUEST, NOW, {})


def test_only_documented_ten_ms_uptime_resolution_is_accepted_without_changing_evidence():
    p = packet()
    p["elapsed_realtime_ns"] = NOW + 9_999_999
    assert received_gps_matches(p, REQUEST, NOW, POSITION)
    p["elapsed_realtime_ns"] = NOW + 10_000_000
    assert not received_gps_matches(p, REQUEST, NOW, POSITION)


def test_native_observer_is_read_only_and_same_nonce_gps_guard_is_additive():
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionService.java"
    ).read_text("utf-8")
    assert "requestLocationUpdates" in source and "removeUpdates" in source
    assert "SystemClock.elapsedRealtimeNanos()" in source
    assert "gpsCallbacks < 2" in source and "now - lastGpsReceivedNs > 2_000_000_000L" in source
    assert "requestId.equals(request)" in source and "gpsFix.distanceTo(expected) <= 10" in source
    assert "setTestProviderLocation" not in source and "setMock" not in source
    assert 'intent.getBooleanExtra("motion_require_received_gps", false)' in source
