"""当前乐跑登录界面检查及有限正常导航；不读取认证目录/密码，不开始跑步。"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

from .mumu import MuMuBridge
from .target import TARGET
from .ui import AppUI, focused_component

MAIN = TARGET + ".activities.main.MainActivity"
SPLASH = TARGET + ".activities.splash.SplashActivity"
LAUNCHER = "com.edurun.launcher"


def login_ui(hierarchy: str) -> dict:
    result = {"state": "unknown", "student_verified_ui": False, "server_auth_proven": False}
    if (
        len(hierarchy) > 2 * 1024 * 1024
        or "<!DOCTYPE" in hierarchy.upper()
        or "<!ENTITY" in hierarchy.upper()
    ):
        return result
    try:
        root = ET.fromstring(hierarchy)
        nodes = [n for n in root.iter("node") if n.get("visible-to-user") == "true"]
        if any(
            n.get("password") == "true" or n.get("package") not in (TARGET, "com.android.systemui")
            for n in nodes
        ):
            return result
        if any(
            n.get("resource-id")
            in {TARGET + ":id/" + k for k in ("ad_close", "alert_dialog_content", "dialog_confirm")}
            for n in nodes
        ):
            return result

        def one(name):
            found = [
                n
                for n in nodes
                if n.get("package") == TARGET and n.get("resource-id") == TARGET + ":id/" + name
            ]
            return found[0] if len(found) == 1 else None

        title, mine, nickname, uid = (
            one(n) for n in ("tv_title", "tv_mine", "user_nickname", "user_uid")
        )
        if (
            title is None
            or title.get("text") != "我的"
            or mine is None
            or mine.get("selected") != "true"
        ):
            return result
        if nickname is not None and nickname.get("text") in {"立即登录", "点击登录", "登录"}:
            return {**result, "state": "login_required"}
        if (
            (nickname is not None and (not nickname.get("text") or len(nickname.get("text")) > 128))
            or uid is None
            or not re.fullmatch(r"ID:[0-9]{1,16}", uid.get("text", ""))
        ):
            return result
        status = one("tv_status")
        if nickname is None:
            # 4.3.0/code230实际冷启动的学生资料页不暴露昵称资源ID。
            # 只接收已认证、正数ID与唯一头像布局的完整组合；缺项不能当登录成功。
            avatars = [
                n
                for n in nodes
                if n.get("package") == TARGET
                and n.get("resource-id") == TARGET + ":id/user_avatar_layout"
            ]
            if (
                status is None
                or status.get("text") != "已认证学生"
                or int(uid.get("text")[3:]) <= 0
                or len(avatars) != 1
            ):
                return result
        return {
            **result,
            "state": "logged_in_ui",
            "student_verified_ui": status is not None and status.get("text") == "已认证学生",
        }
    except ET.ParseError:
        return result


def inspect_login_ui(bridge: MuMuBridge, ui_factory: Callable = AppUI) -> dict:
    bridge.check_device()
    before = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
    if before != (TARGET, MAIN):
        return {"state": "protected_or_other_page", "server_auth_proven": False}
    meta = bridge.run("shell", "dumpsys", "package", TARGET)
    if not re.search(r"^\s*versionName=4\.3\.0\s*$", meta, re.M) or not re.search(
        r"^\s*versionCode=230\b", meta, re.M
    ):
        return {"state": "unsupported_version", "server_auth_proven": False}
    ui = ui_factory(bridge)
    if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != before:
        return {"state": "foreground_changed", "server_auth_proven": False}
    tree = ui.device.dump_hierarchy()
    if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != before:
        return {"state": "foreground_changed", "server_auth_proven": False}
    return login_ui(tree)


def prepare_current_login(
    bridge: MuMuBridge,
    *,
    ui_factory: Callable = AppUI,
    clock: Callable = time.monotonic,
    sleep: Callable = time.sleep,
) -> dict:
    """只正常打开客户端/关闭已知广告/切我的；旧运行页或受保护认证页不被导航覆盖。"""
    if not isinstance(bridge, MuMuBridge):
        raise ValueError("正常登录准备只能操作本项目MuMu")
    initial = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
    from_launcher = initial == (LAUNCHER, LAUNCHER + ".LauncherActivity")
    if not from_launcher and initial != (TARGET, MAIN):
        return {"state": "protected_or_other_page", "server_auth_proven": False}
    if from_launcher:
        tasks = bridge.run("shell", "dumpsys", "activity", "activities")
        tops = re.findall(r"Hist #0:.*?\bcom\.lptiyu\.tanke/([A-Za-z0-9_.$]+)", tasks)
        expanded = [TARGET + t if t.startswith(".") else t for t in tops]
        if any(t != MAIN for t in expanded):
            return {"state": "existing_target_session", "server_auth_proven": False}
        bridge.run("shell", "am", "start", "-n", TARGET + "/.activities.splash.SplashActivity")
    # 冷启动、广告和安全守卫RPC也消耗时间；界面更新不是点击完成即同步完成。
    # 后台异步检查允许30秒观察窗口，仍不重试启动/切换受保护页面。
    deadline = clock() + 30
    ui = None
    selected = False
    try:
        while clock() < deadline:
            focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
            if focus is None or focus == (TARGET, SPLASH):
                sleep(0.3)
                continue
            if focus != (TARGET, MAIN):
                return {"state": "protected_or_other_page", "server_auth_proven": False}
            # 未采集版本不能通过自动控件点击来尝试。
            meta = bridge.run("shell", "dumpsys", "package", TARGET)
            if not re.search(r"^\s*versionName=4\.3\.0\s*$", meta, re.M) or not re.search(
                r"^\s*versionCode=230\b", meta, re.M
            ):
                return {"state": "unsupported_version", "server_auth_proven": False}
            ui = ui or ui_factory(bridge)
            close = ui.device(resourceId=TARGET + ":id/ad_close", description="关闭")
            if close.count == 1:
                bridge.check_device()
                if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != focus:
                    return {"state": "foreground_changed", "server_auth_proven": False}
                close.click()
                sleep(0.2)
                continue
            result = inspect_login_ui(bridge, lambda _b, current=ui: current)
            if result["state"] != "unknown":
                return result
            mine = ui.device(resourceId=TARGET + ":id/tv_mine", text="我的")
            if selected:
                sleep(0.3)
                continue
            if mine.count != 1:
                return result
            bridge.check_device()
            if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != focus:
                return {"state": "foreground_changed", "server_auth_proven": False}
            mine.click()
            selected = True
            sleep(0.3)
        return {"state": "startup_timeout", "server_auth_proven": False}
    finally:
        # 只在仍是安全首页时返回原生助手；认证/密码/摄像头页面不被抢回或读取。
        if from_launcher and focused_component(
            bridge.run("shell", "dumpsys", "window", "displays")
        ) == (TARGET, MAIN):
            bridge.run("shell", "am", "start", "-n", LAUNCHER + "/.LauncherActivity")
