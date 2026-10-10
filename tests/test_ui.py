from types import SimpleNamespace

import pytest

from budao.ui import AppUI, focused_package


@pytest.mark.parametrize("activity", [".MainActivity", "com.target.MainActivity", ".Main$Child"])
def test_android16_displays_current_focus(activity):
    dump = f"  mCurrentFocus=Window{{64d35e5 u0 com.target/{activity}}}\n"
    dump += "  mFocusedApp=ActivityRecord{other u0 com.background/.WelcomeActivity t9}\n"
    assert focused_package(dump) == "com.target"


@pytest.mark.parametrize(
    "dump",
    [
        "",
        "mCurrentFocus=null",
        "mCurrentFocus=Window{abc u0 NotificationShade}",
        "ACTIVITY com.background/.WelcomeActivity abc pid=1",
        "mFocusedApp=ActivityRecord{abc u0 com.target/.MainActivity t1}",
        "mCurrentFocus=Window{abc u0 com.target/.MainActivity} trailing-data",
        "mCurrentFocus=Window{abc u0 com.target/.MainActivity}\n"
        "mCurrentFocus=Window{def u0 com.background/.Activity}",
        "mCurrentFocus=Window{abc u0 com.target/.MainActivity}\nmCurrentFocus=null",
    ],
)
def test_unknown_or_ambiguous_focus_never_uses_background_task(dump):
    assert focused_package(dump) is None


def make_ui(dump):
    calls = []

    def run(*args):
        calls.append(args)
        return dump

    ui = AppUI.__new__(AppUI)
    ui.bridge = SimpleNamespace(check_device=lambda: calls.append("guard"), run=run)
    ui.device = SimpleNamespace(press=lambda key: calls.append(("press", key)))
    ui.target_package = "com.target"
    ui.ad_ids = ()
    return ui, calls


def test_actual_focus_not_library_stale_app_current():
    ui, calls = make_ui("mCurrentFocus=Window{abc u0 com.target/.MainActivity}")
    ui.device.app_current = lambda: {"package": "com.background"}
    assert ui.recover() == "unknown_overlay_needs_user"
    assert calls == ["guard", ("shell", "dumpsys", "window", "displays")]


def test_unknown_focus_does_not_send_back():
    ui, calls = make_ui("mCurrentFocus=null")
    assert ui.recover() == "unknown_foreground_needs_observation"
    assert ("press", "back") not in calls


def test_external_focused_app_sends_single_back():
    ui, calls = make_ui("mCurrentFocus=Window{abc u0 com.browser/.MainActivity}")
    assert ui.recover() == "back_sent_wait_for_new_observation"
    assert calls[-1] == ("press", "back")
