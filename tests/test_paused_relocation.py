"""合成暂停跳转协议；不是正式成绩证据。"""

from copy import deepcopy
from datetime import date
from subprocess import TimeoutExpired
from types import SimpleNamespace

import pytest
from test_runtime import FakeBridge, frame, witness

from budao.paused_relocation import relocate_paused_once, return_to_pause_anchor_once
from budao.routes import RoutePlanner
from budao.simulation import demo_config


def case(tmp_path, monkeypatch):
    from budao.android import DIAGNOSTIC_PACKAGE

    planner = RoutePlanner(demo_config())
    current, target = planner.config.baseline[:2]
    bridge = FakeBridge()
    bridge.run = lambda *_: "Mode: NORMAL"
    clock = [20.0]
    saved = frame(10, assigned="点一", control="paused", distance_m=220, elapsed=83)
    proof = {
        "paused_frame": saved.model_dump(mode="json"),
        "command_current": current.model_dump(),
        "received_current": current.model_dump(),
        "command_target": target.model_dump(),
        "received_target": target.model_dump(),
        "original_request": witness().request_id,
        "start_anchor_frame_id": witness().before.frame_id,
        "name": "点一",
        "number": 1,
        "actual_range_m": 120,
        "predicted_range_m": 120,
        "navigation_authorized": False,
        "position_command_sent": False,
        "app_confirmed": False,
    }
    caps = []

    def capture(**_):
        clock[0] += 0.1
        value = saved.model_copy(update={"frame_id": str(clock[0]), "sampled_at": clock[0]})
        cap = SimpleNamespace(
            frame=value,
            android_observed_ns=int(clock[0] * 1e9),
            motion_snapshot={
                "package": DIAGNOSTIC_PACKAGE,
                "request_id": "e" * 32,
                "gps_reception": {"last_received_ns": int(clock[0] * 1e9)},
                "raw_motion_probe": {"sent_rows": 0},
            },
        )
        caps.append(cap)
        return cap

    connector = SimpleNamespace(
        bridge=bridge, capture=capture, clock=lambda: clock[0], sleep=lambda _: None
    )
    monkeypatch.setattr("budao.paused_relocation.read_gps_position", lambda _: current)
    # 日期从真实设备读取；这里只替代合成接头，不能把旧日期当现场。
    original_run = bridge.run
    bridge.run = lambda *a: "2026-10-20" if a[-1] == "+%F" else original_run(*a)
    monkeypatch.setattr("budao.paused_relocation.received_gps_matches", lambda *_: True)
    return connector, proof, planner, caps


def run(c, proof, planner, tmp_path, authorize=lambda: True):
    return relocate_paused_once(
        c,
        witness(),
        proof,
        planner,
        tmp_path,
        authorize,
        gps_request="e" * 32,
        start_day=date(2026, 10, 20),
    )


def test_one_location_no_continue_motion_or_grade_and_no_retry(tmp_path, monkeypatch):
    c, proof, p, caps = case(tmp_path, monkeypatch)
    original = deepcopy(proof)
    result = run(c, proof, p, tmp_path)
    assert len(c.bridge.locations) == 1 and result["received_target_twice"]
    assert not result["continued"] and not result["submitted"] and not result["record_valid_proven"]
    assert proof == original and len(caps) == 4
    with pytest.raises(FileExistsError):
        run(c, proof, p, tmp_path)
    assert len(c.bridge.locations) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_request", "b" * 32),
        ("start_anchor_frame_id", "wrong"),
        ("name", "其他点"),
        ("navigation_authorized", True),
        ("position_command_sent", True),
        ("app_confirmed", True),
        ("predicted_range_m", 124),
        ("command_target", {"lat": 60, "lon": 100}),
    ],
)
def test_wrong_or_unbounded_calibration_never_outputs(tmp_path, monkeypatch, field, value):
    c, proof, p, _ = case(tmp_path, monkeypatch)
    proof[field] = value
    with pytest.raises(ValueError):
        run(c, proof, p, tmp_path)
    assert not c.bridge.locations


@pytest.mark.parametrize(
    "change",
    [
        {"run_control_state": "running"},
        {"elapsed_s": 84},
        {"distance_m": 230},
        {"completed_checkpoints": 1},
        {"issues": ("human_face_verification_required",)},
    ],
)
def test_changed_actual_pause_or_human_verification_never_outputs(tmp_path, monkeypatch, change):
    c, proof, p, _ = case(tmp_path, monkeypatch)
    capture = c.capture

    def changed(**kw):
        cap = capture(**kw)
        cap.frame = cap.frame.model_copy(
            update={"inspection": cap.frame.inspection.model_copy(update=change)}
        )
        return cap

    c.capture = changed
    with pytest.raises(ValueError):
        run(c, proof, p, tmp_path)
    assert not c.bridge.locations


