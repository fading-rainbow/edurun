import json
import sys

import pytest

from budao.android import AVD_NAME
from budao.cli import main
from budao.startup_compat import POLICY_KEY, configure_startup_compat


class FakeBridge:
    avd_name = AVD_NAME
    serial = "emulator-5580"

    def __init__(self, original="null", sdk="34", reject=False, fail_readback=False):
        self.policy = original
        self.sdk = sdk
        self.reject = reject
        self.fail_readback = fail_readback
        self.calls = []

    def check_device(self):
        self.calls.append(("guard",))
        if self.reject:
            raise ValueError("foreign device")

    def run(self, *args):
        self.calls.append(args)
        if args == ("shell", "getprop", "ro.build.version.sdk"):
            return self.sdk
        if args == ("shell", "settings", "get", "global", POLICY_KEY):
            return self.policy
        if args[:4] == ("shell", "settings", "put", "global"):
            assert args[4] == POLICY_KEY
            if not self.fail_readback:
                self.policy = args[5]
            return ""
        if args == ("shell", "settings", "delete", "global", POLICY_KEY):
            self.policy = "null"
            return ""
        raise AssertionError(f"Unexpected device operation: {args}")

    @property
    def writes(self):
        return [
            x
            for x in self.calls
            if x[:3] in {("shell", "settings", "put"), ("shell", "settings", "delete")}
        ]


@pytest.mark.parametrize("original", ["null", "-1", "0", "1", "2"])
def test_apply_preserves_first_policy_and_restore(original, tmp_path):
    bridge = FakeBridge(original)
    state = tmp_path / "private" / "policy.json"
    result = configure_startup_compat(bridge, state)
    assert bridge.calls[0] == ("guard",)
    assert result["after"] == "1"
    assert not result["app_data_cleared"] and not result["app_restarted"]
    assert not result["app_launch_verified"]
    baseline = state.read_bytes()
    configure_startup_compat(bridge, state)
    assert state.read_bytes() == baseline
    restored = configure_startup_compat(bridge, state, restore=True)
    assert restored["after"] == original
    assert json.loads(state.read_text())["original"] == original
    if original == "null":
        assert bridge.writes[-1] == ("shell", "settings", "delete", "global", POLICY_KEY)


@pytest.mark.parametrize("sdk", ["28", "", "invalid"])
def test_unsupported_sdk_fails_without_backup_or_write(sdk, tmp_path):
    bridge = FakeBridge(sdk=sdk)
    path = tmp_path / "state.json"
    with pytest.raises(ValueError):
        configure_startup_compat(bridge, path)
    assert not bridge.writes and not path.exists()


@pytest.mark.parametrize("avd", ["MonForee_API28", "arbitrary"])
def test_legacy_or_foreign_avd_refused(avd, tmp_path):
    bridge = FakeBridge()
    bridge.avd_name = avd
    with pytest.raises(ValueError):
        configure_startup_compat(bridge, tmp_path / "state.json")
    assert bridge.calls == [("guard",)]


def test_device_guard_runs_before_file_access(tmp_path):
    bridge = FakeBridge(reject=True)
    path = tmp_path / "state.json"
    with pytest.raises(ValueError, match="foreign device"):
        configure_startup_compat(bridge, path)
    assert bridge.calls == [("guard",)] and not path.exists()


@pytest.mark.parametrize("original", ["3", "arbitrary", "1; echo unsafe"])
def test_unknown_current_policy_not_overwritten(original, tmp_path):
    bridge = FakeBridge(original)
    path = tmp_path / "state.json"
    with pytest.raises(ValueError):
        configure_startup_compat(bridge, path)
    assert not bridge.writes and not path.exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"schema": 2},
        {"schema": True},
        {"avd": "other"},
        {"serial": "emulator-5584"},
        {"original": "1; unsafe"},
        {"original": []},
        {"original": None},
    ],
)
def test_invalid_or_other_device_backup_fails_before_write(patch, tmp_path):
    state = {"schema": 1, "avd": AVD_NAME, "serial": "emulator-5580", "original": "null"}
    state.update(patch)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    bridge = FakeBridge()
    with pytest.raises(ValueError):
        configure_startup_compat(bridge, path, restore=True)
    assert not bridge.writes


def test_missing_backup_not_treated_as_default(tmp_path):
    bridge = FakeBridge("1")
    path = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        configure_startup_compat(bridge, path, restore=True)
    assert not bridge.writes and not path.exists()


def test_failed_device_readback_retains_baseline(tmp_path):
    bridge = FakeBridge(fail_readback=True)
    path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="复核失败"):
        configure_startup_compat(bridge, path)
    assert json.loads(path.read_text())["original"] == "null"


@pytest.mark.parametrize("flags", [[], ["--apply", "--restore"]])
def test_cli_requires_exactly_one_explicit_action(flags, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["budao", "startup-compat", "--serial", "emulator-5580", *flags]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_cli_apply_and_restore_use_private_journal(tmp_path, monkeypatch, capsys):
    bridge = FakeBridge()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("budao.cli.AndroidBridge", lambda *a, **kw: bridge)
    for flag, expected in [("--apply", "1"), ("--restore", "null")]:
        monkeypatch.setattr(
            sys, "argv", ["budao", "startup-compat", "--serial", bridge.serial, flag]
        )
        assert main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["after"] == expected
        assert not result["app_launch_verified"]
    assert (tmp_path / ".local/setup/startup-compat" / f"{AVD_NAME}.json").exists()
