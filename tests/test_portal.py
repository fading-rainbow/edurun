"""合成入口协议测试，不连接App或声明现场验收。"""

from types import SimpleNamespace

import pytest

from budao.auth_ui import MAIN
from budao.mumu import MuMuBridge
from budao.portal import BEFORE, enter_formal_zero
from budao.target import RUN_ACTIVITY, TARGET


class Bridge(MuMuBridge):
    def __init__(self):
        self.focus, self.phase, self.clicks = MAIN, "main", []

    def check_device(self):
        pass

    def run(self, *_args, **_kwargs):
        return f"mCurrentFocus=Window{{1 u0 {TARGET}/{self.focus}}}"


class UI:
    def __init__(self, bridge):
        self.bridge, self.device = bridge, self

    def __call__(self, **kwargs):
        name = kwargs.get("resourceId", "").split("/")[-1]
        count = 0
        if self.bridge.phase == "main" and name in {"tv_run", "tv_enterRun", "tv_run_data"}:
            count = 1
        elif self.bridge.phase == "rules" and name in {
            "tv_title",
            "tv_cancle",
            "tv_standard_distance_value",
            "tv_standard_log_value",
            "tv_standard_speed_value",
        }:
            count = 1

        def click():
            self.bridge.clicks.append(name)
            if name == "tv_enterRun":
                self.bridge.phase, self.bridge.focus = "rules", BEFORE
            if name == "tv_cancle":
                self.bridge.phase, self.bridge.focus = "zero", RUN_ACTIVITY

        return SimpleNamespace(count=count, click=click, get_text=lambda: "1")

    def dump_hierarchy(self):
        return "<hierarchy/>"


def test_single_portal_clicks_rules_then_returns_zero_not_start(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )
    bridge = Bridge()
    enter_formal_zero(bridge, lambda: True, ui_factory=UI, sleep=lambda _: None)
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]


def test_existing_run_and_no_authorization_do_not_navigate():
    bridge = Bridge()
    bridge.focus = RUN_ACTIVITY
    with pytest.raises(ValueError):
        enter_formal_zero(bridge, lambda: True, ui_factory=UI)
    assert not bridge.clicks
    bridge.focus = MAIN
    with pytest.raises(ValueError):
        enter_formal_zero(bridge, lambda: False, ui_factory=UI)
    assert not bridge.clicks


def test_nonzero_page_not_treated_as_new_session(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 1500,
            "elapsed_s": 650,
            "completed_checkpoints": 3,
            "counter_label": "已打卡",
        },
    )
    with pytest.raises(ValueError, match="零状态"):
        enter_formal_zero(Bridge(), lambda: True, ui_factory=UI, sleep=lambda _: None)


def test_slow_grade_or_interstitial_waits_without_second_tab_or_start(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )

    class DelayedUI(UI):
        grade_reads = 0

        def __call__(self, **kwargs):
            control = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_run_data":
                self.grade_reads += 1
                if self.grade_reads <= 3:
                    control.count = 0
            return control

    bridge = Bridge()
    assert enter_formal_zero(bridge, lambda: True, ui_factory=DelayedUI, sleep=lambda _: None) == 1
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]


def test_safety_entry_is_single_and_requires_exact_zone(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )

    class SafetyUI(UI):
        def __call__(self, **kwargs):
            node = super().__call__(**kwargs)
            name = kwargs.get("resourceId", "").split("/")[-1]
            if self.bridge.phase == "safety":
                node.count = int(
                    name in {"tv_title", "tv_sure"}
                    or (
                        name == "tv_current_run_zone" and kwargs.get("text") == "当前跑区：测试跑区"
                    )
                )

                def click():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = "rules", BEFORE

                node.click = click
            elif name == "tv_enterRun":

                def entry():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = "safety", MAIN

                node.click = entry
            return node

    bridge = Bridge()
    assert (
        enter_formal_zero(
            bridge,
            lambda: True,
            ui_factory=SafetyUI,
            sleep=lambda _: None,
            expected_zone="当前跑区：测试跑区",
        )
        == 1
    )
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_sure", "tv_cancle"]
    with pytest.raises(ValueError, match="跑区未核对"):
        enter_formal_zero(
            Bridge(),
            lambda: True,
            ui_factory=SafetyUI,
            sleep=lambda _: None,
            expected_zone="当前跑区：其他跑区",
        )
