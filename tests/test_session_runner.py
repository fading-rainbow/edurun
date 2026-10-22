"""单次编排的合成组件；不连接ADB，不模拟实际验收或成绩。"""

import json
from types import SimpleNamespace

import pytest
from test_raw_motion import clip
from test_runtime import frame, witness

from budao.foreground_motion import ForegroundMotionClient
from budao.models import Decision, GeoPoint
from budao.mumu import MuMuBridge
from budao.runtime import RuntimeDispatcher
from budao.sensor_probe import INJECTION, NORMAL
from budao.session_runner import SessionRunner
from budao.start_connector import RunCapture, StartConnector
from budao.target import TargetInspection


class Bridge(MuMuBridge):
    def __init__(self):
        self.calls, self.mode, self.owner = [], NORMAL, "a" * 32
        self.mode_timeout = False

    def check_device(self):
        pass

    def run(self, *args):
        self.calls.append(args)
        if args == ("shell", "dumpsys", "sensorservice"):
            return "Mode: " + self.mode
        if "data_injection" in args:
            self.mode = INJECTION
            if self.mode_timeout:
                raise TimeoutError("PRIVATE_ERROR")
        if "enable" in args:
            self.mode = NORMAL
        if "run-as" in args:
            return json.dumps({"request_id": self.owner})
        return ""


class Motion(ForegroundMotionClient):
    def __init__(self, bridge):
        super().__init__(bridge, "a" * 32, bounded_stream=True)
        self.calls = []

    def prepare(self, _clip):
        self.calls.append("prepare")

    def preload(self, _clip):
        self.preload_requested = True
        self.calls.append("preload")

    def start(self, observed_ns):
        self.started = True
        self.calls.append(("start", observed_ns))

    def stop(self):
        self.calls.append("stop")


class Dispatcher(RuntimeDispatcher):
    def __init__(self, bridge):
        self.worker = SimpleNamespace(
            bridge=bridge,
            thread=None,
            engine=SimpleNamespace(position=GeoPoint(lon=120, lat=30)),
            pause=lambda *_: None,
        )
        self.pace_range_s_per_km, self.asynchronous_renewals = (180, 600), True
        self.started = False
        self.controller = SimpleNamespace(completed=0, completed_names=(), active_name=None)
        self.observer = SimpleNamespace(confirmed_names=frozenset())
        self.calls = []

    def start(self, proof):
        assert proof == witness()
        self.started = True
        self.calls.append("start")

    def update(self, _frame):
        self.calls.append("update")
        if self.calls.count("update") == 1:
            return Decision(action="base_route", reason="Synthetic startup routing")
        return Decision(action="finish_ready", reason="Synthetic terminal decision")

    def stop(self):
        self.calls.append("stop")


class Connector(StartConnector):
    def __init__(self, bridge, clock, source):
        self.bridge, self.clock, self.source = bridge, clock, source
        self.last_action_request = None
        self.calls, self.paused = [], False
        self.start_failure, self.face, self.expired = False, False, False
        self.gps_invalid = False
        button = SimpleNamespace(count=1, long_click=lambda **_: setattr(self, "paused", True))
        self.ui = SimpleNamespace(device=lambda **_: button)

    def begin_once(self, request, authorize, **kwargs):
        assert authorize() is True
        assert kwargs["gps_position"] == GeoPoint(lon=120, lat=30)
        self.calls.append("begin")
        self.last_action_request = request
        if self.start_failure:
            raise ValueError("PRIVATE_START_ERROR")
        return witness()

    def capture(self, **kwargs):
        self.calls.append("capture")
        self.clock[0] += 0.1
        f = frame(self.clock[0], control="paused" if self.paused else "running")
        if kwargs.get("renew_motion") is not None and self.face:
            f = f.model_copy(
                update={
                    "inspection": TargetInspection(issues=("human_face_verification_required",))
                }
            )
        state = {
            "request_id": "a" * 32,
            "clip_sha256": self.source.sha256,
            "bounded_stream": True,
            "replay_passes": 1,
            "state": "lease_expired" if self.expired else "running",
        }
        result = RunCapture(
            f,
            "<hierarchy/>",
            None,
            2_000_000_000,
            {
                "package": "com.edurun.diagnostic",
                "request_id": "a" * 32,
                "elapsed_realtime_ns": 1_950_000_000,
                "gps_reception": {
                    "registered": True,
                    "provider": "gps",
                    "task_started_ns": 1_000_000_000,
                    "first_received_ns": 1_200_000_000,
                    "last_received_ns": 1_900_000_000,
                    "callback_count": 3,
                    "has_accuracy": True,
                    "accuracy_m": 2,
                    "latitude": 30,
                    "longitude": 120,
                    "is_mock": False,
                },
                "raw_motion_probe": state,
            },
        )
        if self.gps_invalid:
            result.motion_snapshot["gps_reception"]["last_received_ns"] = 1_000_000_000
        return result


