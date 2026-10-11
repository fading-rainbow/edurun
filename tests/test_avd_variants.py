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


def test_interactive_api34_memory_override_is_runtime_only():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/Start-ExperimentalAVD.ps1").read_text("utf-8-sig")
    assert "[ValidateSet(0,2048,3072,4096,6144,8192)][int]$MemoryMiB=0" in text
    assert "$Visible -and $Variant -eq 'api34'" in text
    assert "{ 4096 } else { $spec.Ram }" in text
    assert "'-memory',[string]$ram" in text
    assert "Set-Content" not in text


def test_stop_waits_for_owned_process_and_port_without_force_kill():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/Stop-ExperimentalAVD.ps1").read_text("utf-8-sig")
    assert "[regex]::Escape($spec.Name)" in text
    assert "$i -lt 20" in text
    assert "if (-not $ports -and -not $owned)" in text
    assert "Stop-Process" not in text and "taskkill" not in text
