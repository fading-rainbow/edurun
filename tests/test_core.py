from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from budao.android import AVD_NAME, AndroidBridge
from budao.calibration import Anchor, Calibration
from budao.controller import RunController
from budao.models import GeoPoint, Observation, PixelPoint, RouteConfig, VisualObservation
from budao.playback import Playback
from budao.routes import RoutePlanner, distance, import_track, path_length
from budao.simulation import demo_config, simulate


@pytest.mark.parametrize("seed", range(30))
def test_dynamic_three_points_and_ads(seed):
    result = simulate(seed)
    assert result["passed"] and result["synthetic_only"]
    assert result["distance_m"] >= 1500
    assert result["completed_checkpoints"] == 3
    assert result["ad_pauses"] > 0 and result["real_records_submitted"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 181, -181])
def test_bad_coordinates(value):
    with pytest.raises(ValidationError):
        GeoPoint(lon=value, lat=0)


def test_route_plans_via_corridors_not_diagonal():
    config = demo_config()
    planner = RoutePlanner(config)
    a, b = config.baseline[0], config.baseline[2]
    result = planner.plan(a, b)
    assert path_length(result) > distance(a, b) * 1.3
    assert all(planner.contains(p) for p in result)


def test_snap_same_edge():
    config = demo_config()
    planner = RoutePlanner(config)
    a = GeoPoint(lon=0.0001, lat=0)
    b = GeoPoint(lon=0.0003, lat=0)
    assert abs(path_length(planner.plan(a, b)) - distance(a, b)) < 0.01


def test_offroad_and_outside_rejected():
    planner = RoutePlanner(demo_config())
    for target in [GeoPoint(lon=1, lat=1), GeoPoint(lon=0.0002, lat=0.0002)]:
        with pytest.raises(ValueError):
            planner.plan(demo_config().baseline[0], target)


def test_disconnected_roads_rejected():
    config = demo_config()
    partial = config.model_copy(
        update={
            "corridors": (config.corridors[0], config.corridors[4]),
            "baseline": config.corridors[0],
        }
    )
    planner = RoutePlanner(partial)
    with pytest.raises(ValueError, match="不连通"):
        planner.plan(partial.corridors[0][0], partial.corridors[1][0])


def test_outside_corridor_rejected():
    data = demo_config().model_dump()
    data["corridors"] = [[{"lon": 1, "lat": 1}, {"lon": 2, "lat": 2}]]
    with pytest.raises(ValueError):
        RoutePlanner(RouteConfig.model_validate(data))


def test_nonwgs84_not_silently_converted():
    data = demo_config().model_dump()
    data["coordinate_system"] = "GCJ02"
    with pytest.raises(ValidationError):
        RouteConfig.model_validate(data)


@pytest.mark.parametrize(
    "suffix,body",
    [
        (".kml", "<kml><LineString><coordinates>0,0,0 0.001,0,0</coordinates></LineString></kml>"),
        (
            ".gpx",
            '<gpx><trk><trkseg><trkpt lon="0" lat="0"/>'
            '<trkpt lon="0.001" lat="0"/></trkseg></trk></gpx>',
        ),
        (".geojson", '{"type":"LineString","coordinates":[[0,0],[0.001,0]]}'),
    ],
)
def test_track_formats(tmp_path, suffix, body):
    path = tmp_path / ("test" + suffix)
    path.write_text(body, "utf-8")
    points = import_track(path)
    assert 110 < path_length(points) < 112


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE x [<!ENTITY x "a">]><kml>&x;</kml>',
        "<kml><coordinates>0,0 1,1</coordinates><coordinates>2,2 3,3</coordinates></kml>",
        "<kml><coordinates>NaN,0 0,0</coordinates></kml>",
    ],
)
def test_unsafe_or_ambiguous_track(tmp_path, body):
    path = tmp_path / "test.kml"
    path.write_text(body, "utf-8")
    with pytest.raises(ValueError):
        import_track(path)


def test_playback_monotonic_and_pause():
    track = demo_config().corridors[0]
    player = Playback(track, 2.5)
    player.advance(0)
    p = player.advance(2)
    assert abs(distance(track[0], p) - 5) < 0.01
    player.pause()
    assert distance(player.advance(100), p) < 0.001
    assert abs(player.travelled - 5) < 0.001


@pytest.mark.parametrize("second", [-1, 4, float("nan")])
def test_no_clock_jump(second):
    player = Playback(demo_config().corridors[0], 2.5)
    player.advance(0)
    with pytest.raises(ValueError):
        player.advance(second)
    assert player.travelled == 0


