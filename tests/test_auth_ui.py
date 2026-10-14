import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from budao.auth_ui import LAUNCHER, MAIN, inspect_login_ui, login_ui, prepare_current_login
from budao.mumu import MuMuBridge
from budao.target import TARGET


def profile(**override):
    root = ET.Element("hierarchy")
    values = {
        "tv_title": "我的",
        "tv_mine": "我的",
        "user_uid": "ID:12345678",
        "user_nickname": "PRIVATE_NICKNAME",
        "tv_status": "已认证学生",
    }
    values.update(override)
    for key, value in values.items():
        ET.SubElement(
            root,
            "node",
            {
                "package": TARGET,
                "resource-id": TARGET + ":id/" + key,
                "visible-to-user": "true",
                "text": value,
                "selected": "true" if key == "tv_mine" else "false",
            },
        )
    return ET.tostring(root, encoding="unicode")


def test_login_ui_redacts_identity_and_does_not_infer_server_auth():
    result = login_ui(profile())
    assert result["state"] == "logged_in_ui" and result["student_verified_ui"]
    assert not result["server_auth_proven"]
    assert "PRIVATE_NICKNAME" not in str(result) and "12345678" not in str(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"user_uid": "12345678"},
        {"user_uid": "ID:bad"},
        {"user_nickname": ""},
        {"tv_title": "其他"},
        {"ad_close": ""},
        {"dialog_confirm": "确定"},
    ],
)
def test_unknown_partial_or_overlay_is_not_logged_in(changes):
    assert login_ui(profile(**changes))["state"] == "unknown"


def test_password_foreign_hidden_duplicate_and_entities_cannot_pass():
    for attribute, value in [
        ("password", "true"),
        ("package", "com.tencent.mobileqq"),
        ("visible-to-user", "false"),
    ]:
        tree = ET.fromstring(profile())
        node = next(n for n in tree if n.get("resource-id").endswith("user_uid"))
        node.set(attribute, value)
        assert login_ui(ET.tostring(tree, encoding="unicode"))["state"] == "unknown"
    assert login_ui('<!DOCTYPE hierarchy [<!ENTITY x "secret">]><hierarchy/>')["state"] == "unknown"
    assert login_ui(profile(user_nickname="立即登录"))["state"] == "login_required"


def nickless_profile(**override):
    root = ET.fromstring(profile(**override))
    for n in list(root):
        if n.get("resource-id") == TARGET + ":id/user_nickname":
            root.remove(n)
    ET.SubElement(
        root,
        "node",
        {
            "package": TARGET,
            "resource-id": TARGET + ":id/user_avatar_layout",
            "visible-to-user": "true",
        },
    )
    return ET.tostring(root, encoding="unicode")


def test_actual_nickless_student_profile_requires_all_collected_identity_controls():
    result = login_ui(nickless_profile())
    assert result["state"] == "logged_in_ui" and result["student_verified_ui"]
    assert not result["server_auth_proven"] and "12345678" not in str(result)
    for override in ({"user_uid": "ID:0"}, {"tv_status": "未认证"}, {"tv_title": "其它"}):
        assert login_ui(nickless_profile(**override))["state"] == "unknown"
    root = ET.fromstring(nickless_profile())
    avatar = next(n for n in root if n.get("resource-id") == TARGET + ":id/user_avatar_layout")
    avatar.set("visible-to-user", "false")
    assert login_ui(ET.tostring(root, encoding="unicode"))["state"] == "unknown"
    avatar.set("visible-to-user", "true")
    root.append(ET.fromstring(ET.tostring(avatar)))
    assert login_ui(ET.tostring(root, encoding="unicode"))["state"] == "unknown"


def test_nickless_fallback_cannot_accept_hidden_or_duplicate_nickname_fields():
    for count, visible in ((1, "false"), (2, "true")):
        root = ET.fromstring(nickless_profile())
        for _ in range(count):
            ET.SubElement(
                root,
                "node",
                {
                    "package": TARGET,
                    "resource-id": TARGET + ":id/user_nickname",
                    "text": "Fixture name",
                    "visible-to-user": visible,
                },
            )
        assert login_ui(ET.tostring(root, encoding="unicode"))["state"] == "unknown"


class Device(MuMuBridge):
    def __init__(
        self,
        focus=(TARGET, MAIN),
        *,
        tasks="ACTIVITY MANAGER ACTIVITIES\n",
        version="4.3.0",
        changed=False,
    ):
        self.focus, self.tasks, self.version, self.changed = focus, tasks, version, changed
        self.calls = []
        self.reads = 0

    def check_device(self):
        self.calls.append(("guard",))

    def run(self, *args):
        self.calls.append(args)
        if args == ("shell", "dumpsys", "window", "displays"):
            self.reads += 1
            f = self.focus
            if self.changed and self.reads > 1:
                f = ("com.tencent.mobileqq", "com.tencent.mobileqq.LoginActivity")
            return f"mCurrentFocus=Window{{123 u0 {f[0]}/{f[1]}}}"
        if args == ("shell", "dumpsys", "package", TARGET):
            return f"versionName={self.version}\nversionCode=230 minSdk=21"
        if args == ("shell", "dumpsys", "activity", "activities"):
            return self.tasks
        if args[:3] == ("shell", "am", "start"):
            self.focus = (
                (TARGET, MAIN) if TARGET in args[-1] else (LAUNCHER, LAUNCHER + ".LauncherActivity")
            )
        return ""


