import csv
import json
import math
from pathlib import Path

import pytest
from test_foreground_motion import Device
from test_raw_motion import clip

from budao.driver import MotionClip
from budao.foreground_motion import ForegroundMotionClient
from budao.raw_motion import bounded_clip
from budao.sensor_probe import INJECTION


class PreparedDevice(Device):
    def run(self, *args):
        if args == ("shell", "run-as", "com.edurun.diagnostic", "cat", "files/motion-service.json"):
            return json.dumps(self.preload_state)
        return super().run(*args)


def prepared_device(c, client_request, passes=3):
    d = PreparedDevice(c)
    d.preload_state = {
        "package": "com.edurun.diagnostic",
        "request_id": client_request,
        "raw_motion_probe": {
            "request_id": client_request,
            "state": "prepared",
            "clip_sha256": c.sha256,
            "source_rows": len(c.rows),
            "bounded_stream": True,
            "replay_passes": passes,
            "sent_rows": 0,
            "lease_required": False,
        },
    }
    return d


def long_csv(tmp_path, seconds):
    path = tmp_path / "long.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
        writer.writerows(
            [i / 50, math.sin(i), 9.8, 0.2, 0.02, 0.01, 0.03] for i in range(seconds * 50 + 1)
        )
    return MotionClip(path)


def test_bounded_clip_limits_depend_on_stream_flag(tmp_path):
    source = long_csv(tmp_path, 30)
    with pytest.raises(ValueError):
        bounded_clip(source, 30, "a" * 64)
    assert bounded_clip(source, 30, "a" * 64, bounded_stream=True).duration_s == 30
    with pytest.raises(ValueError):
        bounded_clip(source, 151, "a" * 64, bounded_stream=True)
    with pytest.raises(ValueError):
        bounded_clip(source, 30, "a" * 64, bounded_stream=1)


@pytest.mark.parametrize(
    "stream,passes", [(False, 2), (True, 0), (True, 7), (1, 1), (True, True), (True, 1.0)]
)
def test_client_rejects_invalid_replay_options(tmp_path, stream, passes):
    with pytest.raises(ValueError, match="6遍"):
        ForegroundMotionClient(Device(clip(tmp_path)), bounded_stream=stream, replay_passes=passes)


def test_client_passes_stream_options_to_service(tmp_path):
    c = clip(tmp_path)
    d = prepared_device(c, "a" * 32)
    client = ForegroundMotionClient(d, "a" * 32, bounded_stream=True, replay_passes=3)
    client.prepare(c)
    client.preload(c)
    d.mode = INJECTION
    client.start(12_000_000_000)
    client.stop()
    tail = d.calls[-1]
    assert "motion_bounded_stream" in tail and "motion_replay_passes" in tail
    assert tail[tail.index("motion_replay_passes") + 1] == "3"
    assert tail[tail.index("motion_bounded_stream") + 1] == "true"


def test_cold_long_stream_requires_verified_preload_before_lease(tmp_path):
    c = clip(tmp_path)
    d = Device(c)
    client = ForegroundMotionClient(d, bounded_stream=True)
    client.prepare(c)
    with pytest.raises(ValueError, match="预加载"):
        client.start(12_000_000_000)
    assert not any("com.edurun.diagnostic.MOTION_START" in call for call in d.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("clip_sha256", "bad"),
        ("sent_rows", 1),
        ("lease_required", True),
        ("replay_passes", 6),
        ("state", "lease_expired"),
    ],
)
def test_preload_must_match_nonce_source_and_zero_unleased_state(tmp_path, field, value):
    c = clip(tmp_path)
    d = prepared_device(c, "a" * 32)
    d.preload_state["raw_motion_probe"][field] = value
    client = ForegroundMotionClient(d, "a" * 32, bounded_stream=True, replay_passes=3)
    client.prepare(c)
    with pytest.raises(ValueError):
        client.preload(c)
    assert not client.native_prepared and not client.started


def test_old_preload_cannot_pass_or_retry_start(tmp_path):
    c = clip(tmp_path)
    d = prepared_device(c, "b" * 32)
    client = ForegroundMotionClient(d, "a" * 32, bounded_stream=True, replay_passes=3)
    client.prepare(c)
    with pytest.raises(ValueError, match="本次原生"):
        client.preload(c, clock=iter(range(15)).__next__, sleep=lambda _: None)
    assert not client.native_prepared
    with pytest.raises(ValueError, match="一次"):
        client.preload(c)
    client.stop()
    assert "com.edurun.diagnostic.MOTION_STOP" in d.calls[-1]


def test_preloaded_native_source_cannot_be_replaced(tmp_path):
    c = clip(tmp_path)
    d = prepared_device(c, "a" * 32)
    client = ForegroundMotionClient(d, "a" * 32, bounded_stream=True, replay_passes=3)
    client.prepare(c)
    client.preload(c)
    with pytest.raises(ValueError, match="替换"):
        client.prepare(c)


def test_native_preparation_does_not_open_injection_or_extend_motion_lease():
    root = Path(__file__).resolve().parents[1]
    probe = (root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionProbe.java").read_text(
        "utf-8"
    )
    service = (
        root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionService.java"
    ).read_text("utf-8")
    prepare = probe.split("void prepare()", 1)[1].split("boolean isPrepared()", 1)[0]
    assert (
        "load()" in prepare and "init.invoke" not in prepare and "registerListener" not in prepare
    )
    assert "preparedUntilNs" in service and "30_000_000_000L" in service
    assert "probe.requireLease(observed, ttl); probe.start();" in service
    assert "now-observedNs > 1_000_000_000L" in probe and "ttlMs > 2000" in probe