def test_unknown_command_result_consumes_attempt_without_retry(tmp_path, monkeypatch):
    c, proof, p, _ = case(tmp_path, monkeypatch)
    attempts = []

    def fail(*a, **kw):
        attempts.append(1)
        raise TimeoutError("unknown")

    c.bridge.location_wgs84 = fail
    with pytest.raises(TimeoutError):
        run(c, proof, p, tmp_path)
    with pytest.raises(FileExistsError):
        run(c, proof, p, tmp_path)
    assert len(attempts) == 1


def test_no_permission_or_fresh_receiver_never_outputs(tmp_path, monkeypatch):
    c, proof, p, _ = case(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        run(c, proof, p, tmp_path, lambda: False)
    monkeypatch.setattr("budao.paused_relocation.received_gps_matches", lambda *_: False)
    with pytest.raises(ValueError):
        run(c, proof, p, tmp_path)
    assert not c.bridge.locations


def test_three_second_gps_callback_interval_waits_for_fresh_data_without_output_retry(
    tmp_path, monkeypatch
):
    c, proof, p, caps = case(tmp_path, monkeypatch)
    answers = iter([True, False, False, True, True, True])
    monkeypatch.setattr("budao.paused_relocation.received_gps_matches", lambda *_: next(answers))
    result = run(c, proof, p, tmp_path)
    assert result["received_target_twice"] and len(caps) == 6
    assert len(c.bridge.locations) == 1


def test_vendor_timeout_preserves_unknown_ack_and_only_observes_actual_receipts(
    tmp_path, monkeypatch
):
    c, proof, p, _ = case(tmp_path, monkeypatch)
    attempts = []

    def timeout(*a, **kw):
        attempts.append(1)
        raise TimeoutExpired(["owned-vendor-location"], 1)

    c.bridge.location_wgs84 = timeout
    result = run(c, proof, p, tmp_path)
    assert result["output_state_unknown"] and not result["acknowledged"]
    assert result["received_target_twice"] and len(attempts) == 1
    with pytest.raises(FileExistsError):
        run(c, proof, p, tmp_path)
    assert len(attempts) == 1


def return_case(tmp_path, monkeypatch):
    c, proof, planner, _ = case(tmp_path, monkeypatch)
    ended = frame(13, ("点一",), control="paused", distance_m=220, elapsed=91)
    visit = {
        "observed": [
            frame(t, ("点一",), distance_m=220, elapsed=87 + t - 11).model_dump(mode="json")
            for t in (11, 12)
        ],
        "final": ended.model_dump(mode="json"),
        "submitted": False,
        "motion_started": False,
    }
    original_capture = c.capture

    def capture(**kw):
        cap = original_capture(**kw)
        cap.frame = cap.frame.model_copy(update={"inspection": ended.inspection})
        return cap

    c.capture = capture
    monkeypatch.setattr(
        "budao.paused_relocation.read_gps_position", lambda _: planner.config.baseline[1]
    )
    return c, proof, planner, visit


def test_return_is_exact_original_anchor_after_actual_two_frame_checkpoint(tmp_path, monkeypatch):
    c, proof, p, visit = return_case(tmp_path, monkeypatch)
    result = return_to_pause_anchor_once(
        c,
        witness(),
        proof,
        visit,
        p,
        tmp_path,
        lambda: True,
        gps_request="e" * 32,
        start_day=date(2026, 10, 20),
    )
    assert c.bridge.locations == [p.config.baseline[0]]
    assert result["received_target_twice"] and not result["submitted"]


@pytest.mark.parametrize(
    "change", ["only_one", "wrong_name", "running", "distance_jump", "submitted"]
)
def test_return_refuses_unconfirmed_other_or_changed_record(tmp_path, monkeypatch, change):
    c, proof, p, visit = return_case(tmp_path, monkeypatch)
    if change == "only_one":
        visit["observed"].pop()
    elif change == "wrong_name":
        visit["observed"][-1]["inspection"]["checkpoint_cards"][0]["name"] = "其他"
    elif change == "running":
        visit["final"]["inspection"]["run_control_state"] = "running"
    elif change == "distance_jump":
        visit["final"]["inspection"]["distance_m"] = 650
    else:
        visit["submitted"] = True
    with pytest.raises(ValueError):
        return_to_pause_anchor_once(
            c,
            witness(),
            proof,
            visit,
            p,
            tmp_path,
            lambda: True,
            gps_request="e" * 32,
            start_day=date(2026, 10, 20),
        )
    assert not c.bridge.locations
