import copy
import csv
import io
import json
import math
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from budao import raw_motion
from budao.android import DIAGNOSTIC_PACKAGE
from budao.driver import MotionClip
from budao.motion_data import SOURCE_PAGE, align_clip, file_hash, read_sensor
from budao.mumu import MuMuBridge
from budao.raw_motion import (
    bounded_clip,
    match_recorded_snapshot,
    prepare_recorded_probe,
    replay_recorded_motion,
    write_motion_probe,
)
from budao.sensor_probe import INJECTION

REQUEST = "c" * 32


def clip(tmp_path, seconds=2):
    path = tmp_path / "test.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
        writer.writerows(
            [i / 50, math.sin(i), 9.8, 0.2, 0.02, 0.01, 0.03] for i in range(seconds * 50 + 1)
        )
    return bounded_clip(MotionClip(path), seconds, "a" * 64)


def packet(clip, *, request=REQUEST, stall=False, period=0.02):
    sent = []
    for index, row in enumerate(clip.rows[:25] if stall else clip.rows):
        for kind, offset in ((1, 1), (4, 4)):
            sent.append(
                {
                    "type": kind,
                    "timestamp_ns": round(12e9 + index * period * 1e9) + kind,
                    "values": list(row[offset : offset + 3]),
                    "source_index": index,
                    "source_t_s": row[0],
                }
            )
    return {
        "elapsed_realtime_ns": 30_000_000_000,
        "raw_motion_probe": {
            "request_id": request,
            "clip_sha256": clip.sha256,
            "diagnostic_only": True,
            "initialized": True,
            "client_closed": True,
            "stall_test": stall,
            "state": "clock_gap" if stall else "completed",
            "source_rows": len(clip.rows),
            "sent_rows": 25 if stall else len(clip.rows),
            "maximum_gap_ns": 250_000_000 if stall else round(period * 1e9),
            "sent_samples": sent,
            "received_samples": copy.deepcopy(sent),
        },
    }


def test_complete_clip_needs_every_source_sample_in_real_callback(tmp_path):
    c = clip(tmp_path)
    p = packet(c)
    assert match_recorded_snapshot(p, c, REQUEST)["clip_received_complete"]
    p["raw_motion_probe"]["received_samples"].pop()
    assert not match_recorded_snapshot(p, c, REQUEST)["clip_received_complete"]


@pytest.mark.parametrize(
    "kind",
    [
        "nonce",
        "source_value",
        "source_index",
        "source_time",
        "timestamp",
        "hash",
        "closed",
        "duplicate",
    ],
)
def test_old_or_modified_proof_cannot_pass(tmp_path, kind):
    c = clip(tmp_path)
    p = packet(c)
    probe = p["raw_motion_probe"]
    if kind == "nonce":
        probe["request_id"] = "d" * 32
    elif kind == "source_value":
        probe["sent_samples"][0]["values"][0] += 1
    elif kind == "source_index":
        probe["sent_samples"][0]["source_index"] = True
    elif kind == "source_time":
        probe["sent_samples"][0]["source_t_s"] = float("nan")
    elif kind == "timestamp":
        probe["sent_samples"][0]["timestamp_ns"] = 30_000_000_001
    elif kind == "hash":
        probe["clip_sha256"] = "wrong"
    elif kind == "closed":
        probe["client_closed"] = False
    else:
        probe["received_samples"] = [probe["received_samples"][0]] * 100
    assert not match_recorded_snapshot(p, c, REQUEST)["clip_received_complete"]


@pytest.mark.parametrize("period", [0.005, 0.025, 0.15])
def test_bursts_low_rate_and_clock_gaps_do_not_pass(tmp_path, period):
    c = clip(tmp_path)
    assert not match_recorded_snapshot(packet(c, period=period), c, REQUEST)[
        "clip_received_complete"
    ]


