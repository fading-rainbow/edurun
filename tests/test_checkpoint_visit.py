"""暂停访问合成协议测试；不连接设备、模型或原App，不产生有效成绩。"""

import json
from types import SimpleNamespace

import pytest
from test_runtime import frame, witness
from test_runtime import setup as runtime_setup

from budao.checkpoint_visit import (
    PausedCheckpointVisitor,
    PausedVisitWitness,
    observe_checkpoint_once,
)
from budao.live_runtime import RuntimeSettings
from budao.routes import RoutePlanner
from budao.simulation import demo_config


def packet(point, t, frame_id):
    now = int((t + 100) * 1e9)
    return {
        "frame_id": frame_id,
        "android_observed_ns": now,
        "snapshot": {
            "package": "com.edurun.diagnostic",
            "request_id": "e" * 32,
            "elapsed_realtime_ns": now - 10_000_000,
            "raw_motion_probe": {"request_id": "e" * 32, "sent_rows": 0},
            "gps_reception": {
                "registered": True,
                "provider": "gps",
                "task_started_ns": 1_000_000_000,
                "first_received_ns": 2_000_000_000,
                "last_received_ns": now - 100_000_000,
                "callback_count": 20,
                "has_accuracy": True,
                "accuracy_m": 2,
                "latitude": point.lat,
                "longitude": point.lon,
                "is_mock": True,
            },
        },
    }


def visit_proof(completed=0):
    p = RoutePlanner(demo_config())
    origin, target = p.config.baseline[:2]
    names = tuple(f"之前完成{i}" for i in range(completed))
    before = [
        frame(t, names, "新点", control="paused", distance_m=220, elapsed=83) for t in (10, 11)
    ]
    outbound = [
        frame(t, names, "新点", control="paused", distance_m=220, elapsed=83) for t in (12, 13)
    ]
    done = [
        frame(t, names + ("新点",), control="running", distance_m=220, elapsed=87 + t - 14)
        for t in (14, 15)
    ]
    returned = [
        frame(t, names + ("新点",), control="paused", distance_m=220, elapsed=91) for t in (16, 17)
    ]
    return PausedVisitWitness(
        original=witness(),
        origin=origin,
        target=target,
        departure=before,
        outbound=outbound,
        completed=done,
        returned=returned,
        outbound_receipts=[packet(target, f.sampled_at, f.frame_id) for f in outbound],
        returned_receipts=[packet(origin, f.sampled_at, f.frame_id) for f in returned],
        continue_action_at=13.5,
        pause_action_at=15.5,
    )


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_real_shaped_phase_proof_retains_observed_metrics_and_order(completed):
    p = visit_proof(completed)
    assert p.returned[-1].inspection.completed_checkpoints == completed + 1
    assert p.departure[0].inspection.distance_m == p.returned[-1].inspection.distance_m == 220


@pytest.mark.parametrize(
    "wrong",
    [
        "order",
        "count",
        "name",
        "duplicate",
        "clock",
        "jump_distance",
        "human",
        "nonce",
        "gps",
        "sent_motion",
        "frame_binding",
        "continue_time",
        "card_number",
    ],
)
def test_missing_actual_progress_or_receipts_cannot_confirm_visit(wrong):
    data = visit_proof().model_dump()
    if wrong == "order":
        data["returned"] = list(reversed(data["returned"]))
    elif wrong == "count":
        data["completed"][-1]["inspection"]["completed_checkpoints"] = 2
    elif wrong == "name":
        data["completed"][-1]["inspection"]["checkpoint_cards"][0]["name"] = "其他点"
    elif wrong == "duplicate":
        data["completed"][-1]["frame_id"] = data["completed"][0]["frame_id"]
    elif wrong == "clock":
        data["completed"][-1]["sampled_at"] = 12
    elif wrong == "jump_distance":
        data["returned"][-1]["inspection"]["distance_m"] = 660
    elif wrong == "human":
        data["returned"][-1]["inspection"]["issues"] = ("human_face_verification_required",)
    elif wrong == "nonce":
        data["returned_receipts"][-1]["snapshot"]["request_id"] = "f" * 32
    elif wrong == "gps":
        data["outbound_receipts"][-1]["snapshot"]["gps_reception"]["last_received_ns"] = (
            3_000_000_000
        )
    elif wrong == "sent_motion":
        data["outbound_receipts"][0]["snapshot"]["raw_motion_probe"]["sent_rows"] = 1
    elif wrong == "frame_binding":
        data["returned_receipts"][0]["frame_id"] = "not-this-frame"
    elif wrong == "card_number":
        data["completed"][-1]["inspection"]["checkpoint_cards"][0]["number"] = 2
    else:
        data["continue_action_at"] = 15
    with pytest.raises(ValueError):
        PausedVisitWitness.model_validate(data)


