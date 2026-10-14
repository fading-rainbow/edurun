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
