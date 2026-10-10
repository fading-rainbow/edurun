from __future__ import annotations

import math

import pytest

from budao.driver import REST, DiagnosticWave, DriveEngine, MotionClip
from budao.routes import RoutePlanner, distance, interpolate
from budao.simulation import demo_config


def engine():
    config = demo_config()
    driver = DriveEngine(RoutePlanner(config), config.baseline[0])
    driver.set_path(config.baseline, 0, repeat=True)
    return driver


def test_watchdog_stops_without_ui_or_model_and_resume_does_not_catch_up():
    driver = engine()
    driver.step(0)
    p = driver.step(1).position
    assert distance(demo_config().baseline[0], p) == pytest.approx(2.5, abs=0.01)
    s = driver.step(15)
    assert not s.moving and s.position == p
    driver.renew(20)
    assert driver.step(20).position == p
    assert driver.step(21).travelled_m == pytest.approx(5)


def test_path_switch_rejects_teleport():
    driver = engine()
    driver.step(0)
    driver.step(1)
    with pytest.raises(ValueError, match="瞬移"):
        driver.set_path(demo_config().baseline, 1)


def test_driver_rejects_diagonal_shortcut_inside_region():
    driver = engine()
    with pytest.raises(ValueError, match="通道"):
        driver.set_path((driver.position, demo_config().baseline[2]), 0)


def test_arrived_is_not_app_checkpoint_confirmation():
    driver = engine()
    target = interpolate(driver.position, demo_config().baseline[1], 1)
    driver.set_path(driver.planner.plan(driver.position, target), 0)
    driver.step(0)
    state = driver.step(1)
    assert state.path_done and not state.moving
    assert "不是打卡成功" in state.reason


def test_backward_clock_halts():
    driver = engine()
    driver.step(0)
    driver.step(1)
    assert not driver.step(0.5).moving


@pytest.mark.parametrize("lease", [0, 6, math.inf, math.nan])
def test_bad_lease(lease):
    with pytest.raises(ValueError):
        engine().renew(0, lease)


def test_diagnostic_wave_is_not_rest():
    assert DiagnosticWave().sample(0.1) != REST


def test_motion_clip_interpolation_and_units(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text(
        "t_s,ax,ay,az,gx,gy,gz\n"
        + "".join(f"{i / 10},{i / 10},0,9.81,0.1,0,0\n" for i in range(11))
    )
    clip = MotionClip(path)
    assert clip.sample(0.15).acceleration[0] == pytest.approx(0.15)
    assert clip.sample(1.15).acceleration[0] == pytest.approx(0.15)
    assert clip.sample(0).gyroscope[0] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "body",
    [
        "t_s,ax,ay,az,gx,gy,gz\n",
        "time,ax,ay,az,gx,gy,gz\n",
        "t_s,ax,ay,az,gx,gy,gz\n0,0,0,9.81,0,0,0\n0,0,0,9.81,0,0,0\n1,0,0,9.81,0,0,0",
        "t_s,ax,ay,az,gx,gy,gz\n0,0,0,9.81,0,0,0\n.5,0,0,9.81,0,0,0\n1,0,0,9.81,0,0,0",
        "t_s,ax,ay,az,gx,gy,gz\n0,nan,0,9.81,0,0,0\n.1,0,0,9.81,0,0,0\n1,0,0,9.81,0,0,0",
    ],
)
def test_bad_motion_clip(tmp_path, body):
    path = tmp_path / "sample.csv"
    path.write_text(body)
    with pytest.raises(ValueError):
        MotionClip(path)
