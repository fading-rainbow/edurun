"""独立的位置/运动时钟；慢速界面或模型请求不能延长移动授权。"""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from shapely.geometry import LineString

from .android import AndroidBridge, EmulatorConsole
from .models import GeoPoint
from .playback import Playback
from .routes import RoutePlanner, distance


@dataclass(frozen=True)
class MotionSample:
    acceleration: tuple[float, float, float]
    gyroscope: tuple[float, float, float]


REST = MotionSample((0, 0, 9.81), (0, 0, 0))


class DiagnosticWave:
    """仅用于验证传输链路，不冒充人体跑步、步数或实采数据。"""

    def sample(self, elapsed: float) -> MotionSample:
        wave = math.sin(elapsed * math.tau * 2.5)
        return MotionSample((2 * wave, 0.5 * wave, 9.81 + wave), (0.15 * wave, 0.05 * wave, 0))


class MotionClip:
    """外部CSV：t_s,ax,ay,az,gx,gy,gz；含重力 m/s²、rad/s；来源另行核实。"""

    def __init__(self, path: Path):
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("运动片段超过4MiB")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = ["t_s", "ax", "ay", "az", "gx", "gy", "gz"]
            if reader.fieldnames != fields:
                raise ValueError("CSV列名/顺序必须为t_s,ax,ay,az,gx,gy,gz")
            try:
                rows = np.asarray([[float(row[k]) for k in fields] for row in reader], dtype=float)
            except (TypeError, ValueError, KeyError) as error:
                raise ValueError("无效运动采样") from error
        if rows.ndim != 2 or len(rows) < 3 or not np.isfinite(rows).all():
            raise ValueError("至少3个有限的运动采样")
        times = rows[:, 0]
        if times[0] != 0 or not 1 <= times[-1] <= 600:
            raise ValueError("片段从0秒开始，长度1–600秒")
        if np.any(np.diff(times) <= 0) or np.any(np.diff(times) > 0.2):
            raise ValueError("采样时间必须递增，间隔不大于0.2秒")
        if np.any(np.abs(rows[:, 1:]) > 100):
            raise ValueError("三轴值超过console支持范围")
        self.times, self.values, self.duration = times, rows[:, 1:], float(times[-1])

    def sample(self, elapsed: float) -> MotionSample:
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("运动回放时间无效")
        values = tuple(
            float(np.interp(elapsed % self.duration, self.times, self.values[:, axis]))
            for axis in range(6)
        )
        return MotionSample(values[:3], values[3:])


@dataclass(frozen=True)
class DriveState:
    position: GeoPoint
    moving: bool
    path_done: bool
    travelled_m: float
    reason: str


class DriveEngine:
    """纯时钟核心，移动租约默认2秒；调用方须持续用新观察续约。"""

    def __init__(self, planner: RoutePlanner, initial: GeoPoint):
        if not planner.contains(initial):
            raise ValueError("初始点越界")
        self.planner = planner
        self.position = initial
        self.player: Playback | None = None
        self.lease_until = -math.inf
        self.enabled = False
        self.total = 0.0
        self.reason = "未授权移动"

    def set_path(self, points: tuple[GeoPoint, ...], now: float, repeat: bool = False) -> None:
        self.planner.validate_path(points)
        line = LineString([self.planner.xy(p) for p in points])
        if not self.planner.roads.buffer(self.planner.config.snap_tolerance_m).covers(line):
            raise ValueError("驱动路线离开已知通道，不走区域内任意直线")
        if distance(self.position, points[0]) > 0.05:
            raise ValueError("新路径必须从实际当前位置开始，不允许瞬移")
        self.player = Playback(points, self.planner.config.speed_mps, repeat=repeat)
        self.player.advance(now)
        self.renew(now)

    def renew(self, now: float, lease_s: float = 2) -> None:
        if not math.isfinite(now) or not math.isfinite(lease_s) or not 0 < lease_s <= 5:
            raise ValueError("移动租约必须有限且在0–5秒内")
        if self.player is None:
            raise ValueError("尚无路径")
        if not self.enabled or now >= self.lease_until:
            self.player.pause()
        self.enabled, self.lease_until, self.reason = True, now + lease_s, "新观察授权移动"

    def pause(self, reason: str = "观察等待/广告/未知状态") -> None:
        self.enabled, self.reason = False, reason
        if self.player:
            self.player.pause()

    def step(self, now: float) -> DriveState:
        if not math.isfinite(now):
            raise ValueError("驱动时间必须有限")
        if now >= self.lease_until:
            self.pause("观察租约过期，停止移动")
        if self.enabled and self.player:
            before = self.player.travelled
            try:
                position = self.player.advance(now)
            except ValueError:
                self.pause("时钟倒退/停顿，停止移动且不追赶")
            else:
                if not self.planner.contains(position):
                    self.pause("驱动点越界")
                else:
                    self.position = position
                    self.total += self.player.travelled - before
                    if self.player.done:
                        self.pause("路径到达，等待App确认；不是打卡成功")
        return DriveState(
            self.position,
            self.enabled,
            bool(self.player and self.player.done),
            self.total,
            self.reason,
        )


class MotionWorker:
    """默认25Hz、可选50Hz线程驱动，GPS1Hz；退出恢复静止值。"""

    def __init__(
        self,
        bridge: AndroidBridge,
        engine: DriveEngine,
        motion: DiagnosticWave | MotionClip | None = None,
        console_factory=EmulatorConsole,
        *,
        motion_hz: float = 25,
    ):
        if not math.isfinite(motion_hz) or not 25 <= motion_hz <= 50:
            raise ValueError("运动频率必须为25–50Hz")
        bridge.check_device()
        self.bridge, self.engine = bridge, engine
        self.motion, self.console_factory = motion or DiagnosticWave(), console_factory
        self.motion_hz, self.period_s = motion_hz, 1 / motion_hz
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.error: Exception | None = None
        self.iterations = 0
        self.thread: threading.Thread | None = None
        self.state = engine.step(time.monotonic())

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("驱动不能重复启动")
        self.thread = threading.Thread(target=self._loop, name="budao-motion", daemon=True)
        self.thread.start()

    def check(self) -> DriveState:
        with self.lock:
            if self.error is not None:
                raise RuntimeError("模拟器驱动失败，已停止") from self.error
            return self.state

    def _loop(self) -> None:
        start = time.monotonic()
        next_gps = start
        try:
            with self.console_factory(self.bridge) as console:
                try:
                    while not self.stop_event.is_set():
                        now = time.monotonic()
                        with self.lock:
                            self.state = self.engine.step(now)
                            state = self.state
                        sample = self.motion.sample(now - start) if state.moving else REST
                        console.sensor("acceleration", sample.acceleration)
                        console.sensor("gyroscope", sample.gyroscope)
                        if now >= next_gps:
                            console.location(
                                state.position,
                                self.engine.planner.config.speed_mps if state.moving else 0,
                            )
                            next_gps = now + 1
                        self.iterations += 1
                        # 不补发堆积的旧传感器事件。
                        # Windows Event.wait实测20ms会量化为约31ms；Python3.13 sleep
                        # 使用高精度等待。停止响应最多一个受限周期（20–40ms）。
                        time.sleep(max(0, self.period_s - (time.monotonic() - now)))
                finally:
                    console.sensor("acceleration", REST.acceleration)
                    console.sensor("gyroscope", REST.gyroscope)
                    console.location(self.engine.position, 0)
        except Exception as error:
            with self.lock:
                self.engine.pause("控制连接失败")
                self.error = error

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("驱动未在限时内退出")

    def __enter__(self) -> MotionWorker:
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.close()