def setup(tmp_path):
    source = clip(tmp_path)
    clock = [2.0]
    bridge = Bridge()
    motion = Motion(bridge)
    dispatcher = Dispatcher(bridge)
    connector = Connector(bridge, clock, source)
    runner = SessionRunner(
        connector, dispatcher, motion, source, maximum_s=1, clock=lambda: clock[0]
    )
    return runner, bridge, connector, dispatcher, motion


def test_once_only_kernel_composes_prepare_start_capture_dispatch_and_cleanup(tmp_path):
    r, b, c, d, m = setup(tmp_path)
    reports = []
    result = r.run(lambda: True, reports.append)
    assert result["state"] == "finish_ready" and result["run_started"]
    assert result["mode_restored_normal"] and result["app_pause_confirmed"]
    assert result["records_submitted"] == 0 and not result["record_valid_proven"]
    assert m.calls[:2] == ["prepare", "preload"] and m.calls[-1] == "stop"
    assert d.calls == ["start", "update", "update", "stop"] and c.calls.count("begin") == 1
    assert b.mode == NORMAL
    with pytest.raises(ValueError, match="已经消费"):
        r.run(lambda: True, reports.append)


@pytest.mark.parametrize("failure", ["partial_start", "mode_timeout", "face", "expired"])
def test_partial_start_timeout_verification_and_expiry_preserve_record_and_clean_up(
    tmp_path, failure
):
    r, b, c, d, m = setup(tmp_path)
    if failure == "partial_start":
        c.start_failure = True
    elif failure == "mode_timeout":
        b.mode_timeout = True
    else:
        setattr(c, failure, True)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == ("verification_required" if failure == "face" else "error")
    assert result["records_submitted"] == 0 and b.mode == NORMAL
    assert "PRIVATE_" not in str(result) and m.calls[-1] == "stop" and d.calls[-1] == "stop"
    assert c.calls.count("begin") == 1
    if failure == "partial_start":
        assert result["start_action_attempted"] and not result["run_started"]


def test_changed_mode_owner_is_not_force_stopped_or_globally_reset(tmp_path):
    r, b, _c, _d, _m = setup(tmp_path)
    b.owner = "f" * 32
    result = r.run(lambda: True, lambda _: None)
    assert result["cleanup_unconfirmed"] and b.mode == INJECTION
    assert not any("force-stop" in call or "enable" in call for call in b.calls)


@pytest.mark.parametrize("terminal", ["lease_expired", "clock_gap"])
def test_expired_own_window_uses_normal_pause_recovery_without_another_start(
    tmp_path, monkeypatch, terminal
):
    r, _b, c, _d, _m = setup(tmp_path)
    c.expired = True
    r.recover_expired_windows = True
    calls = []
    capture = c.capture

    def terminal_capture(**kwargs):
        cap = capture(**kwargs)
        if kwargs.get("renew_motion") and c.expired:
            cap.motion_snapshot["raw_motion_probe"]["state"] = terminal
        return cap

    monkeypatch.setattr(c, "capture", terminal_capture)

    def recover(cancel, authorize, *, pause_record=False):
        assert not cancel.is_set() and authorize() is True and pause_record is True
        calls.append(1)
        c.expired = False
        return 0.1

    monkeypatch.setattr(r, "_human_wait", recover)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "finish_ready" and result["transient_input_recoveries"] == 1
    assert calls == [1] and c.calls.count("begin") == 1 and result["records_submitted"] == 0


