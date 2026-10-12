"""合成控件/时钟测试；不连接设备、不产生实际App记录。"""

from types import SimpleNamespace

import pytest

from budao.card_observer import NamedCardObserver
from budao.controller import RunController
from budao.models import Observation
from budao.profiles import CheckpointCatalog, CheckpointEntry
from budao.simulation import demo_config
from budao.target import (
    RUN_ACTIVITY,
    TARGET,
    CheckpointCard,
    TargetInspection,
    inspect_target,
    parse_run_control_state,
)


def xml_control(elapsed="00:00:30", *, paused=False):
    def node(name, text="", cls="android.widget.ProgressBar"):
        return (
            f'<node package="{TARGET}" resource-id="{TARGET}:id/{name}" '
            f'text="{text}" class="{cls}" enabled="true" visible-to-user="true"/>'
        )

    return (
        "<hierarchy>"
        + node("chronometer", elapsed, "android.widget.TextView")
        + node("tv_start_pause_continue_run")
        + (node("tv_stoprun") if paused else "")
        + "</hierarchy>"
    )


def test_collected_control_combinations_are_not_session_proof():
    assert parse_run_control_state(xml_control("00:00:00")) == "not_started_or_countdown"
    assert parse_run_control_state(xml_control()) == "running"
    assert parse_run_control_state(xml_control(paused=True)) == "paused"


@pytest.mark.parametrize(
    "xml",
    [
        xml_control().replace('enabled="true"', 'enabled="false"'),
        xml_control().replace("android.widget.ProgressBar", "android.widget.Button"),
        xml_control().replace('visible-to-user="true"', 'visible-to-user="false"'),
        xml_control().replace("tv_start_pause_continue_run", "wrong_id"),
        xml_control("bad-time"),
        xml_control().replace("</hierarchy>", xml_control() + "</hierarchy>"),
        xml_control().replace(
            "</hierarchy>",
            f'<node package="{TARGET}" resource-id="{TARGET}:id/ad_close" '
            'visible-to-user="true"/></hierarchy>',
        ),
        xml_control().replace(
            "</hierarchy>",
            '<node package="com.foreign.overlay" visible-to-user="true"/></hierarchy>',
        ),
        "<!DOCTYPE hierarchy><hierarchy/>",
    ],
)
def test_ambiguous_hidden_disabled_or_overlay_controls_unknown(xml):
    assert parse_run_control_state(xml) == "unknown"


def test_inspection_exposes_control_state_without_authorizing_run():
    class Bridge:
        def check_device(self):
            pass

        def run(self, *args):
            if "package" in args:
                return "versionName=4.3.0\nversionCode=230"
            return f"mCurrentFocus=Window{{x u0 {TARGET}/{RUN_ACTIVITY}}}"

    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=xml_control))
    result = inspect_target(Bridge(), lambda _b: ui)
    assert result.run_control_state == "running"
    assert not result.run_started_proven and not result.record_valid_proven


@pytest.fixture
def observer():
    config = demo_config()
    points = [config.corridors[0][i] for i in range(5)] + list(config.corridors[1][:3])
    catalog = CheckpointCatalog(
        site_id="synthetic",
        expected_candidate_count=8,
        entries=tuple(CheckpointEntry(name=f"点{i}", approach=p) for i, p in enumerate(points)),
    )
    return NamedCardObserver(catalog, frozenset(f"点{i}" for i in range(8)))


def inspection(names=(), assigned=None, distance=100, elapsed=30):
    cards = [
        CheckpointCard(number=i, state="completed", name=name) for i, name in enumerate(names, 1)
    ]
    if len(names) < 3:
        cards.append(
            CheckpointCard(number=len(names) + 1, state="assigned", name=assigned, distance_m=120)
            if assigned
            else CheckpointCard(number=len(names) + 1, state="pending", allocation_remaining_m=100)
        )
    return TargetInspection(
        package=TARGET,
        activity=RUN_ACTIVITY,
        version="4.3.0",
        state="metrics",
        run_control_state="running",
        counter_label="已打卡",
        gps_label="优",
        distance_m=distance,
        elapsed_s=elapsed,
        completed_checkpoints=len(names),
        checkpoint_cards=tuple(cards),
    )


def test_local_observer_maps_exact_names_without_model_or_pixels(observer):
    frame = observer.observe(inspection(assigned="点2"), 10, "first")
    assert frame.screen == "running" and frame.checkpoint_name == "点2"
    assert frame.checkpoint == observer.catalog.resolve("点2").approach
    assert not frame.record_valid and frame.record_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"package": "com.tencent.mobileqq"},
        {"activity": TARGET + ".CameraActivity"},
        {"version": "4.4.0"},
        {"state": "unavailable"},
        {"run_control_state": "paused"},
        {"run_control_state": "not_started_or_countdown"},
        {"gps_label": "搜索中"},
        {"counter_label": "卡路里"},
        {"elapsed_s": 0},
        {"checkpoint_cards": ()},
        {"issues": ("human_face_verification_required",)},
        {"distance_m": None},
    ],
)
def test_wrong_page_pause_verification_missing_data_do_not_yield_running(observer, changes):
    frame = observer.observe(inspection(assigned="点2").model_copy(update=changes), 10, "one")
    assert frame.screen == "unknown" and frame.checkpoint is None


