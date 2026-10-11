from pathlib import Path
from types import SimpleNamespace

import pytest

from budao.android import AVD_NAME, PROJECT_AVD_NAMES, AndroidBridge


@pytest.mark.parametrize("name", sorted(PROJECT_AVD_NAMES))
def test_explicitly_named_project_devices_only(name):
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else name + "\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    assert AndroidBridge("emulator-5582", avd_name=name, runner=runner).avd_name == name


@pytest.mark.parametrize("name", ["BudaoRun_API36_Play", "BudaoRun_API30_Play"])
def test_variant_is_not_auto_discovered_or_silently_switched(name):
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else name + "\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with pytest.raises(ValueError, match=AVD_NAME):
        AndroidBridge("emulator-5582", runner=runner)


def test_named_devices_are_fixed_and_default_is_unchanged():
    assert AVD_NAME == "BudaoRun_API34_Play"
    assert PROJECT_AVD_NAMES == {
        "BudaoRun_API34_Play",
        "BudaoRun_API36_Play",
        "BudaoRun_API30_Play",
    }


@pytest.mark.parametrize(
    "script",
    ["New-ExperimentalAVD.ps1", "Start-ExperimentalAVD.ps1", "Stop-ExperimentalAVD.ps1"],
)
def test_device_scripts_keep_explicit_selection_and_api34_default(script):
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / script).read_text("utf-8-sig")
    assert "ValidateSet('api34','api36','api30')" in text
    assert "$Variant='api34'" in text
    assert "-wipe-data" not in text


@pytest.mark.parametrize("name", ["MonForee_API28", "ArbitraryName", "BudaoRun_API99_Play"])
def test_caller_cannot_expand_whitelist(name):
    with pytest.raises(ValueError):
        AndroidBridge("emulator-5582", avd_name=name)


def test_empty_device_name_fails_closed():
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with pytest.raises(ValueError, match="未读取"):
        AndroidBridge("emulator-5582", runner=runner)