def test_fixed_stall_proves_stop_not_full_replay(tmp_path):
    c = clip(tmp_path)
    p = packet(c, stall=True)
    result = match_recorded_snapshot(p, c, REQUEST, stall_test=True)
    assert result["clock_gap_stop_verified"] and not result["clip_received_complete"]
    p["raw_motion_probe"]["maximum_gap_ns"] = 20_000_000
    assert not match_recorded_snapshot(p, c, REQUEST, stall_test=True)["clock_gap_stop_verified"]


@pytest.mark.parametrize("seconds", [0, 11, True, 1.5])
def test_bounded_clip_does_not_loop_or_extend(tmp_path, seconds):
    c = clip(tmp_path)
    with pytest.raises(ValueError):
        bounded_clip(MotionClip(tmp_path / "test.csv"), seconds, c.source_sha256)


def source_fixture(tmp_path, monkeypatch):
    directory = tmp_path / ".local/motion"
    directory.mkdir(parents=True)
    archives = {}
    for name, member in (
        ("acc_running_csv.zip", "acc_running_waist.csv"),
        ("gyr_running_csv.zip", "Gyroscope_running_waist.csv"),
    ):
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["id", "attr_time", "attr_x", "attr_y", "attr_z"])
        writer.writerows((i, i * 20, math.sin(i / 50), 0.1, 9.8) for i in range(3301))
        archive = directory / name
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(member, out.getvalue())
        archives[name] = file_hash(archive)
    # Simulated fixtures, not actual publisher files; production hashes remain fixed.
    monkeypatch.setattr(raw_motion, "ARCHIVES", archives)
    values, _ = align_clip(
        read_sensor(directory / "acc_running_csv.zip", "acc_running_waist.csv"),
        read_sensor(directory / "gyr_running_csv.zip", "Gyroscope_running_waist.csv"),
        2,
    )
    csv_file = directory / "recorded.csv"
    with csv_file.open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
        writer.writerows((f"{r[0]:.6f}", *(f"{v:.9f}" for v in r[1:])) for r in values)
    manifest = {
        "dataset": "RealWorld HAR2016",
        "source_page": SOURCE_PAGE,
        "activity_label": "running",
        "body_position": "waist",
        "archive_sha256": archives,
        "csv_file": csv_file.name,
        "csv_sha256": file_hash(csv_file),
        "quality": {"duration_s": 2},
    }
    csv_file.with_suffix(".json").write_text(json.dumps(manifest), "utf-8")
    return csv_file, manifest


def test_source_requires_archive_and_full_csv_reconstruction(tmp_path, monkeypatch):
    source, manifest = source_fixture(tmp_path, monkeypatch)
    assert len(prepare_recorded_probe(tmp_path, source, 1).rows) == 51
    source.write_text(source.read_text().replace("9.800000000", "8.800000000"))
    manifest["csv_sha256"] = file_hash(source)
    source.with_suffix(".json").write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(ValueError, match="来源或文件摘要"):
        prepare_recorded_probe(tmp_path, source, 1)


def test_missing_private_source_and_changed_archive_do_not_reach_device(tmp_path, monkeypatch):
    source, _ = source_fixture(tmp_path, monkeypatch)
    (source.parent / "acc_running_csv.zip").write_bytes(b"changed")
    with pytest.raises(ValueError, match="来源或文件摘要"):
        prepare_recorded_probe(tmp_path, source, 1)
    with pytest.raises(ValueError, match="只能位于"):
        prepare_recorded_probe(tmp_path, tmp_path / "public.csv", 1)