def test_failed_expired_window_recovery_preserves_record(tmp_path, monkeypatch):
    r, b, c, _d, _m = setup(tmp_path)
    r.recover_expired_windows = True
    c.expired = True
    monkeypatch.setattr(r, "_human_wait", lambda *_a, **_kw: None)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "paused" and result["records_submitted"] == 0 and b.mode == NORMAL
    assert c.calls.count("begin") == 1


def test_expired_window_recovery_is_bounded_not_an_infinite_restart(tmp_path, monkeypatch):
    r, _b, c, _d, _m = setup(tmp_path)
    r.recover_expired_windows = True
    c.expired = True
    r.maximum_s = 10
    calls = []
    monkeypatch.setattr(r, "_human_wait", lambda *_a, **_kw: calls.append(1) or 0.1)
    result = r.run(lambda: True, lambda _: None)
    assert calls == [1, 1, 1] and result["state"] == "error" and c.calls.count("begin") == 1


def test_cleanup_failure_detail_stays_private_and_does_not_clear_unknown_state(
    tmp_path, monkeypatch
):
    r, b, _c, d, _m = setup(tmp_path)

    def fail():
        raise TimeoutError("PRIVATE_CLEANUP_DIAGNOSIS")

    monkeypatch.setattr(d, "stop", fail)
    reports = []
    result = r.run(lambda: True, reports.append)
    assert result["cleanup_unconfirmed"] and result["records_submitted"] == 0
    assert r.private_cleanup_failures == [
        {"stage": "position_stop", "type": "TimeoutError", "detail": "PRIVATE_CLEANUP_DIAGNOSIS"}
    ]
    assert "PRIVATE_CLEANUP" not in str(result) + str(reports) and b.mode == NORMAL


def test_denied_start_never_attempts_start_or_enters_injection(tmp_path):
    r, b, c, _d, m = setup(tmp_path)
    result = r.run(lambda: False, lambda _: None)
    assert not result["run_started"] and "begin" not in c.calls and "prepare" not in m.calls
    assert not any("data_injection" in call for call in b.calls)


def test_record_recovery_guard_emits_fixed_reason_without_starting_input(tmp_path):
    from budao.portal import RecordRecoveryRequired

    r, b, c, _d, m = setup(tmp_path)

    def blocked(_authorize):
        raise RecordRecoveryRequired("PRIVATE_SENSITIVE")

    r.prepare_start = blocked
    result = r.run(lambda: True, lambda _: None)
    assert result["preparation_phase"] == "record_recovery"
    assert result["message"] == "原App有恢复记录提示，需本人处理；未开始新的乐跑"
    assert "PRIVATE_SENSITIVE" not in str(result)
    assert not result["run_started"] and not result["start_action_attempted"]
    assert "begin" not in c.calls and "prepare" not in m.calls and b.mode == NORMAL


def test_same_anchor_bootstrap_is_after_real_witness_and_mode_before_motion_start(
    tmp_path, monkeypatch
):
    r, b, c, d, m = setup(tmp_path)
    pulses = []
    start = d.start

    def started(proof):
        start(proof)
        c.gps_invalid = True

    monkeypatch.setattr(d, "start", started)

    def refresh(proof, authorize):
        assert proof == witness() and c.last_action_request == m.request
        assert d.started and b.mode == INJECTION and not m.started
        assert authorize() is True
        pulses.append("same_anchor_only")
        c.gps_invalid = False

    r.refresh_started_gps = refresh
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "finish_ready" and pulses == ["same_anchor_only"]


