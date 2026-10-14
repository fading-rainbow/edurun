import json
from pathlib import Path

import pytest

from budao.live_runtime import LiveRuntime, RuntimeSettings, verify_motion_evidence


def settings():
    return {
        "mode": "known_points_trial",
        "motion_csv": ".local/m.csv",
        "independent_motion_evidence": ".local/proof.json",
        "start_template": ".local/start.png",
    }


def test_explicit_configuration_not_an_api_key_or_arbitrary_runtime(tmp_path):
    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(settings()), "utf-8")
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    assert runtime.settings.mode == "known_points_trial"
    for extra in ({"api_key": "PRIVATE_VALUE"}, {"maximum_s": 901}, {"mode": "unattended"}):
        with pytest.raises(ValueError):
            RuntimeSettings.model_validate(settings() | extra)
    with pytest.raises(ValueError):
        LiveRuntime(tmp_path, Path(".local/p.json"), Path("outside.json"))


def test_missing_or_same_uid_proof_is_not_an_independent_receiver(tmp_path):
    from test_raw_motion import clip

    sample = clip(tmp_path)
    assert not verify_motion_evidence(tmp_path, Path(".local/missing.json"), sample)
    (tmp_path / ".local").mkdir(exist_ok=True)
    proof = {
        "sender": {"package": "com.edurun.diagnostic", "uid": 10041},
        "receiver": {"package": "com.edurun.receiver", "uid": 10041, "capture_done": True},
        "mode_restored_normal": True,
    }
    (tmp_path / ".local/proof.json").write_text(json.dumps(proof), "utf-8")
    assert not verify_motion_evidence(tmp_path, Path(".local/proof.json"), sample)


def test_long_run_requires_explicit_bounded_rollover_not_unlimited_clip():
    assert (
        RuntimeSettings.model_validate(
            settings() | {"maximum_s": 1800, "rollover_enabled": True}
        ).maximum_s
        == 1800
    )
    with pytest.raises(ValueError):
        RuntimeSettings.model_validate(settings() | {"maximum_s": 1801, "rollover_enabled": True})


def test_native_recovery_brings_existing_task_via_exported_launcher_not_run_activity(
    tmp_path, monkeypatch
):
    from budao.auth_ui import LAUNCHER, SPLASH
    from budao.target import RUN_ACTIVITY, TARGET

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    monkeypatch.setattr(runtime, "_resume_ticket", lambda _: (directory, None, None, None, None))

    class Bridge:
        def __init__(self):
            self.started, self.calls = False, []

        def run(self, *args):
            self.calls.append(args)
            if args[:3] == ("shell", "am", "start"):
                self.started = True
                return ""
            if "activities" in args:
                return (
                    "ACTIVITY MANAGER ACTIVITIES\nHist #1: ActivityRecord{a u0 "
                    + TARGET
                    + "/"
                    + RUN_ACTIVITY
                    + " t60}\nHist #0: ActivityRecord{b u0 "
                    + TARGET
                    + "/.activities.main.MainActivity t60}"
                )
            focus = TARGET + "/" + RUN_ACTIVITY if self.started else LAUNCHER + "/.LauncherActivity"
            return "mCurrentFocus=Window{a u0 " + focus + "}"

    b = Bridge()
    assert runtime.prepare_login(b)["state"] == "logged_in_ui"
    starts = [a for a in b.calls if a[:3] == ("shell", "am", "start")]
    assert len(starts) == 1 and TARGET + "/" + SPLASH in starts[0]
    assert TARGET + "/" + RUN_ACTIVITY not in starts[0]


def test_cross_day_resume_evidence_reports_expired_record_in_login_and_preflight(
    tmp_path, monkeypatch
):
    from test_raw_motion import clip

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))

    def expired_ticket(_bridge):
        raise ValueError("暂停会话已跨日")

    monkeypatch.setattr(runtime, "_resume_ticket", expired_ticket)
    monkeypatch.setattr("budao.live_runtime.verify_motion_evidence", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "budao.live_runtime.prepare_recorded_probe", lambda *_a, **_kw: clip(tmp_path)
    )
    monkeypatch.setattr(runtime, "_template", lambda: None)

    from budao.mumu import MuMuBridge

    class DeviceBridge(MuMuBridge):
        def __init__(self):
            pass

        def check_device(self):
            pass

        def run(self, *args):
            if "sensorservice" in args:
                return "Mode: 0"
            return ""

    b = DeviceBridge()
    login_result = runtime.prepare_login(b)
    assert login_result == {
        "state": "expired_record",
        "detail": "过期记录，需用户处理",
        "server_auth_proven": False,
    }

    route_info = {
        "catalog_entries": 1,
        "app_confirmed_approaches": 1,
    }
    preflight_result = runtime.preflight(route_info, b)
    assert preflight_result["runtime"] == ("not_ready", "过期记录，需用户处理")


def test_restored_pause_persisted_and_used_in_build(tmp_path, monkeypatch):
    import time

    from test_runtime import frame, witness

    from budao.runtime import CapturedRunFrame
    from budao.target import RUN_ACTIVITY, TARGET, CheckpointCard

    directory = tmp_path / ".local" / "pending"
    directory.mkdir(parents=True)
    (tmp_path / ".local" / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))

    original_w = witness()
    old_frame = frame(10, control="paused", distance_m=50, elapsed=300)
    ticket = (directory, original_w, old_frame, None, None)
    monkeypatch.setattr(runtime, "_resume_ticket", lambda _: ticket)

    from test_target import hierarchy

    tree = hierarchy(
        tv_distance_value="0.08",
        tv_distance_tip="公里",
        chronometer="00:05:20",
        tv_log_num_value="0",
        tv_log_num_or_calorie_tip="已打卡",
        tv_start_pause_continue_run="继续",
    )

    class EnterBridge:
        def __init__(self):
            self.restored = False

        def run(self, *args):
            focus = (
                f"{TARGET}/{RUN_ACTIVITY}"
                if self.restored
                else f"{TARGET}/.activities.main.MainActivity"
            )
            return f"mCurrentFocus=Window{{a u0 {focus}}}"

    eb = EnterBridge()

    def mock_enter_formal_zero(*_args, on_restored=None, **_kwargs):
        if on_restored:
            on_restored(tree, time.monotonic())
        eb.restored = True
        return 7

    monkeypatch.setattr("budao.live_runtime.enter_formal_zero", mock_enter_formal_zero)
    monkeypatch.setattr("budao.live_runtime.target_task_tops", lambda _: [])
    test_card = CheckpointCard(number=1, name="点1", state="assigned", distance_m=10)
    monkeypatch.setattr("budao.live_runtime.parse_checkpoint_cards", lambda _: (test_card,))

    runtime.prepare_login(eb)

    # Check normal-restored-pause.json was written
    restored_file = directory / "normal-restored-pause.json"
    assert restored_file.exists()
    restored_frame = CapturedRunFrame.model_validate_json(restored_file.read_text("utf-8"))
    assert restored_frame.inspection.distance_m == 80
    assert restored_frame.inspection.elapsed_s == 320
    assert runtime._restored_pause[0] == original_w.request_id
    assert runtime._restored_pause[1].frame_id == restored_frame.frame_id
