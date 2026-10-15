"""模拟时钟/厂商适配器的线程边界测试，不写模拟器或产生真实记录。"""

import math
import threading

import pytest

from budao.driver import DriveEngine
from budao.mumu import LocationAuthorizationExpired, MuMuBridge
from budao.mumu_driver import MuMuPositionWorker
from budao.routes import RoutePlanner, distance, interpolate
from budao.simulation import demo_config


class FakeMuMu(MuMuBridge):
    def __init__(self, clock):
        self.clock = clock
        self.locations = []
        self.guard_delay = 0
        self.failure = None
        self.before_authorize = lambda: None
        self.guard_calls = 0

    def check_device(self):
        self.guard_calls += 1

    def location_wgs84(self, point, *, authorize, command_timeout_s):
        self.check_device()
        self.clock[0] += self.guard_delay
        self.before_authorize()
        if authorize() is not True:
            raise LocationAuthorizationExpired()
        if self.failure:
            raise self.failure
        self.locations.append((point, command_timeout_s))


@pytest.fixture
def setup():
    clock = [0.0]
    config = demo_config()
    engine = DriveEngine(RoutePlanner(config), config.baseline[0])
    bridge = FakeMuMu(clock)
    worker = MuMuPositionWorker(bridge, engine, clock=lambda: clock[0])
    return worker, bridge, clock, config


def test_no_path_or_lease_never_outputs_location(setup):
    worker, bridge, clock, _ = setup
    worker._tick()
    clock[0] = 3
    worker._tick()
    assert not bridge.locations and worker.commands_acknowledged == 0


