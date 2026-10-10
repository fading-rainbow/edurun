"""固定俯视地图的局部仿射标定；缩放/旋转/平移后必须换 viewport_id。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .models import GeoPoint, PixelPoint
from .routes import RoutePlanner, distance


@dataclass(frozen=True)
class Anchor:
    pixel: PixelPoint
    geo: GeoPoint


class Calibration:
    def __init__(
        self,
        planner: RoutePlanner,
        anchors: list[Anchor],
        holdouts: list[Anchor],
        viewport_id: str,
        tolerance_m: float = 5,
    ):
        if len(anchors) < 3 or not holdouts or not viewport_id:
            raise ValueError("至少3个非共线锚点、1个独立验证点及 viewport_id")
        if not math.isfinite(tolerance_m) or tolerance_m <= 0:
            raise ValueError("误差阈值必须大于0")
        a = np.array([[p.pixel.x, p.pixel.y, 1] for p in anchors], dtype=float)
        if np.linalg.matrix_rank(a) < 3:
            raise ValueError("锚点共线，无法建立标定")
        for item in holdouts:
            if any(item == anchor for anchor in anchors):
                raise ValueError("验证点不能重复使用拟合锚点")
        b = np.array([planner.xy(p.geo) for p in anchors])
        self.matrix = np.linalg.lstsq(a, b, rcond=None)[0]
        self.planner = planner
        self.viewport_id = viewport_id
        self.max_error_m = max(distance(self.map(p.pixel, viewport_id), p.geo) for p in holdouts)
        if self.max_error_m > tolerance_m:
            raise ValueError("独立验证点误差超过阈值，拒绝使用标定")

    def map(self, pixel: PixelPoint, viewport_id: str) -> GeoPoint:
        if viewport_id != self.viewport_id:
            raise ValueError("地图视口已变化，标定失效")
        xy = np.array([pixel.x, pixel.y, 1]) @ self.matrix
        point = self.planner.geo(xy)
        if not self.planner.contains(point):
            raise ValueError("识别点不在标定区域内")
        return point