def test_dispatcher_accepts_only_own_current_paused_progress_without_moving(monkeypatch):
    d, w, b, clock, _ = runtime_setup.__wrapped__(monkeypatch)
    d.start(witness())
    clock[0] = 17.1
    p = visit_proof()
    d.accept_paused_visit(p)
    assert d.controller.completed == 1 and d.controller.completed_names == ("新点",)
    assert "新点" in d.observer.resumed_completed_names and "新点" not in d.observer.allowed_names
    assert not b.locations and not w.engine.enabled
    with pytest.raises(ValueError):
        d.accept_paused_visit(p)


@pytest.mark.parametrize(
    "wrong", ["other_request", "stale", "not_started", "moving", "origin", "counter"]
)
def test_dispatcher_rejects_other_or_stale_visit_without_changing_count(monkeypatch, wrong):
    d, w, b, clock, _ = runtime_setup.__wrapped__(monkeypatch)
    d.start(witness())
    clock[0] = 17.1
    if wrong == "other_request":
        d.start_request_id = "f" * 32
    elif wrong == "stale":
        clock[0] = 20
    elif wrong == "not_started":
        d.started = False
    elif wrong == "moving":
        w.engine.enabled = True
    elif wrong == "origin":
        w.engine.position = d.planner.config.baseline[1]
    else:
        d.controller.completed = 2
    count = d.controller.completed
    with pytest.raises(ValueError):
        d.accept_paused_visit(visit_proof())
    assert d.controller.completed == count and not b.locations


def test_runtime_strategy_is_opt_in_and_requires_map_configuration():
    data = {
        "mode": "known_points_trial",
        "motion_csv": ".local/m.csv",
        "independent_motion_evidence": ".local/i.json",
        "start_template": ".local/s.png",
    }
    assert RuntimeSettings.model_validate(data).checkpoint_strategy == "route"
    with pytest.raises(ValueError):
        RuntimeSettings.model_validate(data | {"checkpoint_strategy": "paused_visit_trial"})
    assert RuntimeSettings.model_validate(
        data | {"checkpoint_strategy": "paused_visit_trial", "vision_config": ".local/v.json"}
    )
    with pytest.raises(ValueError):
        RuntimeSettings.model_validate(
            data
            | {
                "checkpoint_strategy": "paused_visit_trial",
                "vision_config": ".local/v.json",
                "resume_evidence": ".local/old",
            }
        )


def observing_connector():
    p = visit_proof()
    clock = [20.0]
    running = [False]
    paused = [False]
    clicks = []
    old = p.departure[0]

    def click():
        running[0] = True
        clicks.append("continue")

    def pause(**_):
        paused[0] = True
        clicks.append("pause")

    button = SimpleNamespace(count=1, click=click, long_click=pause)

    def capture(**_):
        clock[0] += 1
        if not running[0]:
            f = frame(clock[0], assigned="新点", control="paused", distance_m=220, elapsed=83)
        else:
            f = frame(
                clock[0],
                ("新点",),
                control="paused" if paused[0] else "running",
                distance_m=220,
                elapsed=int(clock[0]) + 64,
            )
        rx = packet(p.target, clock[0], f.frame_id)
        return SimpleNamespace(
            frame=f, motion_snapshot=rx["snapshot"], android_observed_ns=rx["android_observed_ns"]
        )

    c = SimpleNamespace(
        capture=capture,
        clock=lambda: clock[0] + 0.01,
        sleep=lambda _: None,
        bridge=SimpleNamespace(check_device=lambda: None),
        ui=SimpleNamespace(device=lambda **_: button),
    )
    cal = {
        "paused_frame": old.model_dump(mode="json"),
        "command_target": p.target.model_dump(),
        "name": "新点",
    }
    return c, cal, clicks


