"""现场辅助权限提示的合成回归，不连接设备或生成跑步记录。"""

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from test_portal import UI, Bridge

from budao.auth_ui import MAIN
from budao.portal import _location_permissions_ready, _optional_permission_tip, enter_formal_zero
from budao.target import RUN_ACTIVITY, TARGET

PERMISSIONS = "\n".join(
    f"android.permission.{name}: granted=true, flags=[]"
    for name in ("ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION")
)
OPS = "FINE_LOCATION: allow; time=+1s ago\nCOARSE_LOCATION: allow; time=+1s ago"


def tip_tree(**replacements):
    texts = {
        "tv_version_info": "必要权限未开启！",
        "tv_content": "请先开启必要权限，以便于稳定、准确地记录您的运动轨迹！",
        "tv_but2": "立即开启",
        "tv_button": "暂不开启继续跑步",
    } | replacements
    root = ET.Element("hierarchy")
    for name, text in texts.items():
        ET.SubElement(
            root,
            "node",
            {
                "package": TARGET,
                "resource-id": TARGET + ":id/" + name,
                "text": text,
                "visible-to-user": "true",
            },
        )
    return ET.tostring(root, encoding="unicode")


def confirmation_tree(**replacements):
    return tip_tree(
        **(
            {
                "tv_version_info": "重要提示!",
                "tv_content": (
                    "当前必要权限不完整，可能无法稳定、准确记录跑步轨迹，"
                    "可能会影响您的跑步记录。您仍可以坚持跑步，但建议保持程序前台运行，"
                    "保持屏幕常亮，以便记录成绩。\n"
                ),
                "tv_but2": "暂不开启，继续跑步",
                "tv_button": "去开启",
            }
            | replacements
        )
    )


class PermissionBridge(Bridge):
    def __init__(self):
        super().__init__()
        self.meta, self.ops, self.reads = PERMISSIONS, OPS, []

    def run(self, *args, **kwargs):
        if args == ("shell", "dumpsys", "package", TARGET):
            self.reads.append("permissions")
            return self.meta
        if args == ("shell", "cmd", "appops", "get", TARGET):
            self.reads.append("appops")
            return self.ops
        return super().run(*args, **kwargs)


class PermissionUI(UI):
    repeat = False
    replacements = {}

    def __call__(self, **kwargs):
        name = kwargs.get("resourceId", "").split("/")[-1]
        result = super().__call__(**kwargs)
        if name == "tv_enterRun":

            def enter():
                self.bridge.clicks.append(name)
                self.bridge.phase, self.bridge.focus = "permission", MAIN

            result.click = enter
        if name == "tv_button" and self.bridge.phase == "permission":

            def skip():
                self.bridge.clicks.append(name)
                if not self.repeat:
                    self.bridge.phase, self.bridge.focus = "rules", RUN_ACTIVITY

            return SimpleNamespace(count=1, click=skip)
        return result

    def dump_hierarchy(self):
        if self.bridge.phase == "permission":
            return tip_tree(**self.replacements)
        return super().dump_hierarchy()


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


def test_observed_tip_continues_once_with_existing_location_grants_without_other_permissions():
    b, events = PermissionBridge(), []
    assert (
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=PermissionUI,
            sleep=lambda _: None,
            on_progress=events.append,
        )
        == 1
    )
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_button", "tv_cancle"]
    assert b.reads == ["permissions", "appops"]
    assert any(e["event"] == "optional_permission_tip_continued" for e in events)
    assert "tv_but2" not in b.clicks and "tv_start_pause_continue_run" not in b.clicks


@pytest.mark.parametrize(
    "change",
    ["denied", "duplicate", "appop_denied", "appop_missing", "uid_denied"],
)
def test_missing_or_denied_location_does_not_skip_tip_or_grant_it(change):
    b = PermissionBridge()
    if change == "denied":
        b.meta = PERMISSIONS.replace(
            "ACCESS_BACKGROUND_LOCATION: granted=true", "ACCESS_BACKGROUND_LOCATION: granted=false"
        )
    elif change == "duplicate":
        b.meta += "\nandroid.permission.ACCESS_FINE_LOCATION: granted=true"
    elif change == "appop_denied":
        b.ops = OPS.replace("FINE_LOCATION: allow", "FINE_LOCATION: ignore")
    elif change == "appop_missing":
        b.ops = ""
    else:
        b.ops = "Uid mode: FINE_LOCATION: ignore\n" + OPS
    assert not _location_permissions_ready(b)
    with pytest.raises(ValueError, match="运动定位权限"):
        enter_formal_zero(b, lambda: True, ui_factory=PermissionUI, sleep=lambda _: None)
    assert b.clicks == ["tv_run", "tv_enterRun"]


