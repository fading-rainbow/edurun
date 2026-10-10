"""独立诊断App的实测，绝不启动或提交乐跑。"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .android import DIAGNOSTIC_PACKAGE, AndroidBridge, EmulatorConsole
from .models import GeoPoint


def read_snapshot(bridge: AndroidBridge) -> dict:
    text = bridge.run("shell", "run-as", DIAGNOSTIC_PACKAGE, "cat", "files/diagnostics.json")
    return json.loads(text)


def probe(bridge: AndroidBridge, apk: Path, output: Path, seconds: float = 12) -> dict:
    if not 5 <= seconds <= 60:
        raise ValueError("诊断时长必须为5–60秒")
    bridge.install(apk)
    bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    bridge.diagnostic_start()
    time.sleep(2)
    sent = 0
    last = GeoPoint(lon=0, lat=0)
    start = time.monotonic()
    with EmulatorConsole(bridge) as console:
        available = console.sensors()
        try:
            next_location = start
            while (now := time.monotonic()) - start < seconds:
                elapsed = now - start
                wave = math.sin(elapsed * 2 * math.pi * 2.5)
                console.sensor("acceleration", (2 * wave, 0.5 * wave, 9.81 + wave))
                console.sensor("gyroscope", (0.15 * wave, 0.05 * wave, 0.02 * wave))
                sent += 1
                if now >= next_location:
                    last = GeoPoint(lon=elapsed * 0.00001, lat=0)
                    console.location(last, 1.1132)
                    next_location = now + 1
                deadline = start + sent / 25
                if deadline > time.monotonic():
                    time.sleep(deadline - time.monotonic())
            time.sleep(1)
            snapshot = read_snapshot(bridge)
        finally:
            console.sensor("acceleration", (0, 0, 9.81))
            console.sensor("gyroscope", (0, 0, 0))
    sensors = {item["type"]: item for item in snapshot.get("sensors", [])}
    checks = {}
    for kind, threshold in [(1, 0.5), (4, 0.01)]:
        item = sensors.get(kind, {})
        checks[str(kind)] = (
            item.get("event_count", 0) >= 20
            and max(item.get("observed_axis_spans", [0])) >= threshold
        )
    gps = next((fix for fix in snapshot.get("locations", []) if fix["provider"] == "gps"), None)
    gps_ok = bool(
        gps
        and abs(gps["longitude"] - last.lon) < 0.00002
        and abs(gps["latitude"] - last.lat) < 0.00002
    )
    result = {
        "diagnostic_only": True,
        "real_records_submitted": 0,
        "passed": gps_ok and all(checks.values()),
        "gps_control_passed": gps_ok,
        "motion_control_passed": checks,
        "step_counter_present": 19 in sensors,
        "step_detector_present": 18 in sensors,
        "input_iterations": sent,
        "elapsed_s": round(time.monotonic() - start, 3),
        "console_sensors": available,
        "snapshot": snapshot,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result