def test_normal_continue_observes_two_completed_frames_then_pauses_once(tmp_path):
    c, cal, clicks = observing_connector()
    result = observe_checkpoint_once(c, cal, lambda: True, gps_request="e" * 32, evidence=tmp_path)
    assert (
        clicks == ["continue", "pause"]
        and result["final"]["inspection"]["completed_checkpoints"] == 1
    )
    assert not result["motion_started"] and not result["submitted"]


def test_without_target_reception_no_continue_is_sent(tmp_path):
    c, cal, clicks = observing_connector()
    with pytest.raises(ValueError):
        observe_checkpoint_once(c, cal, lambda: True, gps_request="f" * 32, evidence=tmp_path)
    assert not clicks


@pytest.mark.parametrize(
    "update",
    [
        {"package": "other.app"},
        {"version": "unknown"},
        {"gps_label": "GPS信号弱"},
        {"issues": ("human_face_verification_required",)},
    ],
)
def test_unknown_or_verification_page_cannot_continue(tmp_path, update):
    c, cal, clicks = observing_connector()
    capture = c.capture

    def changed(**kwargs):
        assert not kwargs.get("pixels")
        cap = capture(**kwargs)
        cap.frame = cap.frame.model_copy(
            update={"inspection": cap.frame.inspection.model_copy(update=update)}
        )
        return cap

    c.capture = changed
    with pytest.raises(ValueError):
        observe_checkpoint_once(c, cal, lambda: True, gps_request="e" * 32, evidence=tmp_path)
    assert not clicks and not (tmp_path / "continue-action.json").exists()


@pytest.mark.parametrize("wrong", ["human", "jump_distance", "nonce", "elapsed"])
def test_continue_failure_preserves_real_actions_without_confirming_or_returning(tmp_path, wrong):
    c, cal, clicks = observing_connector()
    capture = c.capture

    def changed(**kwargs):
        assert not kwargs.get("pixels")
        cap = capture(**kwargs)
        if "continue" in clicks:
            update = {}
            if wrong == "human":
                update["issues"] = ("human_face_verification_required",)
            elif wrong == "jump_distance":
                update["distance_m"] = 660
            elif wrong == "elapsed":
                update["elapsed_s"] = None
            else:
                cap.motion_snapshot["request_id"] = "f" * 32
            cap.frame = cap.frame.model_copy(
                update={"inspection": cap.frame.inspection.model_copy(update=update)}
            )
        return cap

    c.capture = changed
    with pytest.raises(ValueError):
        observe_checkpoint_once(c, cal, lambda: True, gps_request="e" * 32, evidence=tmp_path)
    assert clicks == (["continue"] if wrong == "human" else ["continue", "pause"])
    assert (tmp_path / "continue-action.json").exists()
    assert not (tmp_path / "checkpoint-observation.json").exists()


