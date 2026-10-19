"""组合真实Python核心的合成控件/设备时钟测试，不产生真实App记录。"""

import pytest

from budao.card_observer import NamedCardObserver
from budao.driver import DriveEngine
from budao.mumu import MuMuBridge
from budao.mumu_driver import MuMuPositionWorker
from budao.profiles import CheckpointCatalog, CheckpointEntry
from budao.routes import RoutePlanner, distance, interpolate
from budao.runtime import CapturedRunFrame, RuntimeDispatcher, StartWitness
from budao.simulation import demo_config
from budao.target import RUN_ACTIVITY, TARGET, CheckpointCard, TargetInspection


class FakeBridge(MuMuBridge):
    serial = "127.0.0.1:16416"

    def __init__(self):
        self.locations = []

    def check_device(self):
        pass

    def location_wgs84(self, point, *, authorize, command_timeout_s):
        assert authorize() is True and command_timeout_s == 1
        self.locations.append(point)


def frame(t, names=(), assigned=None, *, distance_m=0, control="running", elapsed=None):
    cards = [
        CheckpointCard(number=i, state="completed", name=name) for i, name in enumerate(names, 1)
    ]
    if len(names) < 3:
        cards.append(
            CheckpointCard(number=len(names) + 1, state="assigned", name=assigned, distance_m=120)
            if assigned
            else CheckpointCard(number=len(names) + 1, state="pending", allocation_remaining_m=100)
        )
    return CapturedRunFrame(
        frame_id=f"frame{t}",
        sampled_at=t,
        inspection=TargetInspection(
            package=TARGET,
            activity=RUN_ACTIVITY,
            version="4.3.0",
            state="metrics",
            counter_label="已打卡",
            gps_label="GPS信号优",
            run_control_state=control,
            completed_checkpoints=len(names),
            distance_m=distance_m,
            elapsed_s=int(t) if elapsed is None else elapsed,
            checkpoint_cards=tuple(cards),
        ),
    )


def witness():
    return StartWitness(
        request_id="a" * 32,
        serial="127.0.0.1:16416",
        action_at=0.5,
        start_ui_verified=True,
        before=frame(0, control="not_started_or_countdown", elapsed=0),
        first_running=frame(1),
        second_running=frame(2),
    )


@pytest.fixture
def setup(monkeypatch):
    clock = [2.0]
    config = demo_config()
    planner = RoutePlanner(config)
    points = [config.corridors[0][i] for i in range(5)] + list(config.corridors[1][:3])
    catalog = CheckpointCatalog(
        site_id="synthetic",
        expected_candidate_count=8,
        entries=tuple(CheckpointEntry(name=f"点{i}", approach=p) for i, p in enumerate(points)),
    )
    observer = NamedCardObserver(catalog, frozenset(f"点{i}" for i in range(8)))
    bridge = FakeBridge()
    worker = MuMuPositionWorker(
        bridge, DriveEngine(planner, config.baseline[0]), clock=lambda: clock[0]
    )
    monkeypatch.setattr(worker, "start", lambda: None)  # 不启动后台设备线程；手动推进模拟时钟。
    dispatcher = RuntimeDispatcher(planner, observer, worker, clock=lambda: clock[0])
    return dispatcher, worker, bridge, clock, config


def update(setup, t, names=(), assigned=None, distance_m=100):
    dispatcher, _worker, _bridge, clock, _config = setup
    clock[0] = t
    return dispatcher.update(frame(t, names, assigned, distance_m=distance_m))


def test_unverified_start_never_arms_or_outputs(setup):
    dispatcher, worker, bridge, _clock, _config = setup
    assert update(setup, 3).action == "pause"
    worker._tick()
    assert not bridge.locations and not worker.engine.enabled


@pytest.mark.parametrize(
    "changes",
    [
        {"start_ui_verified": False},
        {"action_at": 1.5},
        {"first_running": frame(1, elapsed=0)},
        {"second_running": frame(2, elapsed=1)},
        {"second_running": frame(2, ("点0",))},
        {"before": frame(0, control="not_started_or_countdown", elapsed=0, distance_m=10)},
    ],
)
def test_start_witness_rejects_existing_run_countdown_or_missing_causal_evidence(changes):
    data = witness().model_dump()
    data.update({k: v.model_dump() if hasattr(v, "model_dump") else v for k, v in changes.items()})
    with pytest.raises(ValueError):
        StartWitness.model_validate(data)


def test_start_revalidates_copied_witness_and_rejects_wrong_or_stale_device(setup):
    dispatcher, _worker, _bridge, clock, _config = setup
    with pytest.raises(ValueError):
        dispatcher.start(witness().model_copy(update={"start_ui_verified": False}))
    with pytest.raises(ValueError):
        dispatcher.start(witness().model_copy(update={"serial": "127.0.0.1:5555"}))
    clock[0] = 10
    with pytest.raises(ValueError):
        dispatcher.start(witness())
    assert not dispatcher.started


