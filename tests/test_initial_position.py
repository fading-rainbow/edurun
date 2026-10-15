"""起点设置的合成边界测试；不连接设备，不宣称实收或实跑验收。"""

from types import SimpleNamespace

import pytest

from budao.initial_position import (
    plan_initial_position,
    read_gps_position,
    refresh_initial_position,
)
from budao.live_runtime import RuntimeSettings
from budao.models import GeoPoint
from budao.routes import RoutePlanner
from budao.simulation import demo_config
from budao.target import CheckpointCard, TargetInspection


def test_read_only_gps_parser_uses_only_unique_gps_provider():
    tree = (
        "    network provider:\n"
        "      last location=Location[gps 45,75 hAcc=1]\n"
        "    gps provider:\n"
        "      last location=Location[gps -30.5,-120.25 hAcc=1]\n"
        "    passive provider:\n"
        "      last location=Location[gps 50,80 hAcc=1]\n"
    )
    assert read_gps_position(tree) == GeoPoint(lat=-30.5, lon=-120.25)
    for invalid in (
        "",
        tree.replace("gps provider:", "other provider:"),
        tree + "    gps provider:\n",
        tree.replace("Location[gps -30.5,-120.25", "Location[network -30.5,-120.25"),
        tree.replace("-30.5,-120.25", "-90,180"),
        tree.replace("-30.5,-120.25", "nan,inf"),
    ):
        with pytest.raises(ValueError):
            read_gps_position(invalid)


def test_current_reachable_position_is_preserved_not_moved_to_baseline():
    planner = RoutePlanner(demo_config())
    observed = planner.config.corridors[-1][1]
    for enabled in (False, True):
        result = plan_initial_position(planner, observed, initialize_fresh=enabled)
        assert result.observed == result.planned == observed
        assert result.initialization_required is False


@pytest.mark.parametrize("observed", [GeoPoint(lon=1, lat=1), GeoPoint(lon=0.0002, lat=0.0002)])
def test_outside_region_or_off_road_requires_explicit_fresh_setup(observed):
    planner = RoutePlanner(demo_config())
    with pytest.raises(ValueError, match="未授权"):
        plan_initial_position(planner, observed)
    result = plan_initial_position(planner, observed, initialize_fresh=True)
    assert result.observed == observed
    assert result.planned == planner.config.baseline[0]
    assert result.initialization_required is True
    assert not hasattr(result, "received_position_proven")


def test_existing_session_can_never_request_fresh_position():
    base = {
        "mode": "known_points_trial",
        "motion_csv": ".local/m.csv",
        "independent_motion_evidence": ".local/proof.json",
        "start_template": ".local/start.png",
    }
    assert RuntimeSettings.model_validate(base).initialize_fresh_position is False
    assert RuntimeSettings.model_validate(base | {"initialize_fresh_position": True})
    for extra in (
        {"initialize_fresh_position": True, "resume_evidence": ".local/old"},
        {"initialize_fresh_position": "true"},
        {"initialize_fresh_position": 1},
    ):
        with pytest.raises(ValueError):
            RuntimeSettings.model_validate(base | extra)


def zero():
    return TargetInspection(
        state="metrics",
        run_control_state="not_started_or_countdown",
        distance_m=0,
        elapsed_s=0,
        completed_checkpoints=0,
        counter_label="已打卡",
        checkpoint_cards=(CheckpointCard(number=1, state="pending", allocation_remaining_m=100),),
        issues=("start_controls_and_current_record_identity_unverified",),
    )


def connector(view):
    writes = []

    def capture(*, pixels):
        assert pixels is True
        return SimpleNamespace(
            frame=SimpleNamespace(inspection=view, sampled_at=10),
            screenshot=object(),
            hierarchy="<hierarchy/>",
        )

    return SimpleNamespace(
        capture=capture,
        template=SimpleNamespace(matches=lambda *_: True),
        clock=lambda: 10.1,
        bridge=SimpleNamespace(location_wgs84=lambda *a, **kw: writes.append((a, kw))),
    ), writes


@pytest.mark.parametrize(
    "changed",
    [
        {"state": "unavailable"},
        {"run_control_state": "unknown"},
        {"run_control_state": "running"},
        {"run_control_state": "paused"},
        {"distance_m": 10},
        {"elapsed_s": 1},
        {"completed_checkpoints": 1},
        {"counter_label": "热量"},
        {"issues": ("human_face_verification_required",)},
        {"checkpoint_cards": ()},
    ],
)
def test_old_running_paused_free_or_protected_page_never_relocated(changed):
    c, writes = connector(zero().model_copy(update=changed))
    with pytest.raises(ValueError, match="正式零状态"):
        refresh_initial_position(c, GeoPoint(lat=0, lon=0), lambda: True)
    assert not writes


def test_location_write_requires_fresh_zero_and_current_authorization():
    c, writes = connector(zero())
    initial = GeoPoint(lat=0, lon=0)
    for denied in (False, None, 1):
        with pytest.raises(ValueError, match="未获许可"):
            refresh_initial_position(c, initial, lambda denied=denied: denied)
    assert not writes

    def permission():
        return True

    refresh_initial_position(c, initial, permission)
    assert len(writes) == 1 and writes[0][0] == (initial,)
    assert writes[0][1]["command_timeout_s"] == 1
    assert writes[0][1]["authorize"]() is True
    c.clock = lambda: 12
    assert writes[0][1]["authorize"]() is False


def test_countdown_pixel_or_expired_observation_never_sets_position():
    c, writes = connector(zero())
    c.template.matches = lambda *_: False  # Countdown also has zero seconds.
    with pytest.raises(ValueError, match="正式零状态"):
        refresh_initial_position(c, GeoPoint(lat=0, lon=0), lambda: True)
    c.template.matches = lambda *_: True
    for expired in (9.99, 12, 20):
        c.clock = lambda expired=expired: expired
        with pytest.raises(ValueError, match="过期"):
            refresh_initial_position(c, GeoPoint(lat=0, lon=0), lambda: True)
    assert not writes
