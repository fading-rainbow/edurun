"""执行槽的合成协议测试；没有设备、照片上传、真实开始或成绩证明。"""

import threading
import time

import pytest

from budao.execution import ExecutionCoordinator
from budao.session_runner import SessionRunner


class Preparation:
    def __init__(self, *, ready=True, release=None):
        self.ready, self.release = ready, release
        self.calls = 0
        self.entered = threading.Event()

    def check(self, _photo):
        self.calls += 1
        self.entered.set()
        if self.release:
            assert self.release.wait(3)
        return {
            "state": "ready" if self.ready else "blocked",
            "message": "Synthetic preparation",
            "ready": self.ready,
            "run_started": False,
            "records_submitted": 0,
            "checks": [
                {"key": key, "state": "pass"}
                for key in ("route", "photo", "device", "login", "motion", "runtime")
            ],
        }


class SyntheticRunner(SessionRunner):
    def __init__(self, release):
        self.release, self.started = release, False
        self.entered = threading.Event()
        self.calls = 0

    def run(self, authorize, publish, cancel):
        assert authorize() is True
        self.calls += 1
        self.started = True
        self.entered.set()
        base = {"run_started": True, "records_submitted": 0, "record_valid_proven": False}
        publish(
            base
            | {"state": "running", "message": "PRIVATE_SENSITIVE", "secret": "PRIVATE_SENSITIVE"}
        )
        assert self.release.wait(3)
        value = base | {"state": "paused" if cancel.is_set() else "finish_ready"}
        publish(value)
        return value


def wait_terminal(coordinator):
    deadline = time.monotonic() + 3
    while coordinator.slot.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not coordinator.slot.locked()
    return coordinator.status()


def test_factory_assembly_never_exposes_terminal_ready_to_native_poller():
    entered, release = threading.Event(), threading.Event()
    runner_release = threading.Event()
    runner_release.set()

    def factory():
        entered.set()
        assert release.wait(3)
        return SyntheticRunner(runner_release)

    coordinator = ExecutionCoordinator(Preparation(), factory)
    try:
        coordinator.begin(start=True, photo_ready=True)
        assert entered.wait(1)
        assert coordinator.status()["state"] == "preparing"
    finally:
        release.set()
    assert wait_terminal(coordinator)["state"] == "finish_ready"


def test_start_and_check_share_single_slot_and_never_queue_duplicate_runner():
    release = threading.Event()
    runner = SyntheticRunner(release)
    prep = Preparation()
    c = ExecutionCoordinator(prep, lambda: runner)
    try:
        c.begin(start=True, photo_ready=True)
        assert runner.entered.wait(1)
        operation = c.status()["operation_id"]
        for _ in range(4):
            assert c.begin(start=True, photo_ready=True)["operation_id"] == operation
            assert c.begin(start=False, photo_ready=True)["operation_id"] == operation
        assert runner.calls == prep.calls == 1
        assert "PRIVATE_SENSITIVE" not in str(c.status())
        copy = c.status()
        copy["checks"][0]["state"] = "corrupted"
        assert c.status()["checks"][0]["state"] == "pass"
    finally:
        release.set()
    result = wait_terminal(c)
    assert result["state"] == "finish_ready" and not result["record_valid_proven"]
    assert result["records_submitted"] == 0 and not result["one_click_workflow_verified"]


def test_cancel_in_preparation_never_constructs_a_runner_and_no_late_check_overwrites_stop():
    release = threading.Event()
    prep = Preparation(release=release)
    c = ExecutionCoordinator(prep, lambda: pytest.fail("Cancelled preparation must not start"))
    c.begin(start=True, photo_ready=True)
    assert prep.entered.wait(1)
    assert c.stop()["state"] == "stopping"
    release.set()
    result = wait_terminal(c)
    assert result["state"] == "paused" and not result["run_started"]


def test_stop_request_is_not_cleanup_confirmation_or_a_submission():
    release = threading.Event()
    runner = SyntheticRunner(release)
    c = ExecutionCoordinator(Preparation(), lambda: runner)
    c.begin(start=True, photo_ready=True)
    assert runner.entered.wait(1)
    stopped = c.stop()
    assert stopped["state"] == "stopping" and "mode_restored_normal" not in stopped
    release.set()
    result = wait_terminal(c)
    assert result["state"] == "paused" and result["records_submitted"] == 0


