"""就绪交集的合成回归；无ADB、真实位置输出或成绩。"""

from copy import deepcopy

import pytest
from test_gps_reception import NOW, POSITION, packet
from test_runtime import frame
from test_start_connector import setup

from budao.start_connector import RunCapture

REQUEST = "a" * 32


def readiness(monkeypatch, observations):
    connector, clock, clicked, _ = setup(monkeypatch)
    clock[0] = 0
    calls = []
    values = iter(observations)

    def capture(**kwargs):
        calls.append(kwargs)
        label, received = next(values)
        clock[0] += 0.5
        view = frame(clock[0], control="not_started_or_countdown", elapsed=0)
        view = view.model_copy(
            update={"inspection": view.inspection.model_copy(update={"gps_label": label})}
        )
        snapshot = packet()
        snapshot["request_id"] = REQUEST
        if not received:
            snapshot["gps_reception"]["last_received_ns"] = 1_500_000_000
        return RunCapture(view, "<hierarchy/>", object(), NOW, snapshot)

    monkeypatch.setattr(connector, "capture", capture)
    connector.sleep = lambda value: clock.__setitem__(0, clock[0] + value)
    return connector, clock, clicked, calls


def test_callback_only_or_good_label_only_cannot_complete_preparation(monkeypatch):
    c, _, clicked, captures = readiness(
        monkeypatch,
        [
            ("GPS信号弱", True),
            ("GPS信号优", False),
            ("GPS信号优", True),
        ],
    )
    refreshes = []

    def refresh(authorize):
        assert authorize() is True
        refreshes.append("same_position_only")

    cap = c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION, refresh_gps=refresh)
    assert cap.frame.inspection.gps_label == "GPS信号优"
    assert len(captures) == len(refreshes) == 3
    assert all(v == {"pixels": True, "motion_request": REQUEST} for v in captures)
    assert not clicked and not c.used_requests and c.last_action_request is None


@pytest.mark.parametrize("label,received", [("GPS信号弱", True), ("GPS信号优", False)])
def test_bounded_wait_never_turns_missing_readiness_into_a_start(monkeypatch, label, received):
    c, clock, clicked, _ = readiness(monkeypatch, [(label, received)] * 30)
    with pytest.raises(ValueError, match="未同时确认"):
        c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION)
    assert 10 <= clock[0] < 11 and not clicked and c.last_action_request is None


def test_wait_stops_on_revoked_permission_before_further_capture(monkeypatch):
    c, _, clicked, captures = readiness(monkeypatch, [("GPS信号弱", True)] * 3)
    with pytest.raises(ValueError, match="尚未确认"):
        c.prepare_begin(REQUEST, lambda: len(captures) < 1, gps_position=POSITION)
    assert len(captures) == 1 and not clicked


@pytest.mark.parametrize("invalid", ["GPS未知", None])
def test_unknown_label_is_not_a_transient_ready_state(monkeypatch, invalid):
    c, _, clicked, captures = readiness(monkeypatch, [(invalid, True)])
    with pytest.raises(ValueError, match="定位状态未知"):
        c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION)
    assert len(captures) == 1 and not clicked


def test_changed_pixels_or_protected_capture_fails_without_waiting_or_clicking(monkeypatch):
    c, _, clicked, captures = readiness(monkeypatch, [("GPS信号弱", True)])
    c.template.matches = lambda *_: False
    with pytest.raises(ValueError, match="未点击"):
        c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION)
    assert len(captures) == 1 and not clicked
    monkeypatch.setattr(c, "capture", lambda **_: (_ for _ in ()).throw(ValueError("protected")))
    with pytest.raises(ValueError, match="protected"):
        c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION)
    assert not clicked


def test_same_capture_packet_not_changed_to_make_it_match(monkeypatch):
    c, _, clicked, _ = readiness(monkeypatch, [("GPS信号优", True)])
    original = c.capture(pixels=True, motion_request=REQUEST)
    original.motion_snapshot["request_id"] = "b" * 32
    saved = deepcopy(original.motion_snapshot)

    def capture(**_):
        # Advance only the synthetic host clock, not received data or Android clocks.
        c.sleep(1)
        return original

    monkeypatch.setattr(c, "capture", capture)
    with pytest.raises(ValueError, match="未同时确认"):
        c.prepare_begin(REQUEST, lambda: True, gps_position=POSITION)
    assert original.motion_snapshot == saved and not clicked


def test_new_preparation_context_cannot_supply_commands_or_unbound_refresh(monkeypatch):
    c, _, clicked, captures = readiness(monkeypatch, [])
    for extra in (
        {"gps_position": {}},
        {"refresh_gps": lambda _: None},
        {"gps_position": POSITION, "refresh_gps": "command"},
    ):
        with pytest.raises(ValueError, match="明确任务"):
            c.prepare_begin(REQUEST, lambda: True, **extra)
    assert not captures and not clicked
