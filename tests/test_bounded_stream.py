import csv
import math

import pytest
from test_foreground_motion import Device
from test_raw_motion import clip

from budao.driver import MotionClip
from budao.foreground_motion import ForegroundMotionClient
from budao.raw_motion import bounded_clip
from budao.sensor_probe import INJECTION


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
    d = Device(c)
    client = ForegroundMotionClient(d, bounded_stream=True, replay_passes=3)
    client.prepare(c)
    d.mode = INJECTION
    client.start(12_000_000_000)
    client.stop()
    tail = d.calls[-1]
    assert "motion_bounded_stream" in tail and "motion_replay_passes" in tail
    assert tail[tail.index("motion_replay_passes") + 1] == "3"
    assert tail[tail.index("motion_bounded_stream") + 1] == "true"
