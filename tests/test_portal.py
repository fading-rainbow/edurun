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
