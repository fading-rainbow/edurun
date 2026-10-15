"""成组回放正常异步切页与动作前竞态；不连接设备、不生成记录。"""

from types import SimpleNamespace

import pytest
from test_portal import UI, Bridge

from budao.auth_ui import MAIN, SPLASH
from budao.portal import BEFORE, enter_formal_zero
from budao.target import RUN_ACTIVITY, TARGET


@pytest.fixture(autouse=True)
def zero_metrics(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )


@pytest.mark.parametrize("when", ["hierarchy", "selector"])
@pytest.mark.parametrize(
    "destination,allowed",
    [
        ((TARGET, RUN_ACTIVITY), True),
        ((TARGET, SPLASH), True),
        (None, True),
        (("com.tencent.mobileqq", "LoginActivity"), False),
        ((TARGET, TARGET + ".CameraActivity"), False),
        (("com.android.permissioncontroller", "PermissionActivity"), False),
    ],
)
def test_normal_transition_discards_old_observation_but_protected_focus_stops(
    when, destination, allowed
):
    unset = object()

    class ChangingBridge(Bridge):
        override = unset

        def run(self, *_args):
            if self.override is unset:
                return super().run(*_args)
            if self.override is None:
                return "mCurrentFocus=null"
            package, activity = self.override
            return f"mCurrentFocus=Window{{a u0 {package}/{activity}}}"

    b = ChangingBridge()
    changed = [False]
    events = []

    def transition():
        changed[0] = True
        b.phase, b.focus = "rules", BEFORE
        b.override = destination

    class ChangingUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            name = kwargs.get("resourceId", "").split("/")[-1]
            if when == "hierarchy" and name == "tv_enterRun":
                original = result.click

                def entry():
                    original()
                    b.phase, b.focus = "pending_capture", MAIN

                result.click = entry
            if when == "selector" and name == "tv_cancle" and not changed[0]:
                transition()
            return result

        def dump_hierarchy(self):
            tree = super().dump_hierarchy()
            if when == "hierarchy" and b.phase == "pending_capture" and not changed[0]:
                transition()
            return tree

    def sleep(_):
        if allowed:
            b.override = unset

    def run():
        return enter_formal_zero(
            b, lambda: True, ui_factory=ChangingUI, sleep=sleep, on_progress=events.append
        )

    if allowed:
        assert run() == 1
        assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]
        assert any(e["event"] == "resample_normal_transition" for e in events)
    else:
        with pytest.raises(ValueError, match="认证|未知前台"):
            run()
        assert b.clicks == ["tv_run", "tv_enterRun"]
    assert "tv_start_pause_continue_run" not in b.clicks


@pytest.mark.parametrize("first_count", [0, 2])
def test_missing_selector_is_resampled_but_duplicate_is_never_clicked(first_count):
    missing = [True]

    class SelectorUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun" and missing[0]:
                missing[0] = False
                result.count = first_count
            return result

    b = Bridge()
    if first_count == 0:
        assert enter_formal_zero(b, lambda: True, ui_factory=SelectorUI, sleep=lambda _: None) == 1
        assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]
    else:
        with pytest.raises(ValueError, match="不唯一"):
            enter_formal_zero(b, lambda: True, ui_factory=SelectorUI, sleep=lambda _: None)
        assert b.clicks == ["tv_run"]


def test_uncertain_action_ack_is_not_retried():
    class TimeoutUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun":

                def uncertain():
                    self.bridge.clicks.append("tv_enterRun")
                    raise TimeoutError("ACK unknown")

                result.click = uncertain
            return result

    b = Bridge()
    with pytest.raises(TimeoutError):
        enter_formal_zero(b, lambda: True, ui_factory=TimeoutUI, sleep=lambda _: None)
    assert b.clicks == ["tv_run", "tv_enterRun"]


def test_no_action_rules_observation_can_arrive_later_without_repeating_request():
    b = Bridge()
    requests = []
    waiting = [0]

    class WaitingUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun":
                original = result.click

                def entry():
                    original()
                    b.phase, b.focus = "waiting_rules", RUN_ACTIVITY

                result.click = entry
            return result

    def rules(*_):
        requests.append("observed_no_action")
        return False

    def sleep(_):
        if requests and b.phase == "waiting_rules":
            waiting[0] += 1
            if waiting[0] >= 3:
                b.phase = "rules"

    helper = SimpleNamespace(open_rules=rules, opaque_overlay=lambda _: False)
    assert (
        enter_formal_zero(b, lambda: True, ui_factory=WaitingUI, sleep=sleep, scene_helper=helper)
        == 1
    )
    assert requests == ["observed_no_action"]
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]