def test_protected_page_declined_before_tree_read():
    device = Device(("com.tencent.mobileqq", "com.tencent.mobileqq.LoginActivity"))
    result = inspect_login_ui(device, lambda _: pytest.fail("Do not inspect credential UI"))
    assert result["state"] == "protected_or_other_page"
    assert not any(call[:3] == ("shell", "am", "start") for call in device.calls)


def test_task_stack_multiple_spaces_and_unknown_target_fail_closed():
    from budao.auth_ui import target_task_tops

    for spaces in (" ", "  ", "\t"):
        assert target_task_tops(
            "ACTIVITY MANAGER ACTIVITIES\nHist"
            + spaces
            + "#0: ActivityRecord{1 u0 com.lptiyu.tanke/.activities.main.MainActivity t4}"
        ) == [MAIN]
    device = Device(
        (LAUNCHER, LAUNCHER + ".LauncherActivity"),
        tasks="ACTIVITY MANAGER ACTIVITIES\npackageName=com.lptiyu.tanke",
    )
    assert prepare_current_login(device)["state"] == "unknown_target_tasks"
    assert not any(call[:3] == ("shell", "am", "start") for call in device.calls)


def test_read_checks_version_and_foreground_change():
    assert (
        inspect_login_ui(Device(version="new"), lambda _: pytest.fail("Wrong version"))["state"]
        == "unsupported_version"
    )

    def ui(_):
        return SimpleNamespace(device=SimpleNamespace(dump_hierarchy=lambda: profile()))

    assert inspect_login_ui(Device(changed=True), ui)["state"] == "foreground_changed"
    assert inspect_login_ui(Device(), ui)["state"] == "logged_in_ui"


def test_launcher_login_navigation_never_overrides_background_run_or_protected_top():
    for page in [
        ".activities.school_run.MapRunActivity",
        ".activities.CameraActivity",
        ".LoginActivity",
    ]:
        device = Device(
            (LAUNCHER, LAUNCHER + ".LauncherActivity"),
            tasks="ACTIVITY MANAGER ACTIVITIES\nHist  #0: ActivityRecord{123 u0 com.lptiyu.tanke/"
            + page
            + " t4}",
        )
        assert prepare_current_login(device)["state"] == "existing_target_session"
        assert not any(call[:3] == ("shell", "am", "start") for call in device.calls)


def test_real_root_main_below_map_run_is_not_safe_home():
    from budao.auth_ui import target_task_tops

    tasks = (
        "ACTIVITY MANAGER ACTIVITIES\n"
        " * Hist  #1: ActivityRecord{abc u0 com.lptiyu.tanke/"
        ".activities.school_run.MapRunActivity t60}\n"
        " * Hist  #0: ActivityRecord{def u0 com.lptiyu.tanke/.activities.main.MainActivity t60}\n"
    )
    assert target_task_tops(tasks) == [TARGET + ".activities.school_run.MapRunActivity"]
    device = Device((LAUNCHER, LAUNCHER + ".LauncherActivity"), tasks=tasks)
    assert prepare_current_login(device)["state"] == "existing_target_session"
    assert not any(call[:3] == ("shell", "am", "start") for call in device.calls)


def test_task_top_in_another_package_is_protected_not_underlying_main():
    from budao.auth_ui import target_task_tops

    tasks = (
        "ACTIVITY MANAGER ACTIVITIES\n"
        "Hist #1: ActivityRecord{a u0 com.qq/AuthActivity t5}\n"
        "Hist #0: ActivityRecord{b u0 com.lptiyu.tanke/.activities.main.MainActivity t5}\n"
    )
    with pytest.raises(ValueError):
        target_task_tops(tasks)


def test_prepared_login_returns_to_launcher_without_starting_run_or_auth_copy():
    device = Device((LAUNCHER, LAUNCHER + ".LauncherActivity"))

    class UI:
        def dump_hierarchy(self):
            return profile()

        def __call__(self, **kwargs):
            return SimpleNamespace(count=0)

    result = prepare_current_login(
        device,
        ui_factory=lambda _: SimpleNamespace(device=UI()),
        clock=lambda: 0,
        sleep=lambda _: None,
    )
    assert result["state"] == "logged_in_ui"
    starts = [call[-1] for call in device.calls if call[:3] == ("shell", "am", "start")]
    assert starts == [
        TARGET + "/.activities.splash.SplashActivity",
        LAUNCHER + "/.LauncherActivity",
    ]
    assert not any("run-as" in call or "input" in call for call in device.calls)


def test_slow_mine_page_is_observed_without_second_click():
    device = Device((LAUNCHER, LAUNCHER + ".LauncherActivity"))
    ticks = iter([0, 13, 14, 15, 16])
    clicks = []

    class UI:
        reads = 0

        def dump_hierarchy(self):
            self.reads += 1
            return profile(tv_title="加载中") if self.reads <= 2 else profile()

        def __call__(self, **kwargs):
            return SimpleNamespace(
                count=1 if kwargs.get("text") == "我的" else 0,
                click=lambda: clicks.append(kwargs),
            )

    result = prepare_current_login(
        device,
        ui_factory=lambda _: SimpleNamespace(device=UI()),
        clock=lambda: next(ticks),
        sleep=lambda _: None,
    )
    assert result["state"] == "logged_in_ui"
    assert len(clicks) == 1
