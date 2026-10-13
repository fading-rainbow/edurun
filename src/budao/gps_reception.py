"""自有前台服务的GPS回调接收证据；不修改位置、UI信号、来源或有效记录。"""

from __future__ import annotations

import math
import re

from .android import DIAGNOSTIC_PACKAGE
from .models import GeoPoint
from .routes import distance


def received_gps_matches(
    snapshot: dict | None, request: str, observed_ns: int, expected: GeoPoint
) -> bool:
    """核对新任务的实际接收，而不是信任模拟器Location内可能异常的时钟。"""
    try:
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("package") != DIAGNOSTIC_PACKAGE
            or snapshot.get("request_id") != request
            or not isinstance(request, str)
            or not re.fullmatch(r"[a-f0-9]{32}", request)
            or type(observed_ns) is not int
            or observed_ns <= 0
            or not isinstance(expected, GeoPoint)
        ):
            return False
        expected = GeoPoint.model_validate(expected.model_dump(warnings=False))
        gps = snapshot["gps_reception"]
        clocks = [
            gps["task_started_ns"],
            gps["first_received_ns"],
            gps["last_received_ns"],
            snapshot["elapsed_realtime_ns"],
        ]
        if (
            gps["registered"] is not True
            or gps["provider"] != "gps"
            or gps["has_accuracy"] is not True
            or type(gps["callback_count"]) is not int
            or gps["callback_count"] < 2
            or any(type(v) is not int or v <= 0 for v in clocks)
            # /proc/uptime只提供两位小数，真实读取时刻位于该10ms区间内。
            # 这里只承认时钟分辨率，不改变证据时间或原生两秒租约。
            or not clocks[0] <= clocks[1] < clocks[2] <= clocks[3] < observed_ns + 10_000_000
            or observed_ns - clocks[2] > 2_000_000_000
            or type(gps["is_mock"]) is not bool
        ):
            return False
        values = [gps["latitude"], gps["longitude"], gps["accuracy_m"]]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            return False
        if not 0 <= gps["accuracy_m"] <= 5:
            return False
        actual = GeoPoint(lat=gps["latitude"], lon=gps["longitude"])
        return distance(actual, expected) <= 10
    except (KeyError, TypeError, ValueError):
        return False
