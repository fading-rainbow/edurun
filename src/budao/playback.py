"""以实际单调时钟推进；间断后不追赶，避免瞬移。"""

from __future__ import annotations

import math

from .models import GeoPoint
from .routes import distance, interpolate, path_length


class Playback:
    def __init__(
        self,
        points: tuple[GeoPoint, ...],
        speed_mps: float,
        max_gap_s: float = 3,
        repeat: bool = False,
    ):
        if len(points) < 2 or not math.isfinite(speed_mps) or not 0 < speed_mps <= 10:
            raise ValueError("无效轨迹或速度")
        self.length = path_length(points)
        if self.length <= 0 or not math.isfinite(max_gap_s) or max_gap_s <= 0:
            raise ValueError("零长轨迹或无效时钟间断阈值")
        if repeat and distance(points[0], points[-1]) > 0.05:
            raise ValueError("重复轨迹必须闭合，不能在结束时瞬移回起点")
        self.points = points
        self.speed = speed_mps
        self.max_gap = max_gap_s
        self.repeat = repeat
        self.travelled = 0.0
        self.last_time: float | None = None

    @property
    def done(self) -> bool:
        return not self.repeat and self.travelled >= self.length

    def pause(self) -> None:
        self.last_time = None

    def position(self) -> GeoPoint:
        remaining = self.travelled % self.length if self.repeat else self.travelled
        for a, b in zip(self.points, self.points[1:], strict=False):
            length = distance(a, b)
            if remaining <= length:
                return interpolate(a, b, remaining)
            remaining -= length
        return self.points[-1]

    def advance(self, now: float) -> GeoPoint:
        if not math.isfinite(now):
            raise ValueError("时间必须有限")
        if self.last_time is not None:
            elapsed = now - self.last_time
            if elapsed < 0 or elapsed > self.max_gap:
                self.pause()
                raise ValueError("时钟倒退或驱动停顿：暂停，不补发跳跃的位置")
            self.travelled += self.speed * elapsed
            if not self.repeat:
                self.travelled = min(self.travelled, self.length)
        self.last_time = now
        return self.position()
