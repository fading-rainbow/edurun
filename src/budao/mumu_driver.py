"""MuMu官方位置链路的有限时钟线程；没有App开始/上传操作或模拟步数。"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from .driver import DriveEngine, DriveState
from .models import GeoPoint
from .mumu import LocationAuthorizationExpired, MuMuBridge


class MuMuPositionWorker:
    """调用方仍须验证会话开始/身份。只能通过本对象更新新核心，不共享QEMU租约。"""

    def __init__(
        self,
        bridge: MuMuBridge,
        engine: DriveEngine,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(bridge, MuMuBridge):
            raise ValueError("位置线程只接受独立MuMu适配器，不能用于真机或QEMU")
        if engine.enabled or engine.player is not None:
            raise ValueError("必须从未授权的新驱动核心开始，不能复用其他线程的移动租约")
        bridge.check_device()
        self.bridge, self.engine, self.clock = bridge, engine, clock
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.startup_wake = threading.Event()
        self.revoked = threading.Event()
        self.revoked.set()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.last_observed_at = -math.inf
        self.state = engine.step(clock())
        self.commands_acknowledged = 0
        self.last_acknowledged: GeoPoint | None = None
        self.output_state_unknown = False
        self.pending_lock = threading.Lock()
        self.pending_renewal: float | None = None
        self.pending_continuous = False
        self.last_async_observed_at = -math.inf
        self.async_allowed = False

    def _snapshot(self) -> DriveState:
        # 更新许可不推进位置；只有输出线程的时钟步可以改变轨迹。
        return DriveState(
            self.engine.position,
            self.engine.enabled,
            bool(self.engine.player and self.engine.player.done),
            self.engine.total,
            self.engine.reason,
        )

    def _fresh(self, observed_at: float) -> float:
        now = self.clock()
        if (
            not math.isfinite(now)
            or not math.isfinite(observed_at)
            or observed_at < 0
            or not 0 <= now - observed_at <= 2
            or observed_at <= max(self.last_observed_at, self.last_async_observed_at)
            or self.stop_event.is_set()
            or self.error is not None
        ):
            self.engine.pause("观察过期、重复或线程已停止，拒绝续约")
            self.revoked.set()
            raise ValueError("需要读取前时间戳递增的2秒内新观察，停止后不可续约")
        return now

    def set_path(self, points: tuple[GeoPoint, ...], observed_at: float, *, repeat=False) -> None:
        # 在等待IO锁之前撤销旧输出，慢速设备查询不能阻止暂停/切换请求撤销许可。
        self.revoked.set()
        self._clear_pending(disallow=True)
        with self.lock:
            self.engine.pause("新路径检查中")
            try:
                now = self._fresh(observed_at)
                self.engine.set_path(points, now, repeat=repeat)
                self.last_observed_at = observed_at
                with self.pending_lock:
                    self.async_allowed = True
                    self.revoked.clear()
            finally:
                self.state = self._snapshot()
            if self.commands_acknowledged == 0:
                self.startup_wake.set()

    def renew(self, observed_at: float) -> None:
        with self.lock:
            try:
                now = self._fresh(observed_at)
                self.engine.renew(now)
                self.last_observed_at = observed_at
                with self.pending_lock:
                    self.async_allowed = True
                    self.revoked.clear()
            finally:
                self.state = self._snapshot()

    def renew_async(self, observed_at: float) -> None:
        """只交付最新观察，不等待厂商IO；在线程中按观察时间原期限应用，不补发。"""
        now = self.clock()
        with self.pending_lock:
            if (
                not math.isfinite(now)
                or not math.isfinite(observed_at)
                or observed_at < 0
                or not 0 <= now - observed_at < 2
                or observed_at <= max(self.last_observed_at, self.last_async_observed_at)
                or self.stop_event.is_set()
                or self.error is not None
                or self.engine.player is None
                or not self.async_allowed
            ):
                self.pending_renewal = None
                self.revoked.set()
                self.async_allowed = False
                raise ValueError("需要新鲜、递增且已有路径的观察，拒绝异步续约")
            # The mailbox is applied only on the next output tick. A renewal
            # delivered BEFORE the existing deadline is a continuous lease,
            # even when that tick happens after the old deadline. Preserve
            # this handoff through latest-only coalescing; never bridge a gap.
            deadline = self.engine.lease_until
            if self.pending_renewal is not None and self.pending_continuous:
                deadline = max(deadline, self.pending_renewal + 2)
            self.pending_continuous = self.engine.enabled and now < deadline
            self.pending_renewal = observed_at
            self.last_async_observed_at = observed_at
            self.revoked.clear()

    def _clear_pending(self, *, disallow=False):
        with self.pending_lock:
            self.pending_renewal = None
            self.pending_continuous = False
            if disallow:
                self.async_allowed = False
                self.revoked.set()

    def _apply_pending(self, now: float):
        with self.pending_lock:
            observed = self.pending_renewal
            continuous = self.pending_continuous
            self.pending_renewal = None
            self.pending_continuous = False
            if (
                observed is None
                or observed <= self.last_observed_at
                or not self.async_allowed
                or self.revoked.is_set()
            ):
                return
            age = now - observed
            if not 0 <= age < 2 or self.stop_event.is_set() or self.error is not None:
                self.engine.pause("异步观察已过期，不延长旧许可")
                self.revoked.set()
                self.async_allowed = False
                return
            # 与撤销使用同一小锁；暂停不能被已经取出的旧邮箱观察重新解除。
            last_tick = self.engine.player.last_time if self.engine.player else None
            preserve_clock = continuous and self.engine.enabled
            self.engine.renew(now, lease_s=2 - age)
            if preserve_clock and self.engine.player is not None:
                self.engine.player.last_time = last_tick
            self.last_observed_at = observed

    def pause(self, reason: str = "观察不明/广告/验证/用户暂停") -> None:
        self.revoked.set()
        self._clear_pending(disallow=True)
        with self.lock:
            self.engine.pause(reason)
            self.state = self._snapshot()

    def start(self) -> None:
        if self.thread is not None or self.stop_event.is_set():
            raise RuntimeError("位置线程不可重复启动或停止后重启")
        self.bridge.check_device()
        self.thread = threading.Thread(target=self._loop, name="budao-mumu-position", daemon=True)
        self.thread.start()

    def check(self) -> DriveState:
        with self.lock:
            if self.error is not None:
                raise RuntimeError("MuMu位置输出失败，已停止；需要重新核对接收位置") from self.error
            return self.state

    def _tick(self) -> None:
        with self.lock:
            now = self.clock()
            self._apply_pending(now)
            if self.revoked.is_set() or self.stop_event.is_set():
                self.engine.pause("移动许可撤销")
                self.state = self.engine.step(now)
                return
            player = self.engine.player
            before = (self.engine.position, self.engine.total, player.travelled if player else 0)
            permitted = self.engine.enabled and now < self.engine.lease_until
            lease_until = self.engine.lease_until
            self.state = self.engine.step(now)
            if (
                not permitted
                or self.revoked.is_set()
                or not (self.state.moving or self.state.path_done)
            ):
                return

            def authorize():
                current = self.clock()
                # A new validated frame may have arrived while the device
                # guard held the IO lock. Apply only a gap-free mailbox lease;
                # otherwise the old tick must still expire without output.
                with self.pending_lock:
                    continuous = self.pending_continuous
                if continuous and not self.revoked.is_set():
                    self._apply_pending(current)
                return (
                    math.isfinite(current)
                    and 0 <= current - now <= 1.5
                    and current < max(lease_until, self.engine.lease_until)
                    and not self.stop_event.is_set()
                    and not self.revoked.is_set()
                )

            try:
                self.bridge.location_wgs84(
                    self.state.position, authorize=authorize, command_timeout_s=1
                )
            except LocationAuthorizationExpired:
                # 明确未发出位置命令：回退这一未输出的时钟步，恢复后不追赶。
                self.engine.position, self.engine.total = before[:2]
                if player:
                    player.travelled = before[2]
                self.engine.pause("设备检查太慢或移动许可撤销；位置命令未发出")
                self.revoked.set()
                self.state = self.engine.step(self.clock())
            except Exception:
                # 超时/连接错误可能发生在厂商已执行之后，不能假装明确未输出或自动重试。
                self.output_state_unknown = True
                raise
            else:
                self.commands_acknowledged += 1
                self.last_acknowledged = self.state.position

    def _loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                before = self.clock()
                self._tick()
                # GPS至多约1Hz，不补发积压的位置。没有运动/计步API调用。
                delay = max(0, 1 - (self.clock() - before))
                if self.commands_acknowledged:
                    self.stop_event.wait(delay)
                else:
                    # 尚未输出任何位置时，首条已验证路径可唤醒空闲线程；
                    # 首次输出之后仍固定至多约1Hz，不因换路/续期突发补发。
                    self.startup_wake.wait(delay)
                    self.startup_wake.clear()
        except Exception as error:
            with self.lock:
                self.error = error
                self.engine.pause("位置线程失败；不重试不明结果")
                self.revoked.set()
                self.state = self._snapshot()
            self.stop_event.set()

    def stop(self, timeout_s: float = 2) -> None:
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("线程退出等待必须在0–30秒内")
        self.stop_event.set()
        self.startup_wake.set()
        self.revoked.set()
        self._clear_pending(disallow=True)
        if self.thread:
            self.thread.join(timeout_s)
            if self.thread.is_alive():
                raise RuntimeError("仍在设备只读检查/有界命令中，许可已撤销；未声称线程已退出")
        self.pause("位置线程已退出；未重复输出静止点或修改App")
