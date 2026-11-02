"""Bounded startup-log summaries; raw messages never enter the returned report."""

from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urlsplit

from .mumu import MuMuBridge

PACKAGE = "com.lptiyu.tanke"
# Derived from this APK's network-security resource, not observed HTTP traffic.
POLICY_HOSTS = frozenset({"api2.lptiyu.com"})
MAX_LOG_BYTES = 2 * 1024 * 1024
_LINE = re.compile(r"^\s*(\d{9,}(?:\.\d+)?)\s+(\d+)\s+(\d+)\s+[VDIWEFS]\s+[^:]+:\s?(.*)$")
_URL = re.compile(r"https?://[^\s\"'<>()]+", re.IGNORECASE)
_LOADER_LIBRARY = re.compile(r"\blibkadp(?:_x86)?\.so\b")
_SYMPTOMS = {
    "native_load_error": re.compile(r"dlopen failed|UnsatisfiedLinkError", re.IGNORECASE),
    "fatal_exception": re.compile(r"FATAL EXCEPTION", re.IGNORECASE),
    "permission_denied": re.compile(r"Permission denied", re.IGNORECASE),
}


def summarize_startup_log(raw: str, *, pid: int, device_epoch: int, age_seconds: int) -> dict:
    """Project only fixed counters and allowlisted hosts; omit paths, queries and messages."""
    if type(pid) is not int or pid < 1:
        raise ValueError("无效目标PID")
    if type(device_epoch) is not int or device_epoch < 1:
        raise ValueError("未取得有效设备时间")
    if type(age_seconds) is not int or not 1 <= age_seconds <= 1800:
        raise ValueError("日志时间范围必须在1–1800秒内")
    if len(raw.encode("utf-8")) > MAX_LOG_BYTES:
        raise ValueError("日志返回超过2MiB，拒绝生成摘要")

    matched_lines = 0
    excluded_lines = 0
    unparsed_lines = 0
    wrong_pid_lines = 0
    outside_window_lines = 0
    header_lines = 0
    hosts: Counter[str] = Counter()
    other_hosts = 0
    other_project_hosts = 0
    symptoms = dict.fromkeys(_SYMPTOMS, 0)
    loader_libraries: Counter[str] = Counter()
    native_error_kinds: Counter[str] = Counter()
    for line in raw.splitlines():
        if not line.strip() or line.startswith("--------- beginning of "):
            excluded_lines += 1
            header_lines += 1
            continue
        match = _LINE.match(line)
        if match is None:
            excluded_lines += 1
            unparsed_lines += 1
            continue
        if int(match[2]) != pid:
            excluded_lines += 1
            wrong_pid_lines += 1
            continue
        epoch = float(match[1])
        if not math.isfinite(epoch) or not 0 <= device_epoch - epoch <= age_seconds:
            excluded_lines += 1
            outside_window_lines += 1
            continue
        matched_lines += 1
        message = match[4]
        for name, pattern in _SYMPTOMS.items():
            symptoms[name] += bool(pattern.search(message))
        if _SYMPTOMS["native_load_error"].search(message):
            loader_libraries.update(_LOADER_LIBRARY.findall(message))
            kind = "not_found" if "not found" in message.lower() else "other"
            native_error_kinds[kind] += 1
        for url in _URL.findall(message):
            try:
                host = (urlsplit(url).hostname or "").lower().rstrip(".")
            except ValueError:
                continue
            if host in POLICY_HOSTS:
                hosts[host] += 1
            elif host == "lptiyu.com" or host.endswith(".lptiyu.com"):
                other_project_hosts += 1
            else:
                other_hosts += 1

    return {
        "package": PACKAGE,
        "pid": pid,
        "device_epoch": device_epoch,
        "age_seconds": age_seconds,
        "matched_lines": matched_lines,
        "excluded_lines": excluded_lines,
        "unparsed_lines": unparsed_lines,
        "wrong_pid_lines": wrong_pid_lines,
        "outside_window_lines": outside_window_lines,
        "header_lines": header_lines,
        "format_coverage": "partial" if unparsed_lines else "complete-within-returned-lines",
        "policy_hostname_log_candidates": dict(sorted(hosts.items())),
        "unlisted_project_hostname_occurrences": other_project_hosts,
        "other_hostname_occurrences": other_hosts,
        "symptom_counts": symptoms,
        "known_loader_library_error_log_candidates": dict(sorted(loader_libraries.items())),
        "native_load_error_kind_counts": dict(sorted(native_error_kinds.items())),
        "raw_logs_saved": False,
        "raw_messages_returned": False,
        "url_paths_or_query_values_returned": False,
        "http_requests_proven": False,
        "limitations": [
            "Only the selected PID's bounded main-buffer log window was inspected.",
            "A URL in a log does not prove transmission, receipt or server acceptance.",
            "Unlisted hostnames and all message/path/query/header/body values are omitted.",
            "Missing log evidence does not prove absence of network or native activity.",
        ],
    }


def _main_pid(bridge: MuMuBridge) -> int:
    value = bridge.run("shell", "pidof", PACKAGE).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise RuntimeError("未取得唯一目标主进程；不启动App或猜测PID")
    pid = int(value)
    name = bridge.run("shell", "cat", f"/proc/{pid}/cmdline").rstrip("\x00")
    if name != PACKAGE:
        raise RuntimeError("PID归属不是目标主进程，丢弃观察")
    return pid


def observe_startup(bridge: MuMuBridge, *, lines: int = 200, age_seconds: int = 600) -> dict:
    """Read an already-running owned target; no start, stop, clear, root or interception."""
    if type(lines) is not int or not 1 <= lines <= 500:
        raise ValueError("日志行数必须在1–500以内")
    if type(age_seconds) is not int or not 1 <= age_seconds <= 1800:
        raise ValueError("日志时间范围必须在1–1800秒内")
    pid = _main_pid(bridge)
    epoch = bridge.run("shell", "date", "+%s")
    if not re.fullmatch(r"[1-9][0-9]{0,12}", epoch):
        raise RuntimeError("设备时间不可解析；不读取日志")
    raw = bridge.run(
        "logcat", "-d", "-b", "main", "--pid", str(pid), "-v", "epoch", "-t", str(lines)
    )
    if _main_pid(bridge) != pid:
        raise RuntimeError("读取时主进程发生变化，丢弃观察")
    result = summarize_startup_log(raw, pid=pid, device_epoch=int(epoch), age_seconds=age_seconds)
    result.update(
        {
            "maximum_lines": lines,
            "process_identity_checked_before_and_after": True,
            "app_launch_requested": False,
            "device_mutation_requested": False,
        }
    )
    return result
