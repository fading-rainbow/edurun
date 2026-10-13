import json
from types import SimpleNamespace

import pytest
from test_runtime import frame

from budao.mumu import MuMuBridge
from budao.start_connector import CAPTURE_GUARD, CAPTURE_MOTION_TAIL, RunCapture, StartConnector
from budao.target import RUN_ACTIVITY, TARGET


class Device(MuMuBridge):
    serial = "127.0.0.1:16416"

    def __init__(self):
        self.focus = (TARGET, RUN_ACTIVITY)

    def check_device(self):
        pass

    def run(self, *args):
        return f"mCurrentFocus=Window{{123 u0 {self.focus[0]}/{self.focus[1]}}}"


def setup(monkeypatch):
    clock = [0.5]
    clicked = []
    device = Device()
    template = SimpleNamespace(matches=lambda *_: True)
    connector = StartConnector(device, template, clock=lambda: clock[0], sleep=lambda _: None)
    button = SimpleNamespace(count=1, click=lambda: clicked.append("start"))
    connector.ui = SimpleNamespace(device=lambda **_: button)
    frames = iter(
        [
            frame(0, control="not_started_or_countdown", elapsed=0),
            frame(1, elapsed=1),
            frame(2, elapsed=2),
        ]
    )

    def capture(**kwargs):
        value = next(frames)
        if value.sampled_at:
            clock[0] = value.sampled_at
        return RunCapture(value, "<hierarchy/>", object() if kwargs.get("pixels") else None)

    monkeypatch.setattr(connector, "capture", capture)
    return connector, clock, clicked, device


def test_one_click_creates_causal_three_frame_witness_not_record_or_movement(monkeypatch):
    connector, _, clicked, _ = setup(monkeypatch)
    witness = connector.begin_once("a" * 32, lambda: True)
    assert clicked == ["start"]
    assert (
        witness.before.sampled_at
        < witness.action_at
        < witness.first_running.sampled_at
        < witness.second_running.sampled_at
    )
    assert witness.start_ui_verified and witness.serial == "127.0.0.1:16416"
    with pytest.raises(ValueError, match="消费"):
        connector.begin_once("a" * 32, lambda: True)


def test_not_authorized_or_stale_does_not_click(monkeypatch):
    connector, _, clicked, _ = setup(monkeypatch)
    with pytest.raises(ValueError, match="尚未确认"):
        connector.begin_once("a" * 32, lambda: False)
    assert not clicked
    connector, clock, clicked, _ = setup(monkeypatch)
    clock[0] = 3
    with pytest.raises(ValueError, match="过期"):
        connector.begin_once("b" * 32, lambda: True)
    assert not clicked


def test_template_or_foreground_change_blocks_and_does_not_retry(monkeypatch):
    connector, _, clicked, _ = setup(monkeypatch)
    connector.template.matches = lambda *_: False
    with pytest.raises(ValueError, match="未点击"):
        connector.begin_once("a" * 32, lambda: True)
    assert not clicked
    connector, _, clicked, device = setup(monkeypatch)
    device.focus = ("com.tencent.mobileqq", "com.tencent.mobileqq.LoginActivity")
    with pytest.raises(ValueError, match="前台"):
        connector.begin_once("a" * 32, lambda: True)
    assert not clicked


def test_capture_refuses_protected_page_before_constructing_ui():
    device = Device()
    device.focus = ("com.tencent.mobileqq", "com.tencent.mobileqq.LoginActivity")
    connector = StartConnector(
        device, object(), ui_factory=lambda _: pytest.fail("protected UI read")
    )
    with pytest.raises(ValueError, match="不读取"):
        connector.capture(pixels=True)


class BatchDevice(Device):
    def __init__(self, *, version="4.3.0", uptime="12.34 8.00", changed=False, nonce="b" * 32):
        super().__init__()
        self.version, self.uptime, self.changed, self.nonce = version, uptime, changed, nonce
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        f = super().run(*args)
        if args == ("shell", CAPTURE_GUARD):
            return f + f"\n__EDURUN_PACKAGE__\nversionName={self.version}\nversionCode=230"
        if args == ("shell", CAPTURE_MOTION_TAIL):
            if self.changed:
                f = "mCurrentFocus=Window{123 u0 com.tencent.mobileqq/.LoginActivity}"
            return (
                f
                + "\n__EDURUN_CLOCK__\n"
                + self.uptime
                + "\n__EDURUN_MOTION__\n"
                + json.dumps({"package": "com.edurun.diagnostic", "request_id": self.nonce})
            )
        return f


def batch_connector(device):
    from test_target import hierarchy

    connector = StartConnector(device, object())
    connector.ui = SimpleNamespace(
        device=SimpleNamespace(dump_hierarchy=lambda: hierarchy(tv_gps_strength="GPS信号优"))
    )
    return connector


def test_shared_capture_keeps_version_focus_checks_and_exact_android_clock():
    device = BatchDevice()
    result = batch_connector(device).capture(motion_request="b" * 32)
    assert result.android_observed_ns == 12_340_000_000
    assert result.motion_snapshot["request_id"] == "b" * 32
    assert device.calls == [("shell", CAPTURE_GUARD), ("shell", CAPTURE_MOTION_TAIL)]
    assert result.frame.inspection.gps_label == "GPS信号优"


def test_shared_capture_ignores_old_motion_nonce_without_inventing_current_state():
    result = batch_connector(BatchDevice(nonce="c" * 32)).capture(motion_request="b" * 32)
    assert result.motion_snapshot is None and result.android_observed_ns == 12_340_000_000


@pytest.mark.parametrize("value", ["-1.00 3.00", "NaN 3.00", "1.23", "1.234 2.000"])
def test_shared_capture_rejects_unknown_android_clock(value):
    with pytest.raises(ValueError, match="单调时钟"):
        batch_connector(BatchDevice(uptime=value)).capture(motion_request="b" * 32)


def test_shared_capture_foreground_change_and_version_still_fail_closed():
    with pytest.raises(ValueError, match="前台"):
        batch_connector(BatchDevice(changed=True)).capture(motion_request="b" * 32)
    with pytest.raises(ValueError, match="版本"):
        batch_connector(BatchDevice(version="other")).capture(motion_request="b" * 32)


@pytest.mark.parametrize("value", [123, "shell; secret", "f" * 31])
def test_invalid_motion_task_does_not_reach_device(value):
    device = BatchDevice()
    with pytest.raises(ValueError, match="任务标记"):
        batch_connector(device).capture(motion_request=value)
    assert not device.calls