def test_already_fresh_post_start_gps_does_not_refresh_and_reset_provider(tmp_path):
    r, _, _, _, _ = setup(tmp_path)

    def unnecessary_refresh(*_):
        raise AssertionError("Fresh actual GPS must not be reset")

    r.refresh_started_gps = unnecessary_refresh
    assert r.run(lambda: True, lambda _: None)["state"] == "finish_ready"


def test_post_start_weak_signal_waits_boundedly_for_real_ready_frame_before_input(
    tmp_path, monkeypatch
):
    r, b, c, d, m = setup(tmp_path)
    capture, start = c.capture, m.start
    observed, refreshes = [], []

    def refresh(proof, authorize):
        assert proof == witness() and authorize() is True
        assert b.mode == INJECTION and not m.started and "update" not in d.calls
        refreshes.append("same_anchor")

    def transient(**kwargs):
        cap = capture(**kwargs)
        if c.last_action_request is not None and not m.started and not c.paused:
            label = "GPS信号弱" if len(observed) < 2 else "GPS信号优"
            observed.append(label)
            return RunCapture(
                cap.frame.model_copy(
                    update={
                        "inspection": cap.frame.inspection.model_copy(update={"gps_label": label})
                    }
                ),
                cap.hierarchy,
                cap.screenshot,
                cap.android_observed_ns,
                cap.motion_snapshot,
            )
        return cap

    def input_start(ns):
        assert observed == ["GPS信号弱", "GPS信号弱", "GPS信号优"]
        start(ns)

    r.refresh_started_gps = refresh
    monkeypatch.setattr(c, "capture", transient)
    monkeypatch.setattr(m, "start", input_start)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "finish_ready" and c.calls.count("begin") == 1
    assert refreshes == ["same_anchor"] * 2


def test_post_start_missing_reception_expires_without_motion_route_or_repeat_start(
    tmp_path, monkeypatch
):
    r, _b, c, d, m = setup(tmp_path)
    capture = c.capture
    refreshes = []

    def refresh(_proof, authorize):
        assert authorize() is True and not m.started
        refreshes.append(True)

    def stale(**kwargs):
        cap = capture(**kwargs)
        if c.last_action_request is not None:
            cap.motion_snapshot["gps_reception"]["last_received_ns"] = 1_000_000_000
        return cap

    r.refresh_started_gps, r.sleep = refresh, lambda _: None
    monkeypatch.setattr(c, "capture", stale)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and result["run_started"]
    assert result["message"] == "新开始已确认，定位接续超时；输入已停止并保留记录"
    assert not m.started and "update" not in d.calls and c.calls.count("begin") == 1
    assert len(refreshes) <= 101 and result["app_pause_confirmed"]


@pytest.mark.parametrize(
    "change", [{"distance_m": 20}, {"elapsed_s": 30}, {"gps_label": "UNKNOWN"}]
)
def test_bootstrap_never_waits_through_changed_session_or_unknown_gps(
    tmp_path, monkeypatch, change
):
    r, _b, c, d, m = setup(tmp_path)
    capture = c.capture
    refreshes = []
    r.refresh_started_gps = lambda *_: refreshes.append(True)

    def changed(**kwargs):
        cap = capture(**kwargs)
        if c.last_action_request is not None:
            return RunCapture(
                cap.frame.model_copy(
                    update={"inspection": cap.frame.inspection.model_copy(update=change)}
                ),
                cap.hierarchy,
                cap.screenshot,
                cap.android_observed_ns,
                cap.motion_snapshot,
            )
        return cap

    monkeypatch.setattr(c, "capture", changed)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and not m.started and "update" not in d.calls
    assert refreshes == [] and c.calls.count("begin") == 1