def test_unknown_or_unconfirmed_names_do_not_reuse_previous_target(observer):
    assert observer.observe(inspection(assigned="点2相似"), 10, "one").screen == "unknown"
    partial = NamedCardObserver(observer.catalog, frozenset({"点1"}))
    assert partial.observe(inspection(assigned="点2"), 10, "one").screen == "unknown"


def test_capture_timestamp_is_taken_before_slow_collection(observer):
    events = []

    def clock():
        events.append("clock")
        return 10

    def inspector(_bridge):
        events.append("inspect")
        return inspection(assigned="点2")

    frame = observer.capture(object(), inspector=inspector, clock=clock)
    assert events == ["clock", "inspect"] and frame.sampled_at == 10
    controller = RunController(named_card_mode=True)
    assert controller.step(frame, 16).action == "pause"


def send(controller, observer, tick, names=(), assigned=None, distance=100, **changes):
    frame = observer.observe(
        inspection(names, assigned, distance, elapsed=tick), float(tick), f"f{tick}"
    )
    if changes:
        frame = frame.model_copy(update=changes)
    return controller.step(frame, float(tick))


@pytest.mark.parametrize("order", [(0, 3, 7), (5, 1, 4), (7, 6, 2), (2, 0, 5), (6, 4, 1)])
def test_named_cards_complete_sequentially_without_geometric_arrival(observer, order):
    controller = RunController(named_card_mode=True, minimum_distance_m=1550)
    names = ()
    for i, index in enumerate(order):
        tick = i * 10 + 1
        name = f"点{index}"
        assigned_distance = i * 500 + 100
        confirmed_distance = (i + 1) * 500 + (50 if i == 2 else 0)
        assert send(controller, observer, tick, names, name, assigned_distance).action == "wait"
        assert (
            send(controller, observer, tick + 1, names, name, assigned_distance).action
            == "navigate"
        )
        # App已确认可早于到达目录接近位置；未调用mark_arrived，不伪造位置到达。
        assert not controller.arrived
        names += (name,)
        assert (
            send(controller, observer, tick + 2, names, distance=confirmed_distance).action
            == "wait"
        )
        assert controller.completed == i
        send(controller, observer, tick + 3, names, distance=confirmed_distance)
        assert controller.completed == i + 1
    assert send(controller, observer, 30, names, distance=1550).action == "finish_ready"
    assert controller.submitted_record_id is None and not controller.validated


def activate(observer):
    controller = RunController(named_card_mode=True)
    send(controller, observer, 1, assigned="点2")
    send(controller, observer, 2, assigned="点2")
    return controller


def test_completion_needs_two_frames_and_interruption_resets_confirmation(observer):
    c = activate(observer)
    assert send(c, observer, 3, ("点2",)).action == "wait"
    assert c.completed == 0
    assert send(c, observer, 4, ("点2",), screen="unknown").action == "pause"
    assert send(c, observer, 5, ("点2",)).action == "wait"
    send(c, observer, 6, ("点2",))
    assert c.completed == 1 and c.completed_names == ("点2",)


@pytest.mark.parametrize("names", [("点1",), ("点2", "点3")])
def test_wrong_completed_name_or_counter_skip_rejected(observer, names):
    c = activate(observer)
    assert send(c, observer, 3, names).action == "pause"
    assert c.completed == 0


def test_changed_active_target_does_not_keep_navigating_old_route(observer):
    c = activate(observer)
    assert send(c, observer, 3, assigned="点3").action == "pause"


def test_repeated_ids_equal_time_and_elapsed_rollback_rejected(observer):
    for changes in ({"frame_id": "f1"}, {"sampled_at": 1.0}, {"elapsed_s": 0}):
        c = RunController(named_card_mode=True)
        send(c, observer, 1, assigned="点2")
        assert send(c, observer, 2, assigned="点2", **changes).action == "pause"
        assert c.active is None
    c = activate(observer)
    assert send(c, observer, 3, assigned="点2", elapsed_s=1).action == "pause"


def test_legacy_unlabelled_frames_cannot_drive_named_mode():
    c = RunController(named_card_mode=True)
    value = Observation(frame_id="one", sampled_at=1, package=TARGET)
    assert c.step(value, 1).action == "pause"


def test_finish_needs_two_current_threshold_frames_not_historical_maximum(observer):
    c = RunController(named_card_mode=True, minimum_distance_m=1550)
    # 设置纯状态机测试前置条件，不冒充真实历史或设备验收。
    c.completed, c.completed_names, c.distance_m = 3, ("点0", "点1", "点2"), 1550
    assert send(c, observer, 1, c.completed_names, distance=1550).action == "wait"
    with pytest.raises(ValueError):
        c.mark_submitted("not-yet-confirmed")
    assert send(c, observer, 2, c.completed_names, distance=1540).action == "base_route"
    assert send(c, observer, 3, c.completed_names, distance=1550).action == "wait"
    assert send(c, observer, 4, c.completed_names, distance=1550).action == "finish_ready"
    assert c.finish_confirmed and c.submitted_record_id is None


def test_completion_history_names_must_not_change_even_if_counter_is_unchanged(observer):
    c = activate(observer)
    send(c, observer, 3, ("点2",))
    send(c, observer, 4, ("点2",))
    assert send(c, observer, 5, ("点1",)).action == "pause"
    assert c.completed_names == ("点2",)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 0, -1])
def test_nonfinite_controller_limits_rejected(value):
    with pytest.raises(ValueError):
        RunController(minimum_distance_m=value)
    with pytest.raises(ValueError):
        RunController(maximum_frame_age_s=value)