def test_real_timer_start_can_be_proven_with_weak_post_start_gps_but_never_arms_drive(setup):
    data = witness().model_dump()
    for key in ("first_running", "second_running"):
        data[key]["inspection"]["gps_label"] = "GPS信号弱"
    proof = StartWitness.model_validate(data)
    dispatcher, worker, bridge, clock, _config = setup
    dispatcher.start(proof)
    worker._tick()
    assert dispatcher.started and not worker.engine.enabled and not bridge.locations
    clock[0] = 3
    weak = frame(3).model_copy(
        update={"inspection": frame(3).inspection.model_copy(update={"gps_label": "GPS信号弱"})}
    )
    assert dispatcher.update(weak).action == "pause"
    worker._tick()
    assert not worker.engine.enabled and not bridge.locations


@pytest.mark.parametrize("key,label", [("before", "GPS信号弱"), ("first_running", "UNKNOWN")])
def test_timer_proof_still_requires_ready_pre_click_gps_and_known_post_click_state(key, label):
    data = witness().model_dump()
    data[key]["inspection"]["gps_label"] = label
    with pytest.raises(ValueError):
        StartWitness.model_validate(data)


def test_duplicate_start_rejected_without_new_driver_start(setup):
    dispatcher, *_ = setup
    dispatcher.start(witness())
    with pytest.raises(ValueError):
        dispatcher.start(witness())


def test_start_guard_delay_cannot_turn_old_witness_into_new_movement_permission(setup, monkeypatch):
    dispatcher, worker, bridge, clock, _config = setup
    monkeypatch.setattr(worker, "start", lambda: clock.__setitem__(0, 10))
    with pytest.raises(ValueError, match="耗时过长"):
        dispatcher.start(witness())
    assert not dispatcher.started and worker.stop_event.is_set() and not bridge.locations


def test_start_witness_frames_cannot_be_replayed_to_reauthorize_driver(setup):
    dispatcher, worker, bridge, clock, _config = setup
    dispatcher.start(witness())
    clock[0] = 2
    assert dispatcher.update(witness().second_running).action == "pause"
    assert not worker.engine.enabled and not bridge.locations


def test_baseline_to_named_target_updates_existing_worker_not_manual_route_choice(setup):
    dispatcher, worker, bridge, clock, config = setup
    dispatcher.start(witness())
    assert update(setup, 3).action == "base_route"
    worker._tick()
    assert update(setup, 4).action == "base_route"
    worker._tick()
    assert distance(config.baseline[0], worker.check().position) == pytest.approx(2.5, abs=0.01)
    assert update(setup, 5, assigned="点4").action == "wait"
    assert not worker.engine.enabled
    assert update(setup, 6, assigned="点4").action == "navigate"
    assert worker.engine.player.points[0] == worker.check().position
    assert not dispatcher.controller.arrived
    worker._tick()
    assert bridge.locations and dispatcher.controller.completed == 0


def test_pause_and_unknown_target_stop_existing_path(setup):
    dispatcher, worker, bridge, clock, _ = setup
    dispatcher.start(witness())
    update(setup, 3)
    worker._tick()
    assert update(setup, 4, assigned="陌生点").action == "pause"
    clock[0] = 5
    worker._tick()
    assert len(bridge.locations) == 1 and not worker.engine.enabled


def test_slow_gis_plan_cannot_renew_movement_from_expired_observation(setup, monkeypatch):
    dispatcher, worker, bridge, clock, _config = setup
    dispatcher.start(witness())
    assert update(setup, 3, assigned="点4").action == "wait"
    original = dispatcher.planner.plan

    def delayed(*args):
        clock[0] += 3
        return original(*args)

    monkeypatch.setattr(dispatcher.planner, "plan", delayed)
    assert update(setup, 4, assigned="点4").action == "pause"
    assert not worker.engine.enabled and not bridge.locations


def test_return_to_baseline_connects_by_roads_without_teleport(setup):
    dispatcher, worker, _bridge, clock, config = setup
    dispatcher.start(witness())
    # 合成测试设置当前位置；不是实际设备接收证明。
    current = interpolate(config.baseline[0], config.baseline[1], 10)
    worker.engine.position = current
    worker.state = worker._snapshot()
    assert update(setup, 3).action == "base_route"
    path = worker.engine.player.points
    assert distance(path[0], current) < 0.01 and distance(path[-1], config.baseline[0]) < 0.01
    assert not worker.engine.player.repeat


def test_three_random_named_points_and_app_distance_finish_without_submission(setup):
    dispatcher, worker, bridge, clock, _config = setup
    dispatcher.start(witness())
    names = ()
    for i, name in enumerate(("点4", "点1", "点7")):
        t = 3 + i * 10
        assert update(setup, t, names, name, i * 500 + 100).action == "wait"
        assert update(setup, t + 1, names, name, i * 500 + 100).action == "navigate"
        worker._tick()
        names += (name,)
        progress = (i + 1) * 500 + (50 if i == 2 else 0)
        assert update(setup, t + 2, names, distance_m=progress).action == "wait"
        update(setup, t + 3, names, distance_m=progress)
        assert dispatcher.controller.completed == i + 1
    assert update(setup, 30, names, distance_m=1550).action == "finish_ready"
    assert dispatcher.finish_ready and not worker.engine.enabled
    assert dispatcher.controller.submitted_record_id is None and not dispatcher.controller.validated
    commands = len(bridge.locations)
    clock[0] = 31
    worker._tick()
    assert len(bridge.locations) == commands


