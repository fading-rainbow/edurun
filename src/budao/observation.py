"""连接截图、视觉输出和已验证标定；关键完成状态仍来自本地App控件。"""

from __future__ import annotations

import io
import math
import uuid
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

from .calibration import Calibration
from .models import Observation
from .vision import VisionClient, crop_image


@dataclass(frozen=True)
class TrustedStatus:
    """真实流程必须由已审核控件读取，不可用模型推测填充。"""

    package: str
    distance_m: float
    completed_checkpoints: int
    running: bool


class ViewportGuard:
    def __init__(
        self,
        reference: Image.Image,
        landmark_boxes: tuple[tuple[int, int, int, int], ...],
        max_rms: float = 12,
    ):
        if len(landmark_boxes) < 3 or not math.isfinite(max_rms) or max_rms <= 0:
            raise ValueError("至少3个稳定地图地标区域及正的变化阈值")
        self.size = reference.size
        self.boxes = landmark_boxes
        self.threshold = max_rms
        self.patches = []
        for box in landmark_boxes:
            # 使用统一裁剪验证，拒绝截取地图外的敏感区域。
            crop_image(reference, box)
            patch = reference.crop(box).convert("L")
            if ImageStat.Stat(patch).stddev[0] < 5:
                raise ValueError("平坦无特征区域不能作为地图地标")
            self.patches.append(patch)

    def matches(self, current: Image.Image) -> bool:
        if current.size != self.size:
            return False
        for box, reference in zip(self.boxes, self.patches, strict=True):
            difference = ImageChops.difference(reference, current.crop(box).convert("L"))
            if ImageStat.Stat(difference).rms[0] > self.threshold:
                return False
        return True


class MappedObserver:
    def __init__(
        self,
        calibration: Calibration,
        viewport_guard: ViewportGuard,
        client: VisionClient,
        map_crop: tuple[int, int, int, int],
        target_package: str = "com.lptiyu.tanke",
    ):
        self.calibration = calibration
        self.guard = viewport_guard
        self.client = client
        self.crop = map_crop
        self.target_package = target_package

    def observe(
        self, screenshot: Image.Image, status: TrustedStatus, sampled_at: float
    ) -> Observation:
        base = {
            "frame_id": uuid.uuid4().hex,
            "sampled_at": sampled_at,
            "package": status.package,
            "distance_m": status.distance_m,
            "completed_checkpoints": status.completed_checkpoints,
        }
        # 包名或界面不匹配时不发送图片，也不猜测控件状态。
        if status.package != self.target_package or not status.running:
            return Observation(**base, screen="unknown", confidence=0)
        png = crop_image(screenshot, self.crop)
        with Image.open(io.BytesIO(png)) as cropped:
            if not self.guard.matches(cropped):
                return Observation(**base, screen="unknown", confidence=0)
        visual = self.client.observe(png)
        point = None
        if visual.screen == "running" and visual.confidence >= 0.8 and visual.checkpoint_pixel:
            try:
                point = self.calibration.map(visual.checkpoint_pixel, self.calibration.viewport_id)
            except ValueError:
                return Observation(**base, screen="unknown", confidence=0)
        # 模型输出的里程、完成数、有效性不能覆盖本地可信状态。
        return Observation(
            **base,
            screen=visual.screen,
            confidence=visual.confidence,
            checkpoint_number=visual.checkpoint_number if point else None,
            checkpoint=point,
        )