def test_unready_check_only_or_missing_component_does_not_start():
    for ready, start, factory in (
        (False, True, lambda: pytest.fail("Unready")),
        (True, False, lambda: pytest.fail("Check only")),
        (True, True, None),
    ):
        c = ExecutionCoordinator(Preparation(ready=ready), factory)
        c.begin(start=start, photo_ready=True)
        assert wait_terminal(c)["run_started"] is False


def test_wrong_factory_type_fails_terminal_and_releases_slot_without_echo():
    c = ExecutionCoordinator(Preparation(), lambda: object())
    c.begin(start=True, photo_ready=True)
    result = wait_terminal(c)
    assert result["state"] == "error"
    assert result["ready"] is False and result["run_started"] is False


def test_runner_error_also_clears_preparation_ready_not_just_factory_error():
    c = ExecutionCoordinator(Preparation())
    c._set(Preparation().check(True))
    c._publish_runner(
        {
            "state": "error",
            "run_started": False,
            "records_submitted": 0,
            "record_valid_proven": False,
        }
    )
    assert c.status()["ready"] is False


@pytest.mark.parametrize(
    "state,phase,started,attempted,specific",
    [
        ("preparing", "gps_readiness", False, False, True),
        ("error", "gps_timeout", False, False, True),
        ("error", "gps_timeout", True, False, False),
        ("error", "gps_timeout", False, True, False),
        ("error", "gps_timeout", False, None, False),
        ("error", "PRIVATE_SENSITIVE", False, False, False),
        ("running", "gps_readiness", False, False, False),
    ],
)
def test_gps_progress_only_allows_fixed_text_and_truthful_unstarted_state(
    state, phase, started, attempted, specific
):
    c = ExecutionCoordinator(Preparation())
    c._publish_runner(
        {
            "state": state,
            "preparation_phase": phase,
            "run_started": started,
            "records_submitted": 0,
            "record_valid_proven": False,
            "message": "PRIVATE_SENSITIVE",
            **({"start_action_attempted": attempted} if attempted is not None else {}),
        }
    )
    result = c.status()
    assert ("定位就绪" in result["message"]) is specific
    assert "PRIVATE_SENSITIVE" not in str(result) and "preparation_phase" not in result


@pytest.mark.parametrize("change", [{"ready": 1}, {"records_submitted": True}, {"checks": []}])
def test_eligible_preparation_requires_exact_booleans_and_all_distinct_gates(change):
    result = Preparation().check(True)
    assert ExecutionCoordinator._eligible_preparation(result)
    assert not ExecutionCoordinator._eligible_preparation(result | change)


def test_execution_slot_does_not_accept_component_claims_of_submission_or_success():
    c = ExecutionCoordinator(Preparation())
    value = {
        "state": "running",
        "run_started": True,
        "records_submitted": 0,
        "record_valid_proven": False,
    }
    for change in (
        {"records_submitted": 1},
        {"record_valid_proven": True},
        {"distance_m": float("nan")},
    ):
        with pytest.raises(ValueError):
            c._publish_runner(value | change)


def test_cancel_with_late_recovering_state_never_overwrites_stopping():
    release = threading.Event()
    publish_event = threading.Event()

    class LateRecoveringRunner(SessionRunner):
        def __init__(self):
            self.started = True

        def run(self, authorize, publish, _cancel):
            assert authorize() is True
            publish(
                {
                    "state": "running",
                    "run_started": True,
                    "records_submitted": 0,
                    "record_valid_proven": False,
                }
            )
            publish_event.set()
            assert release.wait(3)
            # Try to publish recovering after stop was requested
            publish(
                {
                    "state": "recovering",
                    "run_started": True,
                    "records_submitted": 0,
                    "record_valid_proven": False,
                }
            )
            return {
                "state": "paused",
                "run_started": True,
                "records_submitted": 0,
                "record_valid_proven": False,
            }

    c = ExecutionCoordinator(Preparation(), LateRecoveringRunner)
    c.begin(start=True, photo_ready=True)
    assert publish_event.wait(1)
    stopped = c.stop()
    assert stopped["state"] == "stopping"
    release.set()
    result = wait_terminal(c)
    assert result["state"] == "paused"