def test_human_verification_even_after_distance_threshold_does_not_continue(setup):
    dispatcher, worker, *_ = setup
    dispatcher.start(witness())
    dispatcher.finish_ready = True
    prompt = frame(3).model_copy(
        update={"inspection": TargetInspection(issues=("human_face_verification_required",))}
    )
    assert dispatcher.update(prompt).action == "pause"
    assert not worker.engine.enabled


def test_motion_renewal_precedes_position_io_lock(setup, monkeypatch):
    dispatcher, worker, _, clock, _ = setup
    dispatcher.start(witness())
    clock[0] = 3
    events = []
    original = worker.check

    def position_check():
        events.append("position")
        return original()

    monkeypatch.setattr(worker, "check", position_check)
    decision = dispatcher.update(frame(3), renew_motion=lambda: events.append("motion"))
    assert decision.action == "base_route" and events == ["motion", "position"]


def test_failed_motion_renewal_revokes_position_before_output(setup):
    dispatcher, worker, bridge, clock, _ = setup
    dispatcher.start(witness())
    clock[0] = 3

    def expired():
        raise ValueError("Observation expired")

    assert dispatcher.update(frame(3), renew_motion=expired).action == "pause"
    worker._tick()
    assert not worker.engine.enabled and bridge.locations == []


def test_unknown_or_paused_page_never_renews_motion(setup):
    dispatcher, _, _, clock, _ = setup
    dispatcher.start(witness())
    clock[0] = 3
    assert (
        dispatcher.update(
            frame(3, control="paused"), renew_motion=lambda: pytest.fail("Do not renew on pause")
        ).action
        == "pause"
    )


def paced_dispatcher(setup):
    original, worker, _bridge, clock, _config = setup
    dispatcher = RuntimeDispatcher(
        original.planner,
        original.observer,
        worker,
        clock=lambda: clock[0],
        pace_range_s_per_km=(180, 600),
    )
    dispatcher.start(witness())
    # Synthetic state-machine setup only, not actual device/checkpoint proof.
    dispatcher.controller.completed = 3
    dispatcher.controller.completed_names = ("点0", "点1", "点2")
    return dispatcher, worker, clock


def test_slow_formal_pace_continues_roads_and_requires_two_new_eligible_finish_frames(setup):
    dispatcher, worker, clock = paced_dispatcher(setup)
    names = dispatcher.controller.completed_names
    clock[0] = 3
    result = dispatcher.update(frame(3, names, distance_m=1550, elapsed=1000))
    assert result.action == "base_route" and worker.engine.enabled
    assert not dispatcher.finish_ready and not dispatcher.controller.finish_confirmed
    for t, expected in ((4, "wait"), (5, "finish_ready")):
        clock[0] = t
        assert (
            dispatcher.update(frame(t, names, distance_m=2000, elapsed=1100 + t)).action == expected
        )
    assert dispatcher.finish_ready and not worker.engine.enabled
    assert dispatcher.controller.submitted_record_id is None


def test_too_fast_pace_holds_without_moving_or_accepting_ineligible_finish_frame(setup):
    dispatcher, worker, clock = paced_dispatcher(setup)
    names = dispatcher.controller.completed_names
    clock[0] = 3
    assert dispatcher.update(frame(3, names, distance_m=1550, elapsed=200)).action == "wait"
    assert not worker.engine.enabled and dispatcher.controller.finish_candidate is None
    clock[0] = 4
    assert dispatcher.update(frame(4, names, distance_m=1550, elapsed=300)).action == "wait"
    clock[0] = 5
    assert dispatcher.update(frame(5, names, distance_m=1550, elapsed=301)).action == "finish_ready"


@pytest.mark.parametrize(
    "value", [(0, 600), (600, 180), (True, 600), (180, float("nan")), [180, 600]]
)
def test_pace_range_must_be_explicit_well_formed_rule_values(setup, value):
    dispatcher, worker, *_ = setup
    with pytest.raises(ValueError, match="配速"):
        RuntimeDispatcher(
            dispatcher.planner, dispatcher.observer, worker, pace_range_s_per_km=value
        )


def test_opt_in_async_renewal_uses_mailbox_and_replans_after_pause(setup, monkeypatch):
    dispatcher, worker, _bridge, clock, _config = setup
    dispatcher.asynchronous_renewals = True
    dispatcher.start(witness())
    assert update(setup, 3).action == "base_route"
    worker._tick()
    calls = []
    original = worker.renew_async
    monkeypatch.setattr(worker, "renew", lambda _: pytest.fail("Do not wait for GPS IO"))
    monkeypatch.setattr(worker, "renew_async", lambda t: calls.append(t) or original(t))
    assert update(setup, 4).action == "base_route" and calls == [4]
    worker.pause()
    # A cleared mailbox cannot be rearmed with renewal; a new current-position path is needed.
    assert update(setup, 5).action == "base_route"
    assert calls == [4] and worker.engine.player.points[0] == worker.check().position
