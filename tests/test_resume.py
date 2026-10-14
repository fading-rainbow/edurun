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
