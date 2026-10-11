import json
import math
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from budao.driver import MotionClip, MotionWorker
from budao.motion_data import align_clip, prepare_realworld, read_sensor


def sensor(times):
    t = np.asarray(times, dtype=float)
    return np.column_stack((t, np.sin(t), np.cos(t), np.full_like(t, 9.81)))


def archive(tmp_path, body):
    p = tmp_path / "source.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("data.csv", body)
    return p


def test_sensor_schema_millisecond_time_and_raw_units(tmp_path):
    p = archive(
        tmp_path,
        "id,attr_time,attr_x,attr_y,attr_z\n1,1000,1,2,9.81\n2,1020,2,3,9.81\n3,1040,3,4,9.81\n",
    )
    data = read_sensor(p, "data.csv")
    assert data[:, 0].tolist() == [1, 1.02, 1.04]
    assert data[0, 1:].tolist() == [1, 2, 9.81]


@pytest.mark.parametrize(
    "body",
    [
        "id,time,x,y,z\n1,1000,1,2,3",
        "id,attr_time,attr_x,attr_y,attr_z\n",
        "id,attr_time,attr_x,attr_y,attr_z\n1,1000,nan,0,0\n2,1020,0,0,0\n3,1040,0,0,0",
        "id,attr_time,attr_x,attr_y,attr_z\n1,1000,0,0,0\n2,1000,0,0,0\n3,1040,0,0,0",
        "id,attr_time,attr_x,attr_y,attr_z\n1,1000,101,0,0\n2,1020,0,0,0\n3,1040,0,0,0",
    ],
)
def test_invalid_source_not_coerced_or_clipped(tmp_path, body):
    with pytest.raises(ValueError):
        read_sensor(archive(tmp_path, body), "data.csv")


def test_common_clock_alignment_does_not_extrapolate():
    a = sensor(np.arange(0, 160, 0.02))
    g = sensor(np.arange(0.007, 160, 0.02))
    rows, quality = align_clip(a, g, 60)
    assert rows.shape == (3001, 7)
    assert rows[0, 0] == 0 and rows[-1, 0] == 60
    assert np.diff(rows[:, 0]) == pytest.approx(np.full(3000, 0.02))
    assert quality["output_hz"] == 50
    assert quality["source_start_epoch_ms"] == 60007
    assert rows[:, 3] == pytest.approx(np.full(3001, 9.81))


def test_sensor_holes_are_not_filled():
    a = sensor(np.r_[np.arange(0, 70, 0.02), np.arange(90, 180, 0.02)])
    g = sensor(np.arange(0, 180, 0.02))
    _, quality = align_clip(a, g, 60)
    assert quality["source_start_epoch_ms"] == 90000


def test_no_long_common_interval_is_refused():
    a = sensor(np.r_[np.arange(0, 70, 0.02), np.arange(90, 140, 0.02)])
    with pytest.raises(ValueError, match="连续"):
        align_clip(a, sensor(np.arange(0, 140, 0.02)), 60)


@pytest.mark.parametrize("seconds", [0, 301, True, 60.5, math.nan])
def test_bad_duration_before_download(tmp_path, monkeypatch, seconds):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("budao.motion_data.download_sources", lambda _d: pytest.fail("download"))
    with pytest.raises(ValueError):
        prepare_realworld(tmp_path / ".local/motion", seconds)


def test_output_outside_local_refused_before_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("budao.motion_data.download_sources", lambda _d: pytest.fail("download"))
    with pytest.raises(ValueError, match=".local"):
        prepare_realworld(tmp_path / "public")


def test_prepared_format_idempotence_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".local/motion"
    monkeypatch.setattr("budao.motion_data.download_sources", lambda d: d.mkdir(parents=True))
    data = sensor(np.arange(0, 160, 0.02))
    monkeypatch.setattr("budao.motion_data.read_sensor", lambda *_: data)
    result = prepare_realworld(directory)
    clip = MotionClip(Path(result["csv"]))
    assert clip.duration == 60
    manifest = json.loads(Path(result["provenance"]).read_text())
    assert not manifest["device_replay_verified"]
    assert not manifest["target_app_acceptance_verified"]
    assert not manifest["effective_record_verified"]
    monkeypatch.setattr("budao.motion_data.download_sources", lambda _d: None)
    assert prepare_realworld(directory) == result
    Path(result["csv"]).write_text("preserve this", encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖"):
        prepare_realworld(directory)
    assert Path(result["csv"]).read_text() == "preserve this"


@pytest.mark.parametrize("rate", [25, 50])
def test_worker_period_is_explicit_and_default_remains_25(rate):
    bridge = SimpleNamespace(check_device=lambda: None)
    engine = SimpleNamespace(step=lambda _t: None)
    worker = MotionWorker(bridge, engine, motion_hz=rate)
    assert worker.period_s == 1 / rate
    assert MotionWorker(bridge, engine).motion_hz == 25


@pytest.mark.parametrize("rate", [0, 24, 51, math.inf, math.nan])
def test_bad_worker_rate_does_not_touch_device(rate):
    bridge = SimpleNamespace(check_device=lambda: pytest.fail("device access"))
    with pytest.raises(ValueError):
        MotionWorker(bridge, None, motion_hz=rate)
