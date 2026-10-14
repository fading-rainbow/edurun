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
            bridge=bridge, thread=None, engine=SimpleNamespace(position=GeoPoint(lon=120, lat=30))
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

    def begin_once(self, request, authorize):
        assert authorize() is True
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


def test_missing_fresh_same_position_gps_cannot_click_start_or_open_injection(tmp_path):
    r, b, c, _d, _m = setup(tmp_path)
    c.gps_invalid = True
    r.sleep = lambda _: None
    result = r.run(lambda: True, lambda _: None)
    assert result["state"] == "error" and not result["start_action_attempted"]
    assert "begin" not in c.calls and not any("data_injection" in call for call in b.calls)