@pytest.mark.parametrize(
    "replacement",
    [
        {"tv_content": "PRIVATE_AUTH_MESSAGE"},
        {"tv_button": "开始跑步"},
        {"dialog_confirm": "去验证"},
        {"alert_dialog_content": "PRIVATE_AUTH_MESSAGE"},
    ],
)
def test_only_complete_known_permission_advice_is_skipped(replacement):
    assert not _optional_permission_tip(tip_tree(**replacement))

    class ChangedUI(PermissionUI):
        replacements = replacement

    b = PermissionBridge()
    with pytest.raises(ValueError, match="权限提示未确认") as error:
        enter_formal_zero(b, lambda: True, ui_factory=ChangedUI, sleep=lambda _: None)
    assert b.clicks == ["tv_run", "tv_enterRun"]
    assert "PRIVATE_AUTH_MESSAGE" not in str(error.value)


def test_repeat_tip_does_not_repeat_continue_action():
    class RepeatingUI(PermissionUI):
        repeat = True

    b = PermissionBridge()
    with pytest.raises(ValueError, match="权限提示未确认"):
        enter_formal_zero(b, lambda: True, ui_factory=RepeatingUI, sleep=lambda _: None)
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_button"]


@pytest.mark.parametrize("protected", ["password", "other_package"])
def test_known_tip_with_protected_foreground_nodes_is_rejected(protected):
    root = ET.fromstring(tip_tree())
    ET.SubElement(
        root,
        "node",
        {
            "visible-to-user": "true",
            "package": "com.android.permissioncontroller"
            if protected == "other_package"
            else TARGET,
            "password": "true" if protected == "password" else "false",
        },
    )
    assert not _optional_permission_tip(ET.tostring(root, encoding="unicode"))


def test_tip_not_caused_by_this_entry_does_not_advance_other_workflow():
    b = PermissionBridge()
    b.phase = "permission"
    with pytest.raises(ValueError, match="权限提示未确认"):
        enter_formal_zero(b, lambda: True, ui_factory=PermissionUI, sleep=lambda _: None)
    assert not b.clicks and not b.reads


def test_second_observed_confirmation_is_distinct_and_each_stage_is_clicked_once():
    class TwoStageUI(PermissionUI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            name = kwargs.get("resourceId", "").split("/")[-1]
            if name == "tv_button" and self.bridge.phase == "permission":

                def skip():
                    self.bridge.clicks.append(name)
                    self.bridge.phase = "confirmation"

                result.click = skip
            elif name == "tv_but2" and self.bridge.phase == "confirmation":

                def confirm():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = "rules", RUN_ACTIVITY

                result = SimpleNamespace(count=1, click=confirm)
            return result

        def dump_hierarchy(self):
            if self.bridge.phase == "confirmation":
                return confirmation_tree()
            return super().dump_hierarchy()

    b, events = PermissionBridge(), []
    assert _optional_permission_tip(confirmation_tree(), confirmation=True)
    assert not _optional_permission_tip(confirmation_tree())
    assert (
        enter_formal_zero(
            b, lambda: True, ui_factory=TwoStageUI, sleep=lambda _: None, on_progress=events.append
        )
        == 1
    )
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_button", "tv_but2", "tv_cancle"]
    assert b.reads == ["permissions", "appops"] * 2
    assert any(e["event"] == "optional_permission_confirmation_continued" for e in events)


def test_unbound_confirmation_cannot_skip_first_stage():
    class UnboundUI(UI):
        def dump_hierarchy(self):
            return confirmation_tree()

    b = PermissionBridge()
    with pytest.raises(ValueError, match="权限提示未确认"):
        enter_formal_zero(b, lambda: True, ui_factory=UnboundUI, sleep=lambda _: None)
    assert not b.clicks and not b.reads
