"""Export historical App-confirmed approaches, not new GPS/identity/grade evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import GeoPoint, StrictModel
from .routes import distance


class JournalPrefix(StrictModel):
    size: int = Field(ge=1, le=4 * 1024 * 1024, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class JournalApproach(StrictModel):
    kind: Literal["journal_checkpoint_v1"] = "journal_checkpoint_v1"
    directory: Path
    frames: JournalPrefix
    inputs: JournalPrefix
    start_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    frame_ids: tuple[str, str, str]


def _directory(root, value):
    from .profiles import private_file

    directory = private_file(root, value, must_exist=False)
    if directory.parent != root.resolve() / ".local/runs" or not re.fullmatch(
        r"native-[a-f0-9]{16}", directory.name
    ):
        raise ValueError("打卡来源必须是本项目原生运行目录")
    return directory


def _read(directory, name, prefix=None, *, maximum=4 * 1024 * 1024):
    path = directory / name
    if path.resolve() != path or not path.is_file():
        raise ValueError("打卡原始日志缺失或被重定向")
    size = prefix.size if prefix is not None else path.stat().st_size
    if not 0 < size <= maximum:
        raise ValueError("打卡来源超过有界大小")
    with path.open("rb") as stream:
        data = stream.read(size)
    if len(data) != size or (prefix and hashlib.sha256(data).hexdigest() != prefix.sha256):
        raise ValueError("打卡原始日志前缀与导出凭据不符")
    return data


def _records(data):
    if not data.endswith(b"\n"):
        raise ValueError("打卡日志前缀不是完整记录")
    return [json.loads(line) for line in data.decode("utf-8-sig").splitlines()]


def _received_position(value, frame):
    g, m = value["gps_reception"], value["motion"]
    observed = value["android_observed_ns"]
    clocks = [g["task_started_ns"], g["first_received_ns"], g["last_received_ns"], observed]
    if (
        value["sampled_at"] != frame.sampled_at
        or value["frame_id"] != frame.frame_id
        or not re.fullmatch(r"[a-f0-9]{32}", m["request_id"])
        or not re.fullmatch(r"[a-f0-9]{64}", m["clip_sha256"])
        or m["state"] != "running"
        or g["registered"] is not True
        or g["provider"] != "gps"
        or g["has_accuracy"] is not True
        or type(g["is_mock"]) is not bool
        or type(g["callback_count"]) is not int
        or g["callback_count"] < 2
        or any(type(v) is not int or v <= 0 for v in clocks)
        or not clocks[0] <= clocks[1] < clocks[2] < observed + 10_000_000
        or not -10_000_000 < observed - clocks[2] <= 2_000_000_000
        or any(
            type(v) not in (float, int) or not math.isfinite(v)
            for v in (g["latitude"], g["longitude"], g["accuracy_m"])
        )
        or not 0 <= g["accuracy_m"] <= 5
    ):
        raise ValueError("历史打卡帧没有对应的新鲜实收定位")
    return GeoPoint(lat=g["latitude"], lon=g["longitude"])


def confirmed_journal_approach(root: Path, entry, value):
    from .profiles import private_file
    from .resume import resume_completed_names
    from .runtime import CapturedRunFrame, StartWitness
    from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET

    proof = JournalApproach.model_validate(value)
    directory = _directory(root, proof.directory)
    start = _read(directory, "start-witness.json", maximum=128 * 1024)
    if hashlib.sha256(start).hexdigest() != proof.start_sha256:
        raise ValueError("打卡来源的原生开始凭据改变")
    witness = StartWitness.model_validate_json(start)
    from .mumu import MANAGER_SHA256, PLAYER_NAME

    receipt_file = private_file(root, Path(".local/setup/mumu-owned-instance.json"))
    if receipt_file.stat().st_size > 128 * 1024:
        raise ValueError("独立设备来源凭据超过限制")
    receipt = json.loads(receipt_file.read_text("utf-8-sig"))
    if (
        witness.serial != receipt["serial"]
        or receipt["name"] != PLAYER_NAME
        or receipt["manager_sha256"] != MANAGER_SHA256
        or not re.fullmatch(r"127\.0\.0\.1:[1-9]\d{3,4}", witness.serial)
        or receipt["provisioned_by_this_task"] is not True
        or receipt["old_login_data_imported"] is not False
    ):
        raise ValueError("打卡来源不是本任务独立模拟器")
    frames = _records(_read(directory, "frames.jsonl", proof.frames))
    ids = [row["frame_id"] for row in frames]
    indexes = [ids.index(fid) for fid in proof.frame_ids]
    if (
        len(set(proof.frame_ids)) != 3
        or any(ids.count(fid) != 1 for fid in proof.frame_ids)
        or indexes != list(range(indexes[0], indexes[0] + 3))
    ):
        raise ValueError("打卡增量必须是原日志相邻三帧")
    selected = [CapturedRunFrame.model_validate(frames[i]) for i in indexes]
    before, first, second = selected
    n = first.inspection.completed_checkpoints
    if type(n) is not int or not 1 <= n <= 3:
        raise ValueError("打卡计数不在允许范围")
    names = [resume_completed_names(f.inspection) for f in selected]
    if (
        len(names[0]) != n - 1
        or names[1] != names[0] + (entry.name,)
        or names[2] != names[1]
        or before.inspection.checkpoint_cards[n - 1].state != "assigned"
        or before.inspection.checkpoint_cards[n - 1].name != entry.name
        or witness.second_running.sampled_at >= before.sampled_at
    ):
        raise ValueError("原App没有分配点到两帧实际完成的增量")
    for frame in selected:
        v = frame.inspection
        if (
            (v.package, v.activity, v.version) != (TARGET, RUN_ACTIVITY, "4.3.0")
            or v.state != "metrics"
            or v.run_control_state != "running"
            or v.gps_label != GPS_READY_LABEL
            or v.elapsed_s is None
            or v.distance_m is None
            or any(i != "start_controls_and_current_record_identity_unverified" for i in v.issues)
        ):
            raise ValueError("打卡增量不是同一正常运行页")
    for a, b in zip(selected, selected[1:], strict=False):
        if not (
            0 < b.sampled_at - a.sampled_at <= 2
            and 0 <= b.inspection.elapsed_s - a.inspection.elapsed_s <= 3
            and 0 <= b.inspection.distance_m - a.inspection.distance_m <= 15
        ):
            raise ValueError("打卡帧时间或里程不连续")
    rows = _records(_read(directory, "input-observations.jsonl", proof.inputs))
    inputs = []
    for frame in (first, second):
        matched = [row for row in rows if row["frame_id"] == frame.frame_id]
        if len(matched) != 1:
            raise ValueError("打卡帧的接收观察重复或缺失")
        inputs.append(matched[0])
    a, b = inputs
    positions = [
        _received_position(row, frame) for row, frame in zip(inputs, (first, second), strict=True)
    ]
    if (
        a["motion"]["request_id"] != b["motion"]["request_id"]
        or a["motion"]["clip_sha256"] != b["motion"]["clip_sha256"]
        or a["gps_reception"]["task_started_ns"] != b["gps_reception"]["task_started_ns"]
        or not a["android_observed_ns"] < b["android_observed_ns"]
        or a["gps_reception"]["last_received_ns"] > b["gps_reception"]["last_received_ns"]
        or a["gps_reception"]["callback_count"] > b["gps_reception"]["callback_count"]
        or distance(*positions) > 10
        or distance(positions[-1], entry.approach) > 0.05
    ):
        raise ValueError("打卡接收任务或接近位置不一致，不使用模型估计位置")
    return True


def export_journal_approach(root: Path, directory: Path, name: str):
    """Pure offline export; never edits source logs, catalog, device or a run."""
    from .profiles import CheckpointEntry

    directory = _directory(root, directory)
    frames, inputs = (
        _read(directory, filename) for filename in ("frames.jsonl", "input-observations.jsonl")
    )
    records, received = _records(frames), _records(inputs)
    for i in range(1, len(records) - 1):
        before, first, second = records[i - 1 : i + 2]
        cards = first["inspection"].get("checkpoint_cards", [])
        n = first["inspection"].get("completed_checkpoints")
        if (
            type(n) is not int
            or not 1 <= n <= 3
            or len(cards) < n
            or cards[n - 1].get("name") != name
            or before["inspection"].get("completed_checkpoints") != n - 1
            or second["inspection"].get("completed_checkpoints") != n
        ):
            continue
        matches = [row for row in received if row["frame_id"] == second["frame_id"]]
        if len(matches) != 1:
            raise ValueError("实际完成帧的接收观察重复或缺失")
        row = matches[0]
        entry = CheckpointEntry(
            name=name,
            approach=GeoPoint(
                lat=row["gps_reception"]["latitude"], lon=row["gps_reception"]["longitude"]
            ),
        )
        proof = JournalApproach(
            directory=directory.relative_to(root.resolve()),
            frames=JournalPrefix(size=len(frames), sha256=hashlib.sha256(frames).hexdigest()),
            inputs=JournalPrefix(size=len(inputs), sha256=hashlib.sha256(inputs).hexdigest()),
            start_sha256=hashlib.sha256(
                _read(directory, "start-witness.json", maximum=128 * 1024)
            ).hexdigest(),
            frame_ids=(before["frame_id"], first["frame_id"], second["frame_id"]),
        )
        confirmed_journal_approach(root, entry, proof.model_dump())
        return entry, proof
    raise ValueError("没有本次点名的连续实际打卡增量")
