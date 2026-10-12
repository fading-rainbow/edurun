from types import SimpleNamespace

import pytest
from test_runtime import frame

from budao.mumu import MuMuBridge
from budao.start_connector import RunCapture, StartConnector
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
