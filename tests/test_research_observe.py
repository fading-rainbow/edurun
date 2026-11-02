import json

import pytest

from budao.research_observe import (
    MAX_LOG_BYTES,
    PACKAGE,
    observe_startup,
    summarize_startup_log,
)


def line(message, *, epoch=1800000000, pid=42):
    return f"{epoch}.000  {pid}  77 I Research: {message}"


def summarize(raw):
    return summarize_startup_log(raw, pid=42, device_epoch=1800000001, age_seconds=60)


def test_secrets_paths_headers_and_payloads_never_returned():
    raw = line(
        "Authorization: Bearer private-marker "
        "https://account:password@api2.lptiyu.com/person/student-name?session=private-marker "
        "face_payload=private-image-marker coordinate=private-position-marker"
    )
    result = summarize(raw)
    text = json.dumps(result)
    assert result["policy_hostname_log_candidates"] == {"api2.lptiyu.com": 1}
    for secret in [
        "private-marker",
        "student-name",
        "password",
        "private-image",
        "private-position",
    ]:
        assert secret not in text
    assert result["http_requests_proven"] is False


def test_host_allowlist_does_not_accept_lookalikes_or_unknown_subdomains():
    raw = line(
        "https://api2.lptiyu.com.evil.invalid/x https://unique-user.lptiyu.com/x "
        "https://192.0.2.10/x https://API2.LPTIYU.COM./x"
    )
    result = summarize(raw)
    assert result["policy_hostname_log_candidates"] == {"api2.lptiyu.com": 1}
    assert result["unlisted_project_hostname_occurrences"] == 1
    assert result["other_hostname_occurrences"] == 2
    assert "unique-user" not in json.dumps(result)


def test_only_recent_selected_pid_lines_contribute():
    raw = "\n".join(
        [
            "--------- beginning of main",
            line("FATAL EXCEPTION", pid=99),
            line("FATAL EXCEPTION", epoch=1799999900),
            line("FATAL EXCEPTION", epoch=1800000002),
            line("Permission denied student-name"),
        ]
    )
    result = summarize(raw)
    assert result["matched_lines"] == 1
    assert result["excluded_lines"] == 4
    assert result["symptom_counts"] == {
        "native_load_error": 0,
        "fatal_exception": 0,
        "permission_denied": 1,
    }
    assert "student-name" not in json.dumps(result)


def test_empty_log_is_not_successful_runtime_or_network_proof():
    result = summarize("")
    assert result["matched_lines"] == 0
    assert result["policy_hostname_log_candidates"] == {}
    assert result["http_requests_proven"] is False


def test_real_android_epoch_column_padding_is_supported():
    result = summarize("         " + line("https://api2.lptiyu.com/private?session=secret"))
    assert result["matched_lines"] == 1
    assert result["unparsed_lines"] == 0
    assert result["format_coverage"] == "complete-within-returned-lines"
    assert result["policy_hostname_log_candidates"] == {"api2.lptiyu.com": 1}


def test_unknown_log_format_is_reported_as_partial_not_empty_evidence():
    result = summarize("unrecognized private-message")
    assert result["unparsed_lines"] == 1
    assert result["format_coverage"] == "partial"
    assert "private-message" not in json.dumps(result)


def test_native_error_keeps_only_known_loader_basename_not_paths_or_credentials():
    raw = line(
        'dlopen failed: library "libkadp_x86.so" not found '
        "/private-student-path auth=private-credential libprivate_component.so"
    )
    result = summarize(raw)
    assert result["known_loader_library_error_log_candidates"] == {"libkadp_x86.so": 1}
    assert result["native_load_error_kind_counts"] == {"not_found": 1}
    assert "private-student" not in json.dumps(result)
    assert "private-credential" not in json.dumps(result)
    assert "libprivate_component" not in json.dumps(result)


def test_oversized_input_is_rejected_without_echoing_it():
    with pytest.raises(ValueError, match="2MiB"):
        summarize("x" * (MAX_LOG_BYTES + 1))


class Bridge:
    def __init__(self, *, change_pid=False, wrong_name=False, epoch="1800000001"):
        self.commands = []
        self.change_pid = change_pid
        self.wrong_name = wrong_name
        self.epoch = epoch
        self.pid_reads = 0

    def run(self, *args):
        self.commands.append(args)
        if args == ("shell", "pidof", PACKAGE):
            self.pid_reads += 1
            return "43" if self.change_pid and self.pid_reads == 2 else "42"
        if args[:2] == ("shell", "cat"):
            return "another.package\x00" if self.wrong_name else PACKAGE + "\x00"
        if args == ("shell", "date", "+%s"):
            return self.epoch
        if args[0] == "logcat":
            return line("https://api2.lptiyu.com/path?credential=hidden")
        raise AssertionError(args)


def test_observer_is_read_only_and_checks_identity_twice():
    bridge = Bridge()
    result = observe_startup(bridge, lines=100, age_seconds=60)
    assert bridge.pid_reads == 2
    assert result["app_launch_requested"] is False
    assert result["device_mutation_requested"] is False
    assert result["raw_logs_saved"] is False
    assert bridge.commands[3] == (
        "logcat",
        "-d",
        "-b",
        "main",
        "--pid",
        "42",
        "-v",
        "epoch",
        "-t",
        "100",
    )
    assert all(args[0] in {"shell", "logcat"} for args in bridge.commands)


def test_restarted_process_discards_observation():
    with pytest.raises(RuntimeError, match="进程发生变化"):
        observe_startup(Bridge(change_pid=True))


def test_wrong_pid_identity_never_reads_logs():
    bridge = Bridge(wrong_name=True)
    with pytest.raises(RuntimeError, match="PID归属"):
        observe_startup(bridge)
    assert not any(args[0] == "logcat" for args in bridge.commands)


def test_bad_device_time_never_reads_logs():
    bridge = Bridge(epoch="unparseable")
    with pytest.raises(RuntimeError, match="设备时间"):
        observe_startup(bridge)
    assert not any(args[0] == "logcat" for args in bridge.commands)


@pytest.mark.parametrize("lines", [0, 501, True])
def test_invalid_limit_never_contacts_device(lines):
    bridge = Bridge()
    with pytest.raises(ValueError, match="日志行数"):
        observe_startup(bridge, lines=lines)
    assert bridge.commands == []


@pytest.mark.parametrize("age", [0, 1801, True])
def test_invalid_age_never_contacts_device(age):
    bridge = Bridge()
    with pytest.raises(ValueError, match="时间范围"):
        observe_startup(bridge, age_seconds=age)
    assert bridge.commands == []
