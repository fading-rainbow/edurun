"""有限的界面恢复：只关闭已知广告，不让模型自由点击或安装。"""

from __future__ import annotations

import os

from PIL import Image

from .android import AndroidBridge


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

    def recover(self) -> str:
        self.bridge.check_device()
        current = self.device.app_current().get("package")
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
