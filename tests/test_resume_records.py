"""续跑证据边界测试；无设备操作，不把错误状态改写为成功。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_resume import proof
from test_runtime import frame, witness

from budao.live_runtime import LiveRuntime, RuntimeSettings, resume_trace_sources


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
    directories = [record(tmp_path, f"native-{i}") for i in range(5)]
    for parent, child in zip(directories, directories[1:], strict=False):
        link(parent, child)
    with pytest.raises(ValueError, match="四代"):
        resume_trace_sources(tmp_path, directories[-1], witness())


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
