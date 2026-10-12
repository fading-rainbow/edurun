import math
from types import SimpleNamespace

import pytest

from budao.android import AVD_NAME, AndroidBridge


def bridge_with_result(stdout, stderr="", returncode=0):
    def runner(command, **_kwargs):
        arguments = command[3:]
        if arguments[:2] == ["shell", "getprop"]:
            return SimpleNamespace(returncode=0, stdout="1", stderr="")
        if arguments == ["emu", "avd", "name"]:
            return SimpleNamespace(returncode=0, stdout=AVD_NAME + "\nOK", stderr="")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return AndroidBridge("emulator-5580", runner=runner)


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("Starting: Intent {}\nError type 3", ""),
        ("Starting: Intent {}", "Error: Activity class does not exist"),
        ("Error: Activity not started, unable to resolve Intent", ""),
    ],
)
def test_am_start_detects_failure_even_when_adb_returns_zero(stdout, stderr):
    bridge = bridge_with_result(stdout, stderr)
    with pytest.raises(RuntimeError, match="Activity"):
        bridge.run("shell", "am", "start", "-n", "com.example/.MissingActivity")


def test_am_start_retains_non_failure_warnings():
    output = "Starting: Intent {}\nWarning: Activity not started, intent delivered"
    bridge = bridge_with_result(output)
    assert bridge.run("shell", "am", "start", "-n", "com.example/.Activity") == output


@pytest.mark.parametrize("command", ["startservice", "start-foreground-service"])
def test_service_error_with_zero_adb_status_is_not_success_or_echoed(command):
    bridge = bridge_with_result("Error: PRIVATE_SERVICE_PARAMETER permission denied")
    with pytest.raises(RuntimeError) as error:
        bridge.run("shell", "am", command, "-n", "com.example/.MissingService")
    assert "PRIVATE_SERVICE_PARAMETER" not in str(error.value)


def test_read_only_output_is_not_interpreted_as_activity_failure():
    output = "Error: historic log line"
    bridge = bridge_with_result(output)
    assert bridge.run("shell", "logcat", "-d") == output


def test_am_start_error_does_not_echo_authentication_parameters():
    bridge = bridge_with_result("Error: Intent contains PRIVATE_AUTH_PARAMETER")
    with pytest.raises(RuntimeError) as error:
        bridge.run("shell", "am", "start", "-n", "com.example/.Activity")
    assert "PRIVATE_AUTH_PARAMETER" not in str(error.value)


@pytest.mark.parametrize("rate,period", [(25, "40000"), (50, "20000")])
def test_diagnostic_rate_matches_requested_period_and_default(rate, period):
    commands = []

    def runner(command, **_kwargs):
        arguments = command[3:]
        commands.append(arguments)
        output = "1" if arguments[:2] == ["shell", "getprop"] else ""
        if arguments == ["emu", "avd", "name"]:
            output = AVD_NAME + "\nOK"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    bridge = AndroidBridge("emulator-5580", runner=runner)
    bridge.diagnostic_start() if rate == 25 else bridge.diagnostic_start(motion_hz=rate)
    assert commands[-1][:3] == ["shell", "am", "start"]
    assert commands[-1][-3:] == ["--ei", "motion_period_us", period]


@pytest.mark.parametrize("rate", [0, 24, 51, math.inf, math.nan])
def test_bad_diagnostic_rate_does_not_write_or_access_device(rate, monkeypatch):
    bridge = bridge_with_result("")
    monkeypatch.setattr(bridge, "check_device", lambda: pytest.fail("device access"))
    monkeypatch.setattr(bridge, "run", lambda *_a, **_k: pytest.fail("device write"))
    with pytest.raises(ValueError):
        bridge.diagnostic_start(motion_hz=rate)
