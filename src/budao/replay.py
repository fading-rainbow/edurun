"""只在独立诊断App验收持续回放；不启动乐跑，不确认打卡或提交记录。"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from .android import DIAGNOSTIC_PACKAGE, AndroidBridge
from .driver import DriveEngine, MotionClip, MotionWorker
from .models import GeoPoint, RouteConfig
from .probe import read_snapshot
from .routes import RoutePlanner, distance


def diagnostic_foreground(activity_dump: str) -> bool:
    matches = re.findall(
        r"^\s*(?:topResumedActivity=|mResumedActivity:)\s*([^\r\n]+)",
        activity_dump,
        re.MULTILINE,
    )
    return len(matches) == 1 and DIAGNOSTIC_PACKAGE + "/" in matches[0]


def replay_diagnostic(
    bridge: AndroidBridge,
    config: RouteConfig,
    output: Path,
    seconds: float = 30,
    motion_csv: Path | None = None,
    motion_hz: float = 25,
) -> dict:
    if not 5 <= seconds <= 900:
        raise ValueError("诊断回放时长必须为5–900秒")
    if not math.isfinite(motion_hz) or not 25 <= motion_hz <= 50:
        raise ValueError("运动频率必须为25–50Hz")
    if config.baseline[0] != config.baseline[-1]:
        raise ValueError("诊断循环基线路线必须闭合")
    planner = RoutePlanner(config)
    motion = MotionClip(motion_csv) if motion_csv else None
    bridge.run("shell", "am", "force-stop", DIAGNOSTIC_PACKAGE)
    bridge.diagnostic_start(motion_hz=motion_hz)
    time.sleep(1)
    engine = DriveEngine(planner, config.baseline[0])
    start = time.monotonic()
    engine.set_path(config.baseline, start, repeat=True)
    # 用户若切换到其他App，停止注入，不去抢回前台。
    with MotionWorker(bridge, engine, motion, motion_hz=motion_hz) as worker:
        while time.monotonic() - start < seconds:
            foreground = bridge.run("shell", "dumpsys", "activity", "activities")
            if not diagnostic_foreground(foreground):
                with worker.lock:
                    engine.pause("诊断App不在前台")
                raise RuntimeError("诊断App不在前台，已停止回放")
            with worker.lock:
                engine.renew(time.monotonic())
            worker.check()
            time.sleep(0.25)
        with worker.lock:
            engine.pause("诊断结束")
        # 留一个GPS周期把速度0及最终坐标发给诊断App。
        time.sleep(1.5)
        state = worker.check()
        snapshot = read_snapshot(bridge)
        iterations = worker.iterations
    worker.check()
    sensors = {s["type"]: s for s in snapshot.get("sensors", [])}
    fixes = snapshot.get("locations", [])
    gps = next((f for f in fixes if f["provider"] == "gps"), None)
    gps_error = (
        distance(
            state.position,
            GeoPoint(lon=gps["longitude"], lat=gps["latitude"]),
        )
        if gps
        else None
    )
    received_motion = all(
        sensors.get(kind, {}).get("event_count", 0) >= 20
        and sensors[kind]["received_hz"] >= 15
        and max(sensors[kind].get("observed_axis_spans", [0])) >= span
        for kind, span in [(1, 0.5), (4, 0.01)]
    )
    rate_target_met = all(
        sensors.get(kind, {}).get("received_hz", 0) >= motion_hz * 0.9 for kind in (1, 4)
    )
    result = {
        "diagnostic_only": True,
        "passed": bool(
            gps_error is not None
            and gps_error < 3
            and received_motion
            and (motion_hz == 25 or rate_target_met)
        ),
        "elapsed_s": round(time.monotonic() - start, 3),
        "driver_travelled_m": round(state.travelled_m, 3),
        "driver_iterations": iterations,
        "gps_final_error_m": gps_error,
        "received_motion": received_motion,
        "motion_source": "csv_unverified_target_acceptance" if motion_csv else "diagnostic_wave",
        "configured_motion_hz": motion_hz,
        "motion_rate_target_met": rate_target_met,
        "step_counter_present": 19 in sensors,
        "step_detector_present": 18 in sensors,
        "real_records_submitted": 0,
        "snapshot": snapshot,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: v for k, v in result.items() if k != "snapshot"}