def test_bootstrap_cancellation_cannot_start_motion_or_path(tmp_path, monkeypatch):
    import threading

    r, _b, c, d, m = setup(tmp_path)
    cancel = threading.Event()
    start = d.start

    def started(proof):
        start(proof)
        c.gps_invalid = True

    monkeypatch.setattr(d, "start", started)

    def refresh(_proof, authorize):
        assert authorize() is True
        cancel.set()

    r.refresh_started_gps = refresh
    result = r.run(lambda: True, lambda _: None, cancel=cancel)
    assert result["state"] == "paused" and not m.started and "update" not in d.calls


def test_first_valid_frame_routes_before_next_weak_gps_capture(tmp_path, monkeypatch):
    r, _b, c, d, m = setup(tmp_path)
    events = []
    original_capture = c.capture

    def capture(**kwargs):
        cap = original_capture(**kwargs)
        if kwargs.get("renew_motion"):
            events.append("next_capture_weak")
            cap = RunCapture(
                cap.frame.model_copy(
                    update={
                        "inspection": cap.frame.inspection.model_copy(
                            update={"gps_label": "GPS信号弱"}
                        )
                    }
                ),
                cap.hierarchy,
                cap.screenshot,
                cap.android_observed_ns,
                cap.motion_snapshot,
            )
        return cap

    def dispatch(frame):
        assert m.started
        good = frame.inspection.gps_label == "GPS信号优"
        events.append("first_frame_routed" if good else "weak_frame_paused")
        return Decision(action="base_route" if good else "pause", reason="Synthetic GPS transition")

    monkeypatch.setattr(c, "capture", capture)
    monkeypatch.setattr(d, "update", dispatch)
    result = r.run(lambda: True, lambda _: None)
    assert events == ["first_frame_routed", "next_capture_weak", "weak_frame_paused"]
    assert result["state"] == "paused" and result["records_submitted"] == 0


def test_post_start_stale_reception_cannot_arm_motion_or_prime_route(tmp_path, monkeypatch):
    r, _b, c, d, m = setup(tmp_path)
    capture = c.capture

    def stale_after_start(**kwargs):
        cap = capture(**kwargs)
        if c.last_action_request is not None:
            cap.motion_snapshot["gps_reception"]["last_received_ns"] = 1_000_000_000
        return cap

    monkeypatch.setattr(c, "capture", stale_after_start)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and result["run_started"]
    assert not m.started and "update" not in d.calls
    assert result["records_submitted"] == 0 and result["mode_restored_normal"]


@pytest.mark.parametrize("delta,accepted", [(0.97, True), (5.01, False)])
def test_same_anchor_bootstrap_keeps_dynamic_card_range_without_rewriting_frame(
    tmp_path, delta, accepted
):
    import threading

    from budao.target import CheckpointCard

    r, _, c, d, m = setup(tmp_path)
    old = CheckpointCard(number=1, state="assigned", name="测试点", distance_m=282.72)
    proof = witness().model_copy(
        update={
            "second_running": witness().second_running.model_copy(
                update={
                    "inspection": witness().second_running.inspection.model_copy(
                        update={"checkpoint_cards": (old,)}
                    )
                }
            )
        }
    )
    d.observer.confirmed_names = frozenset({"测试点"})
    capture = c.capture

    def changed(**kwargs):
        cap = capture(**kwargs)
        return RunCapture(
            cap.frame.model_copy(
                update={
                    "inspection": cap.frame.inspection.model_copy(
                        update={
                            "checkpoint_cards": (
                                old.model_copy(update={"distance_m": 282.72 + delta}),
                            )
                        }
                    )
                }
            ),
            cap.hierarchy,
            cap.screenshot,
            cap.android_observed_ns,
            cap.motion_snapshot,
        )

    c.capture = changed
    if accepted:
        actual = r._initial_motion_frame(proof, threading.Event(), lambda: True)
        assert actual.frame.inspection.checkpoint_cards[0].distance_m == 282.72 + delta
    else:
        with pytest.raises(ValueError, match="会话"):
            r._initial_motion_frame(proof, threading.Event(), lambda: True)
    assert not m.started and "update" not in d.calls


