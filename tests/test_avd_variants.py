from types import SimpleNamespace

import pytest

from budao.android import AVD_NAME, PROJECT_AVD_NAMES, AndroidBridge


@pytest.mark.parametrize("name", sorted(PROJECT_AVD_NAMES))
def test_explicitly_named_project_devices_only(name):
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else name + "\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    assert AndroidBridge("emulator-5582", avd_name=name, runner=runner).avd_name == name


def test_variant_is_not_auto_discovered_or_silently_switched():
    def runner(command, **_kwargs):
        output = "1" if "getprop" in command else "BudaoRun_API36_Play\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with pytest.raises(ValueError, match=AVD_NAME):
        AndroidBridge("emulator-5582", runner=runner)


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
