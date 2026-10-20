"""Synthetic continuation evidence. No ADB, actual running, or grade claims."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from test_runtime import frame, witness
from test_runtime import setup as build_runtime_setup

from budao.resume import ResumeWitness, resume_once
from budao.target import RUN_ACTIVITY, TARGET


def proof():
    return ResumeWitness(
        original=witness(),
        saved_pause=frame(10, control="paused", distance_m=60, elapsed=580),
        first_paused=frame(20, control="paused", distance_m=60, elapsed=580),
        second_paused=frame(21, control="paused", distance_m=60, elapsed=580),
        action_at=21.5,
        first_running=frame(22, distance_m=60, elapsed=581),
        second_running=frame(23, distance_m=60, elapsed=582),
    )


def progress_proof(completed):
    names = tuple(f"已完成{i}" for i in range(completed))
    assigned = "点4" if completed < 3 else None
    return ResumeWitness(
        original=witness(),
        saved_pause=frame(10, names, assigned, control="paused", distance_m=900, elapsed=580),
        first_paused=frame(20, names, assigned, control="paused", distance_m=900, elapsed=580),
        second_paused=frame(21, names, assigned, control="paused", distance_m=900, elapsed=580),
        action_at=21.5,
        first_running=frame(22, names, assigned, distance_m=900, elapsed=581),
        second_running=frame(23, names, assigned, distance_m=900, elapsed=582),
    )


@pytest.fixture
def runtime_setup(monkeypatch):
    return build_runtime_setup.__wrapped__(monkeypatch)


def test_resume_is_not_a_new_zero_start_and_preserves_current_clock(runtime_setup):
    d, w, b, clock, _ = runtime_setup
    clock[0] = 23
    d.resume(proof())
    assert d.started and not w.engine.enabled and not b.locations
    assert d.controller.distance_m == 60 and d.controller.last_elapsed_s == 582
    assert d.controller.completed == 0
    with pytest.raises(ValueError):
        d.resume(proof())


@pytest.mark.parametrize("completed", [1, 2, 3])
def test_resume_retains_actual_ordered_progress_without_granting_completed_point_coordinates(
    runtime_setup, completed
):
    d, w, b, clock, _ = runtime_setup
    clock[0] = 23
    p = progress_proof(completed)
    d.resume(p)
    names = p.saved_pause.inspection.checkpoint_cards[:completed]
    assert d.controller.completed == completed
    assert d.controller.completed_names == tuple(c.name for c in names)
    assert not w.engine.enabled and not b.locations
    assert "已完成0" not in d.observer.allowed_names
    with pytest.raises(ValueError):
        d.observer.navigation_point("已完成0")
    after = frame(
        24,
        tuple(c.name for c in names),
        "点4" if completed < 3 else None,
        distance_m=900,
        elapsed=583,
    )
    o = d.observer.observe(after.inspection, after.sampled_at, after.frame_id)
    assert o.screen == "running" and o.completed_checkpoint_names == d.controller.completed_names


def test_completed_only_name_is_not_an_allowed_future_navigation_target(runtime_setup):
    d, _, _, clock, _ = runtime_setup
    clock[0] = 23
    d.resume(progress_proof(1))
    after = frame(24, ("已完成0",), "已完成0", distance_m=900, elapsed=583)
    assert d.observer.observe(after.inspection, 24, after.frame_id).screen == "unknown"


def test_partial_resume_rejects_changed_completion_count_or_order():
    p = progress_proof(2)
    for changed in (
        frame(23, ("已完成0",), "点4", distance_m=900, elapsed=582),
        frame(23, ("已完成1", "已完成0"), "点4", distance_m=900, elapsed=582),
    ):
        data = p.model_dump()
        data["second_running"] = changed.model_dump()
        with pytest.raises(ValueError):
            ResumeWitness.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"action_at": 22.5},
        {"second_running": frame(23, distance_m=60, elapsed=581)},
        {"second_running": frame(23, distance_m=70, elapsed=582)},
        {"second_paused": frame(21, control="paused", distance_m=60, elapsed=579)},
        {"second_paused": frame(20, control="paused", distance_m=60, elapsed=580)},
        {"second_running": frame(23, ("点1",), distance_m=60, elapsed=582)},
        {"first_running": frame(22, assigned="点1", distance_m=60, elapsed=581)},
    ],
)
def test_changed_record_or_missing_causality_is_not_resume_evidence(changes):
    data = proof().model_dump()
    data.update({k: v.model_dump() if hasattr(v, "model_dump") else v for k, v in changes.items()})
    with pytest.raises(ValueError):
        ResumeWitness.model_validate(data)


def test_forged_nested_new_start_revalidated_by_dispatcher(runtime_setup):
    d, _, _, clock, _ = runtime_setup
    clock[0] = 23
    forged = proof().model_copy(
        update={"original": witness().model_copy(update={"start_ui_verified": False})}
    )
    with pytest.raises(ValueError):
        d.resume(forged)


def test_resume_action_once_then_two_actual_increasing_frames(tmp_path):
    p = proof()
    source = iter(p.frames)
    clock = [21.5]
    clicks = []
    focus = f"mCurrentFocus=Window{{a u0 {TARGET}/{RUN_ACTIVITY}}}"
    b = SimpleNamespace(
        check_device=lambda: None, run=lambda *a: "2026-10-14" if "date" in a else focus
    )
    button = SimpleNamespace(count=1, click=lambda: clicks.append("continue"))
    c = SimpleNamespace(
        bridge=b,
        used_requests=set(),
        evidence=tmp_path,
        clock=lambda: clock[0],
        sleep=lambda _: None,
        ui=SimpleNamespace(device=lambda **_: button),
        last_action_request=None,
    )

    def capture():
        f = next(source)
        if f.inspection.run_control_state == "running":
            clock[0] = f.sampled_at + 0.1
        return SimpleNamespace(frame=f)

    c.capture = capture
    result = resume_once(c, p.original, p.saved_pause, datetime(2026, 10, 14), lambda: True)
    assert result == p and clicks == ["continue"]
    assert c.last_action_request == p.original.request_id
    with pytest.raises(ValueError):
        resume_once(c, p.original, p.saved_pause, datetime(2026, 10, 14), lambda: True)


def test_changed_current_pause_refuses_continue(tmp_path):
    p = proof()
    b = SimpleNamespace(run=lambda *a: "2026-10-14")
    c = SimpleNamespace(
        bridge=b,
        used_requests=set(),
        evidence=tmp_path,
        capture=lambda: SimpleNamespace(
            frame=frame(20, control="paused", distance_m=70, elapsed=580)
        ),
    )
    with pytest.raises(ValueError):
        resume_once(c, p.original, p.saved_pause, datetime(2026, 10, 14), lambda: True)
    assert not list(tmp_path.iterdir())


def test_post_continue_weak_signal_proves_timer_but_does_not_arm_movement(runtime_setup):
    data = proof().model_dump()
    for key in ("first_running", "second_running"):
        data[key]["inspection"]["gps_label"] = "GPS信号弱"
    p = ResumeWitness.model_validate(data)
    d, w, b, clock, _ = runtime_setup
    clock[0] = 23
    d.resume(p)
    assert d.started and not w.engine.enabled and not b.locations


@pytest.mark.parametrize("key", ["first_paused", "second_paused"])
def test_continue_still_requires_ready_pre_action_observations(key):
    data = proof().model_dump()
    data[key]["inspection"]["gps_label"] = "GPS信号弱"
    with pytest.raises(ValueError):
        ResumeWitness.model_validate(data)


def test_assigned_target_is_inherited_from_actual_two_frame_resume_without_arming(runtime_setup):
    data = proof().model_dump()
    for key in ("saved_pause", "first_paused", "second_paused", "first_running", "second_running"):
        data[key]["inspection"]["checkpoint_cards"] = [
            {"number": 1, "state": "assigned", "name": "点4", "distance_m": 120}
        ]
    p = ResumeWitness.model_validate(data)
    d, w, b, clock, _ = runtime_setup
    clock[0] = 23
    d.resume(p)
    assert d.controller.active_name == "点4" and not w.engine.enabled and not b.locations


@pytest.mark.parametrize("delta,accepted", [(0.97, True), (5.01, False)])
def test_resume_range_refresh_is_bounded_to_saved_card_not_mistaken_for_identity(delta, accepted):
    data = proof().model_dump()
    keys = ("saved_pause", "first_paused", "second_paused", "first_running", "second_running")
    for key in keys:
        data[key]["inspection"]["checkpoint_cards"] = [
            {
                "number": 1,
                "state": "assigned",
                "name": "点4",
                "distance_m": 120 + (delta if key != "saved_pause" else 0),
            }
        ]
    if accepted:
        result = ResumeWitness.model_validate(data)
        assert result.saved_pause.inspection.checkpoint_cards[0].distance_m == 120
        assert result.second_running.inspection.checkpoint_cards[0].distance_m == 120 + delta
    else:
        with pytest.raises(ValueError):
            ResumeWitness.model_validate(data)