@pytest.mark.parametrize("current_range,accepted", [(124.97, True), (128, False)])
def test_resume_bootstrap_range_tolerance_cannot_accumulate_across_proof_phases(
    tmp_path, current_range, accepted
):
    import threading

    from test_resume import proof as resume_proof

    from budao.resume import ResumeWitness

    r, _, c, d, m = setup(tmp_path)
    data = resume_proof().model_dump()
    for key in ("saved_pause", "first_paused", "second_paused", "first_running", "second_running"):
        data[key]["inspection"]["checkpoint_cards"] = [
            {
                "number": 1,
                "state": "assigned",
                "name": "点4",
                "distance_m": 120 if key == "saved_pause" else 124,
            }
        ]
    p = ResumeWitness.model_validate(data)
    captured = c.capture()
    current = frame(24, assigned="点4", distance_m=60, elapsed=583)
    current = current.model_copy(
        update={
            "inspection": current.inspection.model_copy(
                update={
                    "checkpoint_cards": (
                        current.inspection.checkpoint_cards[0].model_copy(
                            update={"distance_m": current_range}
                        ),
                    )
                }
            )
        }
    )
    c.capture = lambda **_: RunCapture(
        current, captured.hierarchy, None, captured.android_observed_ns, captured.motion_snapshot
    )
    d.observer.confirmed_names = frozenset({"点4"})
    r.clock = lambda: 24.1
    if accepted:
        assert r._initial_motion_frame(p, threading.Event(), lambda: True).frame == current
    else:
        with pytest.raises(ValueError, match="会话"):
            r._initial_motion_frame(p, threading.Event(), lambda: True)
    assert not m.started and "update" not in d.calls


@pytest.mark.parametrize("cancelled", [False, True])
def test_input_start_failure_or_cancellation_cannot_prime_route(tmp_path, monkeypatch, cancelled):
    import threading

    r, _b, _c, d, m = setup(tmp_path)
    cancel = threading.Event()

    def start(_):
        if cancelled:
            cancel.set()
        else:
            raise TimeoutError("Synthetic unknown input START result")

    monkeypatch.setattr(m, "start", start)
    result = r.run(lambda: True, lambda _: None, cancel=cancel)
    assert "update" not in d.calls and result["records_submitted"] == 0
    assert result["mode_restored_normal"]


def test_missing_fresh_same_position_gps_cannot_click_start_or_open_injection(tmp_path):
    r, b, c, _d, _m = setup(tmp_path)
    c.gps_invalid = True
    r.sleep = lambda _: None
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and not result["start_action_attempted"]
    assert "begin" not in c.calls and not any("data_injection" in call for call in b.calls)


def test_callback_reception_while_ui_weak_waits_without_beginning(tmp_path, monkeypatch):
    r, b, c, _d, _m = setup(tmp_path)
    capture = c.capture
    r.sleep = lambda _: None

    def weak(**kwargs):
        cap = capture(**kwargs)
        return RunCapture(
            cap.frame.model_copy(
                update={
                    "inspection": cap.frame.inspection.model_copy(update={"gps_label": "GPS信号弱"})
                }
            ),
            cap.hierarchy,
            cap.screenshot,
            cap.android_observed_ns,
            cap.motion_snapshot,
        )

    monkeypatch.setattr(c, "capture", weak)
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and not result["start_action_attempted"]
    assert result["message"] == "定位就绪等待超时；未点击开始，未提交记录"
    assert "begin" not in c.calls and not any("data_injection" in call for call in b.calls)


def test_long_limit_cannot_bypass_clip_budget_without_900_second_window(tmp_path):
    r, b, c, d, m = setup(tmp_path)
    for rollover in (False, True):
        with pytest.raises(ValueError):
            SessionRunner(c, d, m, r.clip, maximum_s=1800, rollover_enabled=rollover)


