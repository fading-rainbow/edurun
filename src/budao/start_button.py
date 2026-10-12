"""本地开始按钮像素模板核对。仅观察、不点击；不把0秒/进度条当作开始文字。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .profiles import private_file
from .target import RUN_ACTIVITY, TARGET, TargetInspection


class StartButtonTemplate:
    def __init__(
        self,
        reference: Image.Image,
        screen_size: tuple[int, int],
        bounds: tuple[int, int, int, int],
    ):
        x1, y1, x2, y2 = bounds
        if (
            any(type(v) is not int for v in bounds + screen_size)
            or not 0 <= x1 < x2 <= screen_size[0]
            or not 0 <= y1 < y2 <= screen_size[1]
            or reference.size != (x2 - x1, y2 - y1)
            or min(reference.size) < 20
            or max(screen_size) > 4096
        ):
            raise ValueError("开始按钮模板尺寸或边界不符合要求")
        self.reference = reference.convert("RGB").copy()
        if max(ImageStat.Stat(self.reference).stddev) < 8:
            raise ValueError("纯色区域不能用作开始文字/按钮证据")
        self.screen_size, self.bounds = screen_size, bounds

    @classmethod
    def from_private_file(cls, root: Path, reference: Path, screen_size, bounds):
        path = private_file(root, reference)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("按钮模板过大")
        with Image.open(path) as image:
            return cls(image, screen_size, bounds)

    def matches(self, frame: TargetInspection, screenshot: Image.Image, hierarchy: str) -> bool:
        if (
            frame.package != TARGET
            or frame.activity != RUN_ACTIVITY
            or frame.version != "4.3.0"
            or frame.state != "metrics"
            or frame.run_control_state != "not_started_or_countdown"
            or frame.elapsed_s != 0
            or frame.distance_m != 0
            or frame.completed_checkpoints != 0
            or frame.counter_label != "已打卡"
            or screenshot.size != self.screen_size
            or any(
                i != "start_controls_and_current_record_identity_unverified" for i in frame.issues
            )
            or len(hierarchy) > 2 * 1024 * 1024
            or "<!DOCTYPE" in hierarchy.upper()
            or "<!ENTITY" in hierarchy.upper()
        ):
            return False
        try:
            nodes = [
                n
                for n in ET.fromstring(hierarchy).iter("node")
                if n.get("package") == TARGET
                and n.get("resource-id") == TARGET + ":id/tv_start_pause_continue_run"
                and n.get("visible-to-user") == "true"
            ]
            if (
                len(nodes) != 1
                or nodes[0].get("class") != "android.widget.ProgressBar"
                or nodes[0].get("enabled") != "true"
            ):
                return False
            found = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", nodes[0].get("bounds", ""))
            if found is None or tuple(map(int, found.groups())) != self.bounds:
                return False
        except ET.ParseError:
            return False
        crop = screenshot.crop(self.bounds).convert("RGB")
        difference = ImageStat.Stat(ImageChops.difference(self.reference, crop))
        # 固定严格阈值；不允许配置用巨大阈值把暂停/倒计时当开始。
        return max(difference.rms) <= 3