class Device(MuMuBridge):
    def __init__(self, clip, *, fail=False, target=False, restore=True):
        self.clip, self.fail, self.target, self.restore = clip, fail, target, restore
        self.calls = []
        self.mode = "NORMAL"
        self.count = 0
        self.request = ""
        self.stall = False
        self.adb = Path("adb.exe")
        self.serial = "127.0.0.1:16416"
        self.runner = self.execute

    def check_device(self):
        self.calls.append(("guard",))

    def execute(self, args, **kwargs):
        self.calls.append(("payload_stdin", args, kwargs))
        return SimpleNamespace(returncode=0)

    def run(self, *args):
        self.calls.append(args)
        if args == ("shell", "ps", "-A"):
            return "com.lptiyu.tanke" if self.target else ""
        if args[:3] == ("shell", "dumpsys", "sensorservice"):
            if len(args) == 3:
                return "Mode : " + self.mode
            if args[3] == "data_injection":
                self.mode = INJECTION
            elif self.restore:
                self.mode = "NORMAL"
        if args[:3] == ("shell", "am", "start"):
            self.request = args[args.index("motion_probe_request") + 1]
            self.stall = args[-1] == "true"
            if self.fail:
                raise RuntimeError("PRIVATE_TEST_COMMAND")
        if args == ("shell", "dumpsys", "window", "displays"):
            return "mCurrentFocus=Window{123 u0 com.edurun.diagnostic/.DiagnosticActivity}"
        if "sha256sum" in args:
            return self.clip.sha256 + "  files/motion-probe.tmp"
        return ""

    def diagnostic_snapshot(self):
        self.count += 1
        if self.mode == INJECTION:
            return packet(self.clip, request=self.request, stall=self.stall)
        return {
            "injection_api": {"initDataInjection": "available", "injectSensorData": "available"},
            "sensors": [
                {"type": t, "event_count": self.count, "last_timestamp_ns": self.count}
                for t in (1, 4)
            ],
        }

    def diagnostic_start(self, hz):
        self.calls.append(("diagnostic_start", hz))


@pytest.mark.parametrize("stall", [False, True])
def test_orchestration_has_no_target_position_steps_or_upload(tmp_path, stall):
    c = clip(tmp_path)
    d = Device(c)
    r = replay_recorded_motion(d, c, stall_test=stall, sleep=lambda _: None)
    assert r["passed"] and r["normal_stream_resumed"]
    assert r["records_submitted"] == r["formal_runs_started"] == 0
    assert not r["target_steps_verified"] and not r["user_motion_verified"]
    assert not any("location" in call or "com.lptiyu.tanke" in call for call in d.calls)
    enabled = d.calls.index(("shell", "dumpsys", "sensorservice", "enable"))
    assert d.calls[enabled - 1] == ("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    payload = next(call for call in d.calls if call[0] == "payload_stdin")
    assert payload[2]["input"] == c.payload


def test_live_failures_still_clean_up_without_echo(tmp_path):
    c = clip(tmp_path)
    d = Device(c, fail=True)
    r = replay_recorded_motion(d, c, sleep=lambda _: None)
    assert r["mode_restored_normal"] and not r["passed"]
    assert "PRIVATE_TEST_COMMAND" not in str(r)
    r = replay_recorded_motion(Device(c, restore=False), c, sleep=lambda _: None)
    assert r["clip_received_complete"] and not r["mode_restored_normal"] and not r["passed"]
    with pytest.raises(ValueError, match="乐跑进程"):
        replay_recorded_motion(Device(c, target=True), c)
    with pytest.raises(ValueError, match="本项目MuMu"):
        write_motion_probe(object(), c.payload)


def test_native_sender_is_bounded_and_metadata_does_not_block_reception():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android/diagnostic/src/com/edurun/diagnostic/RawMotionProbe.java").read_text(
        "utf-8"
    )
    assert "duration > 10" in source and "lines.length > 502" in source
    assert "gap > 100_000_000L" in source and "delay = 250" in source
    assert "received.length() >= 4096" in source and 'requestId.matches("[a-f0-9]{32}")' in source
    assert "new HandlerThread" in source and "if (!active)" in source
    assert "setAccessible" not in source and "hidden_api_policy" not in source
    builder = (root / "scripts/Build-Diagnostic.ps1").read_text("utf-8")
    assert "$source $motionSource" in builder