def test_rollover_pauses_window_and_resumes_same_operation_without_second_begin(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    old, b, c, d, m = setup(tmp_path)
    source = replace(old.clip, rows=old.clip.rows[:-1] + ((150, *old.clip.rows[-1][1:]),))
    c.source = source
    m.replay_passes = 6
    capture = c.capture

    def cap(**kwargs):
        result = capture(**kwargs)
        result.motion_snapshot["raw_motion_probe"]["replay_passes"] = 6
        return result

    monkeypatch.setattr(c, "capture", cap)
    r = SessionRunner(
        c,
        d,
        m,
        source,
        maximum_s=1800,
        rollover_enabled=True,
        clock=lambda: c.clock[0],
        sleep=lambda _: None,
    )
    calls = []

    def dispatch(_):
        if not calls:
            calls.append("first")
            c.clock[0] += 601
            return Decision(action="base_route", reason="synthetic clock window")
        return Decision(action="finish_ready", reason="synthetic terminal state")

    monkeypatch.setattr(d, "update", dispatch)
    windows = []

    def rollover(cancel, authorize, **kwargs):
        windows.append(kwargs)
        c.clock[0] += 5
        return 5

    monkeypatch.setattr(r, "_human_wait", rollover)
    reports = []
    result = r.run(lambda: True, reports.append)
    assert windows == [{"pause_record": True}]
    assert result["state"] == "finish_ready" and result["records_submitted"] == 0
    assert c.calls.count("begin") == 1 and any(x["state"] == "recovering" for x in reports)


def test_map_recovery_failure_pauses_and_preserves_record_without_crashing(tmp_path, monkeypatch):
    from budao.target import CheckpointCard

    r, b, c, d, m = setup(tmp_path)
    card = CheckpointCard(number=1, name="未知点", state="assigned", distance_m=10)
    old_capture = c.capture

    def capture_with_unknown(**kwargs):
        res = old_capture(**kwargs)
        if kwargs.get("renew_motion"):
            res = RunCapture(
                res.frame.model_copy(
                    update={
                        "inspection": res.frame.inspection.model_copy(
                            update={"checkpoint_cards": (card,)}
                        )
                    }
                ),
                res.hierarchy,
                res.screenshot,
                res.android_observed_ns,
                res.motion_snapshot,
            )
        return res

    monkeypatch.setattr(c, "capture", capture_with_unknown)

    def failing_recovery(_authorize):
        raise ValueError("Simulated map recovery failure")

    r.resolve_checkpoint = failing_recovery
    reports = []
    result = r.run(lambda: True, reports.append)
    assert result["state"] == "paused"
    assert result["message"] == "地图恢复未通过；保留记录"
    assert result["mode_restored_normal"] and result["app_pause_confirmed"]
    assert any(x["state"] == "recovering" for x in reports)


def test_cancel_during_human_wait_pauses_with_stop_message(tmp_path, monkeypatch):
    import threading

    from budao.target import CheckpointCard

    r, b, c, d, m = setup(tmp_path)
    card = CheckpointCard(number=1, name="未知点", state="assigned", distance_m=10)
    old_capture = c.capture
    cancel = threading.Event()

    def capture_with_unknown(**kwargs):
        res = old_capture(**kwargs)
        if kwargs.get("renew_motion"):
            res = RunCapture(
                res.frame.model_copy(
                    update={
                        "inspection": res.frame.inspection.model_copy(
                            update={"checkpoint_cards": (card,)}
                        )
                    }
                ),
                res.hierarchy,
                res.screenshot,
                res.android_observed_ns,
                res.motion_snapshot,
            )
        return res

    monkeypatch.setattr(c, "capture", capture_with_unknown)

    def cancel_on_recovery(_authorize):
        cancel.set()
        raise ValueError("Cancelled during recovery")

    r.resolve_checkpoint = cancel_on_recovery
    reports = []
    result = r.run(lambda: True, reports.append, cancel=cancel)
    assert result["state"] == "paused"
    assert result["message"] == "收到停止请求或达到有界运行时限"
