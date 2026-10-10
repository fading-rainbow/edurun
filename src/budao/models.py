"""边界明确的数据结构；模型只能观察像素，不能生成地理坐标。"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class GeoPoint(StrictModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-85, le=85)


class PixelPoint(StrictModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class RouteConfig(StrictModel):
    coordinate_system: Literal["WGS84"] = "WGS84"
    region: tuple[GeoPoint, ...]
    corridors: tuple[tuple[GeoPoint, ...], ...]
    baseline: tuple[GeoPoint, ...]
    speed_mps: float = Field(default=2.5, gt=0, le=10)
    minimum_distance_m: float = Field(default=1500, gt=0)
    margin_m: float = Field(default=50, ge=0)
    snap_tolerance_m: float = Field(default=5, gt=0, le=50)

    @model_validator(mode="after")
    def shapes(self) -> RouteConfig:
        if len(self.region) < 4 or self.region[0] != self.region[-1]:
            raise ValueError("region 必须是至少三个顶点组成的闭合多边形")
        if not self.corridors or any(len(line) < 2 for line in self.corridors):
            raise ValueError("corridors 必须包含至少一条两点以上的通道")
        if len(self.baseline) < 2:
            raise ValueError("baseline 必须包含至少两个点")
        return self


class VisualObservation(StrictModel):
    """远端视觉服务的唯一输出契约：不接受经纬度或执行命令。"""

    screen: Literal["running", "ad", "unknown", "summary"]
    confidence: float = Field(ge=0, le=1)
    checkpoint_number: int | None = Field(default=None, ge=1, le=3)
    checkpoint_pixel: PixelPoint | None = None
    completed_checkpoints: int | None = Field(default=None, ge=0, le=3)
    distance_m: float | None = Field(default=None, ge=0)
    record_valid: bool | None = None

    @model_validator(mode="after")
    def paired_target(self) -> VisualObservation:
        if (self.checkpoint_number is None) != (self.checkpoint_pixel is None):
            raise ValueError("目标序号与像素位置必须同时给出或同时为空")
        return self


class Observation(StrictModel):
    """由本地观察器补充时间、包名及标定后坐标。"""

    frame_id: str
    sampled_at: float
    package: str
    screen: Literal["running", "ad", "unknown", "summary"] = "running"
    confidence: float = Field(default=1, ge=0, le=1)
    checkpoint_number: int | None = Field(default=None, ge=1, le=3)
    checkpoint: GeoPoint | None = None
    completed_checkpoints: int = Field(default=0, ge=0, le=3)
    distance_m: float = Field(default=0, ge=0)
    record_id: str | None = None
    record_valid: bool = False

    @field_validator("sampled_at")
    @classmethod
    def finite_time(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("时间必须有限")
        return value


class Decision(StrictModel):
    action: Literal["base_route", "navigate", "wait", "pause", "finish_ready", "validated"]
    reason: str
    target: GeoPoint | None = None
