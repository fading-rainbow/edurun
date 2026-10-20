"""续跑证据边界测试；无设备操作，不把错误状态改写为成功。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_resume import proof
from test_runtime import frame, witness

from budao.live_runtime import LiveRuntime, RuntimeSettings, resume_trace_sources
from budao.resume import PAUSE_CLOCK_DETAIL, PauseClockExpired


def write(path, value):
    path.write_text(json.dumps(value), "utf-8")


def record(root, name):
    directory = root / ".local" / "runs" / name
    directory.mkdir(parents=True)
    (directory / "start-witness.json").write_text(witness().model_dump_json(), "utf-8")
    (directory / "input-observations.jsonl").write_text("{}\n", "utf-8")
    return directory


def link(parent, child):
    write(child / "recovery-parent.json", {"parent": str(parent), "new_start_attempted": False})
    write(parent / "recovery-claimed.json", {"child": str(child), "retry_allowed": False})


def test_chained_recovery_includes_original_received_trace_not_other_records(tmp_path):
    original = record(tmp_path, "native-original")
    child = record(tmp_path, "native-child")
    record(tmp_path, "native-unrelated")
    link(original, child)
    assert resume_trace_sources(tmp_path, child, witness()) == (
        child / "input-observations.jsonl",
        original / "input-observations.jsonl",
    )


@pytest.mark.parametrize("kind", ["foreign_start", "wrong_child", "cycle", "outside", "unclaimed"])
def test_trace_chain_rejects_wrong_identity_ownership_cycle_or_path(tmp_path, kind):
    parent, child = record(tmp_path, "native-parent"), record(tmp_path, "native-child")
    link(parent, child)
    if kind == "foreign_start":
        other = witness().model_copy(update={"request_id": "b" * 32})
        (parent / "start-witness.json").write_text(other.model_dump_json(), "utf-8")
    elif kind == "wrong_child":
        write(parent / "recovery-claimed.json", {"child": str(parent), "retry_allowed": False})
    elif kind == "cycle":
        link(child, parent)
    elif kind == "outside":
        write(
            child / "recovery-parent.json", {"parent": str(tmp_path), "new_start_attempted": False}
        )
    else:
        (parent / "recovery-claimed.json").unlink()
    with pytest.raises((ValueError, OSError)):
        resume_trace_sources(tmp_path, child, witness())


def test_trace_chain_limit_is_bounded(tmp_path):
    directories = [record(tmp_path, f"native-{i}") for i in range(9)]
    for parent, child in zip(directories, directories[1:], strict=False):
        link(parent, child)
    with pytest.raises(ValueError, match="八个"):
        resume_trace_sources(tmp_path, directories[-1], witness())


def test_five_owned_directories_with_only_two_actual_input_files_remain_bounded(tmp_path):
    directories = [record(tmp_path, f"native-{i}") for i in range(5)]
    for parent, child in zip(directories, directories[1:], strict=False):
        link(parent, child)
    for directory in directories[1:-1]:
        (directory / "input-observations.jsonl").unlink()
    assert resume_trace_sources(tmp_path, directories[-1], witness()) == (
        directories[-1] / "input-observations.jsonl",
        directories[0] / "input-observations.jsonl",
    )


@pytest.fixture
def clean_error_record(tmp_path, monkeypatch):
    child = record(tmp_path, "native-error")
    p = proof()
    (child / "resume-action-attempted.json").write_text("{}", "utf-8")
    (child / "resume-witness.json").write_text(p.model_dump_json(), "utf-8")
    saved = frame(30, control="paused", distance_m=60, elapsed=590)
    (child / "frames.jsonl").write_text(saved.model_dump_json() + "\n", "utf-8")
    status = {
        "state": "error",
        "run_started": True,
        "start_action_attempted": True,
        "mode_restored_normal": True,
        "app_pause_confirmed": True,
        "records_submitted": 0,
        "distance_m": 60,
        "elapsed_s": 590,
    }
    write(child / "status.json", status)
    write(
        child / "session-meta.json",
        {
            "original_request": witness().request_id,
            "start_stamp": "2026-10-19T19:00:00",
            "grade_before": 1,
        },
    )
    runtime = LiveRuntime.__new__(LiveRuntime)
    runtime.root = tmp_path
    runtime.settings = RuntimeSettings(
        mode="known_points_trial",
        motion_csv=Path(".local/a.csv"),
        independent_motion_evidence=Path(".local/proof.json"),
        start_template=Path(".local/start.png"),
        resume_evidence=child,
    )
    monkeypatch.setattr("budao.live_runtime.time.monotonic", lambda: 31)
    bridge = SimpleNamespace(serial=witness().serial, run=lambda *_: "2026-10-19")
    return runtime, bridge, child, status


def test_error_with_genuine_continue_and_confirmed_pause_is_recoverable_not_success(
    clean_error_record,
):
    runtime, bridge, child, _ = clean_error_record
    ticket = runtime._resume_ticket(bridge)
    assert ticket[0] == child and ticket[2].inspection.elapsed_s == 590
    assert json.loads((child / "status.json").read_text())["state"] == "error"
    assert not (child / "recovery-claimed.json").exists()


def test_ticket_retains_completed_points_observed_after_original_continue(clean_error_record):
    runtime, bridge, child, status = clean_error_record
    saved = frame(30, ("已确认旧点",), "当前点", control="paused", distance_m=60, elapsed=590)
    (child / "frames.jsonl").write_text(saved.model_dump_json() + "\n", "utf-8")
    write(child / "status.json", status | {"completed_checkpoints": 1})
    ticket = runtime._resume_ticket(bridge)
    assert ticket[2].inspection.completed_checkpoints == 1
    assert ticket[2].inspection.checkpoint_cards[0].name == "已确认旧点"


@pytest.mark.parametrize(
    "changes",
    [
        {"cleanup_unconfirmed": True},
        {"app_pause_confirmed": False},
        {"mode_restored_normal": False},
        {"run_started": False},
        {"start_action_attempted": False},
        {"records_submitted": 1},
        {"records_submitted": False},
        {"record_valid_proven": True},
        {"one_click_workflow_verified": True},
    ],
)
def test_unconfirmed_error_or_submitted_record_is_never_recovered(clean_error_record, changes):
    runtime, bridge, child, status = clean_error_record
    write(child / "status.json", status | changes)
    with pytest.raises(ValueError):
        runtime._resume_ticket(bridge)


@pytest.mark.parametrize("marker", ["save-action-attempted.json", "recovery-claimed.json"])
def test_consumed_or_saved_error_is_not_retried(clean_error_record, marker):
    runtime, bridge, child, _ = clean_error_record
    (child / marker).write_text("{}", "utf-8")
    with pytest.raises(ValueError):
        runtime._resume_ticket(bridge)


def test_cross_day_error_is_not_rebased_to_today(clean_error_record):
    runtime, bridge, _, _ = clean_error_record
    bridge.run = lambda *_: "2026-10-20"
    with pytest.raises(ValueError, match="跨日"):
        runtime._resume_ticket(bridge)


def test_missing_previous_continue_witness_never_allows_retry(clean_error_record):
    runtime, bridge, child, _ = clean_error_record
    (child / "resume-witness.json").unlink()
    with pytest.raises(OSError):
        runtime._resume_ticket(bridge)


@pytest.mark.parametrize("now", [10, 86431])
def test_previous_clock_epoch_or_expired_pause_is_explicit_not_retimestamped(
    clean_error_record, monkeypatch, now
):
    runtime, bridge, child, _ = clean_error_record
    before = (child / "frames.jsonl").read_bytes()
    monkeypatch.setattr("budao.live_runtime.time.monotonic", lambda: now)
    with pytest.raises(PauseClockExpired, match=PAUSE_CLOCK_DETAIL):
        runtime._resume_ticket(bridge)
    assert runtime.prepare_login(bridge) == {
        "state": "pause_clock_expired",
        "detail": PAUSE_CLOCK_DETAIL,
        "server_auth_proven": False,
    }
    assert (child / "frames.jsonl").read_bytes() == before
