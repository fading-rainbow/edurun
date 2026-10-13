import json
from types import SimpleNamespace

import pytest
from test_runtime import frame

from budao.foreground_motion import ForegroundMotionClient
from budao.models import GeoPoint
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

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if "authorize" in kwargs:
            assert kwargs["authorize"]() is True
        f = super().run(*args)
        if args == ("shell", CAPTURE_GUARD):
            return f + f"\n__EDURUN_PACKAGE__\nversionName={self.version}\nversionCode=230"
        if args == ("shell", CAPTURE_MOTION_TAIL) or "MOTION_RENEW" in args[-1]:
            if self.changed:
                f = "mCurrentFocus=Window{123 u0 com.tencent.mobileqq/.LoginActivity}"
            return (
                f
                + "\n__EDURUN_CLOCK__\n"
                + self.uptime
                + "\n__EDURUN_MOTION__\n"
                + json.dumps({"package": "com.edurun.diagnostic", "request_id": self.nonce})
                + "\n__EDURUN_AFTER_CLOCK__\n"
                + self.uptime
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


def test_merged_renewal_only_after_allowed_frame_and_device_guard():
    device = BatchDevice()
    connector = batch_connector(device)
    client = ForegroundMotionClient(device, "b" * 32)
    client.started = True
    # Synthetic control is not running/paused: authorization alone cannot make it eligible.
    connector.capture(
        motion_request=client.request, renew_motion=client, authorize_motion=lambda _: True
    )
    assert device.calls[-1] == ("shell", CAPTURE_MOTION_TAIL)
    from test_checkpoint_cards import row
    from test_target import hierarchy

    buttons = "".join(
        f'<node package="{TARGET}" resource-id="{TARGET}:id/{name}" '
        'class="android.widget.ProgressBar" enabled="true" visible-to-user="true"/>'
        for name in ("tv_start_pause_continue_run", "tv_stoprun")
    )
    connector.ui.device.dump_hierarchy = lambda: hierarchy(
        tv_gps_strength="GPS信号优", chronometer="00:01:00"
    ).replace("</hierarchy>", row("待分配(再跑123米后分配打卡点)") + buttons + "</hierarchy>")
    result = connector.capture(
        motion_request=client.request, renew_motion=client, authorize_motion=lambda _: True
    )
    assert result.frame.inspection.run_control_state == "paused"
    assert "MOTION_RENEW" in device.calls[-1][-1]
    assert "grep -c" in device.calls[-1][-1] and "motion_lease_ms 2000" in device.calls[-1][-1]
    connector.capture(
        motion_request=client.request, renew_motion=client, authorize_motion=lambda _: False
    )
    assert device.calls[-1] == ("shell", CAPTURE_MOTION_TAIL)


def test_merged_renewal_cannot_use_different_device_nonce_or_slow_pixels():
    device = BatchDevice()
    connector = batch_connector(device)
    client = ForegroundMotionClient(device, "b" * 32)
    client.started = True
    for args in ({"motion_request": "c" * 32}, {"motion_request": client.request, "pixels": True}):
        with pytest.raises(ValueError, match="同一设备"):
            connector.capture(**args, renew_motion=client, authorize_motion=lambda _: True)
    assert not device.calls


@pytest.mark.parametrize("overlay", ["ad", "face"])
def test_merged_capture_never_renews_through_ad_or_human_verification(overlay):
    from test_target import hierarchy

    device = BatchDevice()
    connector = batch_connector(device)
    client = ForegroundMotionClient(device, "b" * 32)
    client.started = True
    fields = (
        {"ad_close": "关闭"}
        if overlay == "ad"
        else {
            "alert_dialog_content": "本次跑步需要进行人脸验证\n验证成功后可继续跑步",
            "dialog_confirm": "去验证",
        }
    )
    connector.ui.device.dump_hierarchy = lambda: hierarchy(tv_gps_strength="GPS信号优", **fields)
    result = connector.capture(
        motion_request=client.request,
        renew_motion=client,
        authorize_motion=lambda _: pytest.fail("Unknown UI must not authorize motion"),
    )
    assert device.calls[-1] == ("shell", CAPTURE_MOTION_TAIL)
    assert result.frame.inspection.run_control_state == "unknown"


def test_weak_ui_gps_requires_explicit_native_reception_guard_not_fake_strong_label():
    from test_checkpoint_cards import row
    from test_target import hierarchy

    device = BatchDevice()
    connector = batch_connector(device)
    client = ForegroundMotionClient(device, "b" * 32)
    client.started = True
    button = (
        f'<node package="{TARGET}" resource-id="{TARGET}:id/tv_start_pause_continue_run" '
        'class="android.widget.ProgressBar" enabled="true" visible-to-user="true"/>'
    )
    connector.ui.device.dump_hierarchy = lambda: hierarchy(
        tv_gps_strength="GPS信号弱", chronometer="00:01:00"
    ).replace("</hierarchy>", row("待分配(再跑123米后分配打卡点)") + button + "</hierarchy>")
    arguments = dict(
        motion_request=client.request, renew_motion=client, authorize_motion=lambda _: True
    )
    connector.capture(**arguments)
    assert device.calls[-1] == ("shell", CAPTURE_MOTION_TAIL)
    result = connector.capture(**arguments, gps_position=GeoPoint(lon=120, lat=30))
    assert result.frame.inspection.gps_label == "GPS信号弱"
    command = device.calls[-1][-1]
    assert "motion_require_received_gps true" in command and "motion_expected_lat 30.0" in command
    assert "motion_lease_ms 2000" in command
    # The command requires native proof; its ACK is not that proof or a movement permission.
    assert "gps_reception" not in result.motion_snapshot
    with pytest.raises(ValueError, match="接收定位"):
        connector.capture(gps_position=GeoPoint(lon=120, lat=30))


@pytest.mark.parametrize("after", ["12.33 8.00", "14.35 8.00", "unknown"])
def test_observation_end_clock_rejects_backward_slow_or_unknown_read(after):
    class EndClockDevice(BatchDevice):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            if "__EDURUN_AFTER_CLOCK__" in result:
                return (
                    result.split("__EDURUN_AFTER_CLOCK__")[0] + "__EDURUN_AFTER_CLOCK__\n" + after
                )
            return result

    with pytest.raises(ValueError, match="结束时钟"):
        batch_connector(EndClockDevice()).capture(motion_request="b" * 32)
