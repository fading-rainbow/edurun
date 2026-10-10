"""有限的界面恢复：只关闭已知广告，不让模型自由点击或安装。"""

from __future__ import annotations

import os
import re

from PIL import Image

from .android import AndroidBridge


def focused_package(window_displays: str) -> str | None:
    """从真实焦点读包名；不回退到后台任务列表的最后一个Activity。"""
    focus = re.findall(r"^\s*mCurrentFocus=([^\r\n]+)", window_displays, re.MULTILINE)
    if len(focus) != 1:
        return None
    match = re.fullmatch(
        r"Window\{[^\r\n{}]*\s+([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+\}", focus[0].strip()
    )
    return match[1] if match else None


class AppUI:
    def __init__(
        self,
        bridge: AndroidBridge,
        target_package: str = "com.lptiyu.tanke",
        known_ad_resource_ids: tuple[str, ...] = (),
    ):
        bridge.check_device()
        self.bridge = bridge
        self.target_package = target_package
        self.ad_ids = known_ad_resource_ids
        os.environ["ADB"] = str(bridge.adb)
        import uiautomator2 as u2

        self.device = u2.connect(bridge.serial)

    def screenshot(self) -> Image.Image:
        self.bridge.check_device()
        return self.device.screenshot()

    def current_package(self) -> str | None:
        self.bridge.check_device()
        # API36的window windows不再包含mCurrentFocus，adbutils会误选后台Activity。
        # displays仍提供当前焦点；缺失/多显示焦点时停止，不猜测。
        return focused_package(self.bridge.run("shell", "dumpsys", "window", "displays"))

    def recover(self) -> str:
        current = self.current_package()
        if current is None:
            return "unknown_foreground_needs_observation"
        if current != self.target_package:
            self.device.press("back")
            return "back_sent_wait_for_new_observation"
        for resource_id in self.ad_ids:
            if not resource_id.startswith(self.target_package + ":id/"):
                raise ValueError("广告按钮必须属于明确的目标App资源ID")
            selector = self.device(resourceId=resource_id)
            if selector.exists:
                selector.click()
                return "known_ad_closed_wait_for_new_observation"
        return "unknown_overlay_needs_user"