def test_guarded_clock_updates_coordinate_and_uses_bounded_vendor_command(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    clock[0] = 1
    worker._tick()
    assert worker.commands_acknowledged == 2
    assert distance(config.baseline[0], worker.last_acknowledged) == pytest.approx(2.5, abs=0.01)
    assert all(timeout == 1 for _, timeout in bridge.locations)


def test_path_endpoint_emitted_once_then_waits_for_app_confirmation(setup):
    worker, bridge, clock, config = setup
    target = interpolate(config.baseline[0], config.baseline[1], 3)
    worker.set_path((config.baseline[0], target), 0)
    worker._tick()
    clock[0] = 1.5
    worker._tick()
    assert worker.check().path_done and not worker.check().moving
    assert distance(worker.last_acknowledged, target) < 0.01
    clock[0] = 2
    worker._tick()
    assert len(bridge.locations) == 2


def test_expired_observer_lease_stops_and_resume_does_not_catch_up(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker._tick()
    position = worker.last_acknowledged
    clock[0] = 20
    worker._tick()
    assert not worker.check().moving and len(bridge.locations) == 1
    worker.renew(20)
    worker._tick()
    assert worker.last_acknowledged == position
    clock[0] = 21
    worker._tick()
    assert distance(position, worker.last_acknowledged) == pytest.approx(2.5, abs=0.01)


def test_slow_guard_cancels_unissued_tick_rolls_back_and_never_catches_up(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    bridge.guard_delay = 2
    clock[0] = 1
    worker._tick()
    assert len(bridge.locations) == 1 and worker.engine.position == config.baseline[0]
    assert worker.engine.total == 0 and not worker.check().moving
    bridge.guard_delay = 0
    worker.renew(clock[0])
    worker._tick()
    assert worker.last_acknowledged == config.baseline[0]


def test_pause_request_during_device_check_cancels_location_before_vendor_write(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    bridge.before_authorize = worker.revoked.set
    clock[0] = 1
    worker._tick()
    assert not bridge.locations and not worker.engine.enabled


def test_pause_before_tick_does_not_even_advance_unissued_internal_position(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker.revoked.set()
    clock[0] = 1
    worker._tick()
    assert worker.engine.position == config.baseline[0] and not bridge.locations


@pytest.mark.parametrize("sampled", [-1, math.nan, math.inf, 10, 0])
def test_bad_old_or_repeated_observation_does_not_extend_lease(setup, sampled):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    with pytest.raises(ValueError):
        worker.renew(sampled)
    assert not worker.check().moving and not bridge.locations


def test_renewing_permission_does_not_advance_the_trajectory(setup):
    worker, _bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew(1)
    assert worker.engine.position == config.baseline[0] and worker.engine.total == 0


def test_unknown_vendor_result_stops_thread_without_retry_or_claim_of_receiving(setup):
    worker, bridge, _clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    bridge.failure = TimeoutError("simulated vendor timeout")
    worker._loop()
    assert worker.stop_event.is_set() and worker.output_state_unknown
    assert worker.commands_acknowledged == 0 and worker.last_acknowledged is None
    with pytest.raises(RuntimeError):
        worker.check()
    with pytest.raises(ValueError):
        worker.renew(1)


def test_no_qemu_console_sensors_or_stationary_repeated_output_on_stop(setup):
    worker, bridge, _clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker.stop()
    worker._tick()
    assert not bridge.locations and not worker.engine.enabled
    with pytest.raises(RuntimeError):
        worker.start()


def test_nonowned_bridge_and_prearmed_core_rejected(setup):
    worker, bridge, _clock, config = setup
    with pytest.raises(ValueError):
        MuMuPositionWorker(object(), worker.engine)
    worker.set_path(config.baseline, 0)
    with pytest.raises(ValueError):
        MuMuPositionWorker(bridge, worker.engine)


def test_thread_shutdown_does_not_claim_success_until_thread_is_dead(setup):
    worker, _bridge, _clock, _config = setup

    class PendingThread:
        def join(self, timeout):
            assert timeout == 2

        def is_alive(self):
            return True

    worker.thread = PendingThread()
    with pytest.raises(RuntimeError, match="未声称线程已退出"):
        worker.stop()
    assert worker.stop_event.is_set() and worker.revoked.is_set()


def test_loop_wait_is_bounded_one_hertz_and_does_not_catch_up(setup, monkeypatch):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    bridge.guard_delay = 0.25
    waits = []

    def wait(delay):
        waits.append(delay)
        worker.stop_event.set()

    monkeypatch.setattr(worker.stop_event, "wait", wait)
    worker._loop()
    assert waits == [0.75] and len(bridge.locations) == 1


def test_first_path_wakes_only_before_first_vendor_output(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    assert worker.startup_wake.is_set()
    worker.startup_wake.clear()
    worker._tick()
    assert len(bridge.locations) == 1
    clock[0] = 1
    worker.set_path(config.baseline, 1, repeat=True)
    assert not worker.startup_wake.is_set()


def test_idle_thread_first_path_wakes_without_waiting_for_poll_grid(setup, monkeypatch):
    worker, bridge, _clock, config = setup
    parked, sent = threading.Event(), threading.Event()
    wait = worker.startup_wake.wait
    output = bridge.location_wgs84

    def parked_wait(_delay):
        parked.set()
        return wait(5)  # Controlled synthetic wait: only wake/stop can release it promptly.

    def location(*args, **kwargs):
        output(*args, **kwargs)
        sent.set()

    monkeypatch.setattr(worker.startup_wake, "wait", parked_wait)
    monkeypatch.setattr(bridge, "location_wgs84", location)
    try:
        worker.start()
        assert parked.wait(1)
        assert not bridge.locations
        worker.set_path(config.baseline, 0, repeat=True)
        assert sent.wait(1)
    finally:
        worker.stop()
    assert len(bridge.locations) == 1 and not worker.thread.is_alive()


def test_real_thread_pause_can_revoke_while_guard_holds_io_lock(setup):
    worker, bridge, _clock, config = setup
    entered, release = threading.Event(), threading.Event()

    def slow_guard():
        entered.set()
        assert release.wait(2), "Test must release simulated guard"

    bridge.before_authorize = slow_guard
    worker.set_path(config.baseline, 0, repeat=True)
    pauser = threading.Thread(target=worker.pause)
    try:
        worker.start()
        assert entered.wait(2)
        pauser.start()
        assert worker.revoked.wait(2)
        release.set()
        pauser.join(2)
        assert not pauser.is_alive()
    finally:
        release.set()
        worker.stop()
        if pauser.ident is not None:
            pauser.join(2)
    assert not bridge.locations and not worker.engine.enabled


def test_async_renewal_is_latest_only_and_keeps_original_expiry(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    clock[0] = 1.25
    worker.renew_async(1.25)
    clock[0] = 2
    worker._tick()
    assert worker.last_observed_at == 1.25 and worker.engine.lease_until == 3.25
    clock[0] = 3.25
    worker._tick()
    assert len(bridge.locations) == 1 and not worker.engine.enabled


def test_async_renewal_does_not_wait_for_vendor_io_lock(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def slow_guard():
        entered.set()
        assert release.wait(2)

    bridge.before_authorize = slow_guard
    tick = threading.Thread(target=worker._tick)

    def renew():
        worker.renew_async(1)
        delivered.set()

    try:
        tick.start()
        assert entered.wait(2)
        clock[0] = 1
        sender = threading.Thread(target=renew)
        sender.start()
        assert delivered.wait(0.5), "Mailbox must not wait for the IO lock"
        sender.join(1)
    finally:
        release.set()
        tick.join(2)
        worker.stop()


def test_pause_discards_pending_observation_and_disallows_rearming(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    worker.pause()
    worker._tick()
    with pytest.raises(ValueError):
        worker.renew_async(1.5)
    assert not bridge.locations and worker.revoked.is_set()


def test_pending_observation_cannot_extend_expired_window(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    clock[0] = 3
    worker._tick()
    assert not bridge.locations and worker.revoked.is_set()
    assert not worker.engine.enabled


def test_path_switch_clears_mailbox_from_previous_path(setup):
    worker, _, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    clock[0] = 1.5
    worker.set_path(config.baseline, 1.5, repeat=True)
    assert worker.pending_renewal is None and worker.last_observed_at == 1.5


def test_external_revocation_after_delivery_is_not_cleared_by_old_pending_frame(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    worker.revoked.set()
    worker._tick()
    assert worker.revoked.is_set() and not bridge.locations


def test_sync_cannot_replay_observation_already_delivered_async(setup):
    worker, _, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    clock[0] = 1
    worker.renew_async(1)
    with pytest.raises(ValueError):
        worker.renew(1)


def test_gap_free_mailbox_handoff_preserves_clock_at_late_tick(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    clock[0] = 1
    worker._tick()
    clock[0] = 1.7  # delivered before the old deadline of 2
    worker.renew_async(1.4)
    clock[0] = 2.1  # consumed after old deadline, but before renewed deadline
    worker._tick()
    assert worker.engine.total == pytest.approx(2.5 * 2.1)
    assert worker.commands_acknowledged == 3
    assert worker.engine.lease_until == pytest.approx(3.4)


def test_mailbox_received_after_gap_does_not_catch_up(setup):
    worker, _, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    clock[0] = 1
    worker._tick()
    clock[0] = 2.1  # real gap, even if the UI sampled before the deadline
    worker.renew_async(1.9)
    worker._tick()
    assert worker.engine.total == pytest.approx(2.5)


def test_latest_only_mailbox_keeps_continuous_delivery_chain(setup):
    worker, _, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    clock[0] = 1.7
    worker.renew_async(1.4)
    clock[0] = 2.2
    worker.renew_async(1.9)
    clock[0] = 2.5
    worker._tick()
    assert worker.engine.total == pytest.approx(2.5 * 2.5)
    assert worker.engine.lease_until == pytest.approx(3.9)


def test_continuous_delivery_during_guard_extends_permission_without_io_wait(setup):
    worker, bridge, clock, config = setup
    worker.set_path(config.baseline, 0, repeat=True)
    worker._tick()
    clock[0] = 1.7

    def deliver():
        clock[0] = 1.9
        worker.renew_async(1.8)
        clock[0] = 2.1

    bridge.before_authorize = deliver
    worker._tick()
    assert worker.commands_acknowledged == 2
    assert worker.engine.total == pytest.approx(2.5 * 1.7)
    assert worker.engine.lease_until == pytest.approx(3.8)
