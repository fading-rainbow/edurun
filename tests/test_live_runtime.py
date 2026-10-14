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
