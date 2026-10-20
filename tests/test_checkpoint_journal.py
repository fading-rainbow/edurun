"""Offline synthetic provenance tests, never formal-run acceptance evidence."""

import copy
import json
from pathlib import Path

import pytest
from test_runtime import frame, witness

from budao.checkpoint_journal import confirmed_journal_approach, export_journal_approach
from budao.mumu import MANAGER_SHA256, PLAYER_NAME
from budao.profiles import confirmed_approach


@pytest.fixture
def journal(tmp_path):
    root = tmp_path
    directory = root / ".local/runs" / ("native-" + "a" * 16)
    directory.mkdir(parents=True)
    receipt = root / ".local/setup/mumu-owned-instance.json"
    receipt.parent.mkdir()
    receipt.write_text(
        json.dumps(
            {
                "serial": witness().serial,
                "name": PLAYER_NAME,
                "manager_sha256": MANAGER_SHA256,
                "provisioned_by_this_task": True,
                "old_login_data_imported": False,
            }
        )
    )
    (directory / "start-witness.json").write_text(witness().model_dump_json())
    name = "新测试入口"
    frames = [
        frame(20, assigned=name, distance_m=100),
        frame(21, names=(name,), distance_m=102),
        frame(22, names=(name,), distance_m=104),
    ]
    inputs = [
        {
            "frame_id": f.frame_id,
            "sampled_at": f.sampled_at,
            "android_observed_ns": t,
            "gps_reception": {
                "task_started_ns": 1_000_000_000,
                "first_received_ns": 1_100_000_000,
                "last_received_ns": t - 500_000_000,
                "callback_count": 5,
                "registered": True,
                "provider": "gps",
                "has_accuracy": True,
                "accuracy_m": 2,
                "is_mock": False,
                "latitude": 30,
                "longitude": 120,
            },
            "motion": {"request_id": "b" * 32, "clip_sha256": "c" * 64, "state": "running"},
        }
        for f, t in zip(frames[1:], (3_000_000_000, 4_000_000_000), strict=True)
    ]

    def save():
        (directory / "frames.jsonl").write_text("".join(f.model_dump_json() + "\n" for f in frames))
        (directory / "input-observations.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in inputs)
        )

    save()
    return root, directory, name, frames, inputs, save


def test_export_uses_actual_receiver_and_never_rewrites_logs_or_claims_grade(journal):
    root, directory, name, _frames, _inputs, _save = journal
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    entry, proof = export_journal_approach(root, directory, name)
    assert (entry.approach.lon, entry.approach.lat) == (120, 30)
    assert confirmed_journal_approach(root, entry, proof.model_dump()) is True
    evidence = root / ".local/confirmed-approach.json"
    evidence.write_text(proof.model_dump_json())
    bound = entry.model_copy(update={"confirmation_evidence": evidence.relative_to(root)})
    assert confirmed_approach(root, bound)
    assert not evidence.with_suffix(".xml").exists()  # Never invent an XML snapshot.
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert "record_valid_proven" not in proof.model_dump()


def test_append_does_not_invalidate_prefix_but_original_line_change_does(journal):
    root, directory, name, *_ = journal
    entry, proof = export_journal_approach(root, directory, name)
    path = directory / "frames.jsonl"
    with path.open("ab") as out:
        out.write(b'{"later":true}\n')
    assert confirmed_journal_approach(root, entry, proof.model_dump())
    path.write_bytes(path.read_bytes().replace(b"frame20", b"frameXX", 1))
    with pytest.raises(ValueError, match="前缀"):
        confirmed_journal_approach(root, entry, proof.model_dump())


@pytest.mark.parametrize(
    "fault", ["stale", "no_accuracy", "different_nonce", "row_clock", "no_increment", "wrong_name"]
)
def test_independent_increment_and_reception_checks_reject_incomplete_proof(journal, fault):
    root, directory, name, frames, inputs, save = journal
    if fault == "stale":
        inputs[-1]["gps_reception"]["last_received_ns"] = 1_500_000_000
    elif fault == "no_accuracy":
        inputs[-1]["gps_reception"]["has_accuracy"] = False
    elif fault == "different_nonce":
        inputs[-1]["motion"]["request_id"] = "d" * 32
    elif fault == "row_clock":
        inputs[-1]["sampled_at"] += 1
    elif fault == "no_increment":
        frames[0] = frame(20, names=(name,), distance_m=100)
    else:
        frames[0] = frame(20, assigned="其他入口", distance_m=100)
    save()
    with pytest.raises(ValueError):
        export_journal_approach(root, directory, name)


def test_estimated_marker_cannot_replace_actual_received_approach(journal):
    root, directory, name, *_ = journal
    entry, proof = export_journal_approach(root, directory, name)
    copied = entry.model_copy(update={"approach": entry.approach.model_copy(update={"lat": 30.01})})
    with pytest.raises(ValueError, match="位置"):
        confirmed_journal_approach(root, copied, proof.model_dump())
    broken = copy.deepcopy(proof.model_dump())
    broken["directory"] = Path(".local/runs/../other")
    with pytest.raises(ValueError, match="目录"):
        confirmed_journal_approach(root, entry, broken)