def test_no_open_route_teleport_loop():
    with pytest.raises(ValueError):
        Playback(demo_config().corridors[0], 2.5, repeat=True)


def test_calibration_and_viewport_change():
    planner = RoutePlanner(demo_config())

    def anchor(x, y):
        return Anchor(PixelPoint(x=x + 20, y=y + 20), planner.geo((x, y)))

    calibration = Calibration(
        planner, [anchor(0, 0), anchor(200, 0), anchor(0, 200)], [anchor(100, 100)], "viewport-1"
    )
    assert calibration.max_error_m < 0.001
    assert (
        distance(calibration.map(PixelPoint(x=120, y=120), "viewport-1"), planner.geo((100, 100)))
        < 0.001
    )
    with pytest.raises(ValueError):
        calibration.map(PixelPoint(x=120, y=120), "viewport-2")


def test_calibration_needs_independent_points():
    planner = RoutePlanner(demo_config())
    a = Anchor(PixelPoint(x=0, y=0), planner.geo((0, 0)))
    b = Anchor(PixelPoint(x=100, y=0), planner.geo((100, 0)))
    c = Anchor(PixelPoint(x=200, y=0), planner.geo((200, 0)))
    with pytest.raises(ValueError):
        Calibration(planner, [a, b, c], [a], "test")


def frame(**updates):
    data = {"frame_id": "1", "sampled_at": 1, "package": "com.lptiyu.tanke"}
    data.update(updates)
    return Observation(**data)


@pytest.mark.parametrize(
    "updates",
    [
        {"package": "some.ad.app"},
        {"screen": "ad"},
        {"screen": "unknown"},
        {"confidence": 0.2},
        {"sampled_at": -10},
        {"sampled_at": 20},
        {"completed_checkpoints": 3},
        {"checkpoint_number": 2, "checkpoint": GeoPoint(lon=0, lat=0)},
    ],
)
def test_controller_pauses_on_bad_state(updates):
    assert RunController().step(frame(**updates), 1).action == "pause"


def test_two_unique_frames_and_arrival_confirmation():
    controller = RunController()
    o = frame(checkpoint_number=1, checkpoint=GeoPoint(lon=0, lat=0))
    assert controller.step(o, 1).action == "wait"
    assert controller.step(o, 1).action == "wait"
    assert controller.step(o.model_copy(update={"frame_id": "2"}), 1).action == "navigate"
    assert controller.completed == 0
    controller.mark_arrived()
    assert controller.step(frame(frame_id="3"), 1).action == "wait"
    controller.step(frame(frame_id="4", completed_checkpoints=1), 1)
    assert controller.completed == 1


def test_cannot_submit_or_validate_old_record():
    controller = RunController()
    with pytest.raises(ValueError):
        controller.mark_submitted("record")
    assert (
        controller.step(frame(screen="summary", record_valid=True, record_id="old"), 1).action
        == "pause"
    )
    controller.completed, controller.distance_m = 3, 1500
    controller.mark_submitted("current")
    assert (
        controller.step(frame(screen="summary", record_valid=True, record_id="old"), 1).action
        == "wait"
    )
    assert (
        controller.step(frame(screen="summary", record_valid=True, record_id="current"), 1).action
        == "validated"
    )


def test_cannot_finish_distance_only():
    controller = RunController()
    assert controller.step(frame(distance_m=1600), 1).action == "base_route"


def test_vision_never_accepts_geo_or_commands():
    for extra in [{"lon": 0, "lat": 0}, {"shell_command": "anything"}]:
        with pytest.raises(ValidationError):
            VisualObservation.model_validate({"screen": "running", "confidence": 1, **extra})


@pytest.mark.parametrize("serial", ["real-phone", "physical-device-serial", "127.0.0.1:5555"])
def test_device_guard_rejects_physical_or_unknown(serial):
    with pytest.raises(ValueError):
        AndroidBridge(serial)


def test_device_guard_rejects_wrong_avd():
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else "Old_AVD\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with pytest.raises(ValueError):
        AndroidBridge("emulator-5580", runner=runner)


def test_device_guard_accepts_only_named_qemu():
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else AVD_NAME + "\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    bridge = AndroidBridge("emulator-5580", runner=runner)
    assert bridge.avd_name == AVD_NAME


def test_config_roundtrip():
    value = RouteConfig.model_validate_json(demo_config().model_dump_json())
    assert json.loads(value.model_dump_json())["coordinate_system"] == "WGS84"
