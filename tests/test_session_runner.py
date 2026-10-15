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
    assert d.calls == ["start", "update", "stop"] and c.calls.count("begin") == 1
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


def test_denied_start_never_attempts_start_or_enters_injection(tmp_path):
    r, b, c, _d, m = setup(tmp_path)
    result = r.run(lambda: False, lambda _: None)
    assert not result["run_started"] and "begin" not in c.calls and "prepare" not in m.calls
    assert not any("data_injection" in call for call in b.calls)


def test_same_anchor_bootstrap_is_after_real_witness_and_mode_before_motion_start(tmp_path):
    r, b, c, d, m = setup(tmp_path)
    pulses = []

    def refresh(proof, authorize):
        assert proof == witness() and c.last_action_request == m.request
        assert d.started and b.mode == INJECTION and not m.started
        assert authorize() is True
        pulses.append("same_anchor_only")

    r.refresh_started_gps = refresh
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "finish_ready" and pulses == ["same_anchor_only"]


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