def visitor_case(tmp_path, monkeypatch, *, failure=None):
    from datetime import date

    p = visit_proof()
    events = []
    accepted = []
    owners = []
    (tmp_path / "start-witness.json").write_text(p.original.model_dump_json(), "utf-8")

    def command(*args):
        if args == ("shell", "run-as", "com.edurun.diagnostic", "cat", "files/motion-service.json"):
            return json.dumps({"request_id": ("f" if failure == "owner" else "e") * 32})
        if args == ("shell", "dumpsys", "sensorservice"):
            return "Mode: " + ("DATA_INJECTION" if failure == "mode" else "NORMAL")
        assert args == ("shell", "am", "force-stop", "com.edurun.diagnostic")
        events.append("close_observer")
        return ""

    c = SimpleNamespace(
        evidence=tmp_path, bridge=SimpleNamespace(run=command), exclude_route_calibration=False
    )
    d = SimpleNamespace(planner=RoutePlanner(demo_config()), accept_paused_visit=accepted.append)
    cal = {
        "number": 1,
        "name": "新点",
        "command_current": p.origin.model_dump(),
        "command_target": p.target.model_dump(),
    }

    def resolve(authorize, *, calibration_only):
        assert authorize() is True and calibration_only is True
        return cal

    recovery = SimpleNamespace(connector=c, dispatcher=d, resolve=resolve)

    class Motion:
        request = "e" * 32

        def __init__(self, *a, **kw):
            assert kw == {"bounded_stream": True, "replay_passes": 1}
            self.preload_requested = False

        def prepare(self, _):
            events.append("prepare")

        def preload(self, _):
            self.preload_requested = True
            events.append("preload")

        def stop(self):
            events.append("stop")
            if failure == "stop":
                raise TimeoutError("unknown stop")

    monkeypatch.setattr("budao.checkpoint_visit.ForegroundMotionClient", Motion)

    def move(*args, **kwargs):
        assert c.exclude_route_calibration and kwargs["gps_request"] == "e" * 32
        events.append("outbound")
        return {
            "received_target_twice": failure != "outbound",
            "before": [f.model_dump(mode="json") for f in p.departure],
            "after": [f.model_dump(mode="json") for f in p.outbound],
            "input_observations": [r.model_dump() for r in p.outbound_receipts],
        }

    monkeypatch.setattr("budao.checkpoint_visit.relocate_paused_once", move)

    def observe(*a, **kw):
        events.append("observe")
        return {
            "observed": [f.model_dump(mode="json") for f in p.completed],
            "final": p.returned[0].model_dump(mode="json"),
            "motion_started": False,
            "submitted": False,
            "continue_action_at": p.continue_action_at,
            "pause_action_at": p.pause_action_at,
        }

    monkeypatch.setattr("budao.checkpoint_visit.observe_checkpoint_once", observe)

    def returned(*a, **kw):
        events.append("return")
        return {
            "received_target_twice": failure != "return",
            "after": [f.model_dump(mode="json") for f in p.returned],
            "input_observations": [r.model_dump() for r in p.returned_receipts],
        }

    monkeypatch.setattr("budao.checkpoint_visit.return_to_pause_anchor_once", returned)
    visitor = PausedCheckpointVisitor(
        recovery, object(), lambda: date(2026, 10, 22), on_observer=owners.append
    )
    return visitor, c, events, accepted, owners


def test_visitor_composes_center_outbound_actual_confirmation_return_and_owned_cleanup(
    tmp_path, monkeypatch
):
    visitor, c, events, accepted, owners = visitor_case(tmp_path, monkeypatch)
    visitor.visit(lambda: True)
    assert events == [
        "prepare",
        "preload",
        "outbound",
        "observe",
        "return",
        "stop",
        "close_observer",
    ]
    assert len(accepted) == 1 and isinstance(accepted[0], PausedVisitWitness) and len(owners) == 1
    assert (
        not c.exclude_route_calibration
        and (tmp_path / "paused-visit-1/visit-witness.json").exists()
    )
    with pytest.raises(FileExistsError):
        visitor.visit(lambda: True)
    assert events.count("outbound") == 1


@pytest.mark.parametrize("failure", ["outbound", "return", "stop"])
def test_unconfirmed_visit_stops_own_observer_and_restores_trace_tag(
    tmp_path, monkeypatch, failure
):
    visitor, c, events, accepted, _ = visitor_case(tmp_path, monkeypatch, failure=failure)
    with pytest.raises((ValueError, TimeoutError)):
        visitor.visit(lambda: True)
    assert events[-1] == ("stop" if failure == "stop" else "close_observer")
    assert not c.exclude_route_calibration
    if failure != "stop":
        assert not accepted
    if failure == "outbound":
        assert "observe" not in events and "return" not in events


@pytest.mark.parametrize("failure", ["owner", "mode"])
def test_observer_close_cannot_terminate_foreign_owner_or_unknown_mode(
    tmp_path, monkeypatch, failure
):
    visitor, c, events, _, _ = visitor_case(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(ValueError):
        visitor.visit(lambda: True)
    assert events[-1] == "stop" and "close_observer" not in events
    assert not c.exclude_route_calibration
