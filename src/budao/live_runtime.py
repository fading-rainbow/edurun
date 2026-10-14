"""本机显式装配原生入口的有界运行。未知点暂停，不将草案标为全场地验收。"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from .card_observer import NamedCardObserver
from .driver import DriveEngine
from .finish_connector import FinishConnector
from .foreground_motion import ForegroundMotionClient
from .models import GeoPoint, RouteConfig, StrictModel
from .mumu import MuMuBridge
from .mumu_driver import MuMuPositionWorker
from .portal import enter_formal_zero
from .profiles import RunProfile, _load, preflight_profile, private_file
from .raw_motion import match_recorded_snapshot, prepare_recorded_probe
from .routes import RoutePlanner
from .runtime import RuntimeDispatcher
from .sensor_probe import NORMAL, sensor_mode
from .session_runner import SessionRunner
from .start_button import StartButtonTemplate
from .start_connector import StartConnector


class RuntimeSettings(StrictModel):
    mode: Literal["known_points_trial"]
    motion_csv: Path
    motion_evidence_csv: Path | None = None
    independent_motion_evidence: Path
    start_template: Path
    maximum_s: int = Field(default=900, ge=30, le=900)
    normal_save_authorized: bool = False


def verify_motion_evidence(root: Path, path: Path, clip) -> bool:
    try:
        source = private_file(root, path)
        if source.stat().st_size > 4 * 1024 * 1024:
            return False
        value = json.loads(source.read_text("utf-8-sig"))
        tx, rx = value["sender"], value["receiver"]
        if (
            tx["package"] != "com.edurun.diagnostic"
            or rx["package"] != "com.edurun.receiver"
            or type(tx["uid"]) is not int
            or type(rx["uid"]) is not int
            or tx["uid"] == rx["uid"]
            or rx["capture_done"] is not True
            or value["mode_restored_normal"] is not True
        ):
            return False
        combined = {
            "elapsed_realtime_ns": rx["elapsed_realtime_ns"],
            "raw_motion_probe": tx["raw_motion_probe"]
            | {"received_samples": rx["received_samples"]},
        }
        return match_recorded_snapshot(combined, clip, tx["raw_motion_probe"]["request_id"])[
            "clip_received_complete"
        ]
    except (ValueError, OSError, KeyError, TypeError):
        return False


class JournalConnector(StartConnector):
    def __init__(self, *args, evidence: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = evidence

    def capture(self, **kwargs):
        value = super().capture(**kwargs)
        (self.evidence / "current.xml").write_text(value.hierarchy, "utf-8")
        with (self.evidence / "frames.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(value.frame.model_dump_json() + "\n")
        if value.screenshot is not None:
            value.screenshot.save(self.evidence / "current.png")
        return value

    def begin_once(self, *args, **kwargs):
        witness = super().begin_once(*args, **kwargs)
        (self.evidence / "start-witness.json").write_text(
            witness.model_dump_json(indent=2), "utf-8"
        )
        return witness


class JournalRunner(SessionRunner):
    def run(self, authorize, publish, cancel=None):
        def forward(report):
            stored = {k: v for k, v in report.items() if k != "finish_evidence"}
            if "finish_evidence" in report:
                stored["finish_evidence"] = asdict(report["finish_evidence"])
            (self.connector.evidence / "status.json").write_text(
                json.dumps(stored, ensure_ascii=False, indent=2), "utf-8"
            )
            if report["state"] == "finish_ready" and self.save_authorized:
                publish(report | {"state": "finishing"})
            else:
                publish(report)

        result = super().run(authorize, forward, cancel)
        if hasattr(self, "private_failure"):
            (self.connector.evidence / "private-failure.json").write_text(
                json.dumps(self.private_failure, ensure_ascii=False), "utf-8"
            )
        if (
            result["state"] == "finish_ready"
            and self.save_authorized
            and result.get("mode_restored_normal") is True
            and not result.get("cleanup_unconfirmed")
            and not result.get("app_pause_unconfirmed")
            and authorize() is True
        ):
            try:
                current = self.connector.capture().frame.inspection
                proof = FinishConnector(self.connector).finish_once(
                    stamp=self.start_stamp,
                    grade_before=self.grade_before,
                    authorize=authorize,
                    expected={"distance_m": current.distance_m, "elapsed_s": current.elapsed_s},
                )
                result.update(
                    state="accepted" if proof.accepted else "submitted_unconfirmed",
                    records_submitted=1,
                    record_valid_proven=proof.accepted,
                    finish_evidence=proof,
                )
            except Exception:
                result.update(state="save_unconfirmed", message="正常结束保存未确认；不重试")
            forward(result)
        return result


class LiveRuntime:
    def __init__(self, root: Path, profile: Path, settings: Path):
        self.root, self.profile = root.resolve(), profile
        self.settings = _load(private_file(self.root, settings), RuntimeSettings)

    def preflight(self, route: dict, bridge: MuMuBridge):
        if not isinstance(bridge, MuMuBridge):
            raise ValueError("仅支持本项目独立MuMu")
        short = prepare_recorded_probe(
            self.root, self.settings.motion_evidence_csv or self.settings.motion_csv, 5
        )
        verified = verify_motion_evidence(
            self.root, self.settings.independent_motion_evidence, short
        )
        normal = sensor_mode(bridge.run("shell", "dumpsys", "sensorservice")) == NORMAL
        # 只承诺已确认点的有限试运行，不能将部分目录/实验通道标为完整官方跑区。
        routes_ok = (
            route["catalog_entries"] > 0
            and route["app_confirmed_approaches"] == route["catalog_entries"]
        )
        self._template()
        prepare_recorded_probe(self.root, self.settings.motion_csv, 150, bounded_stream=True)
        return {
            "route": (
                "pass" if routes_ok else "not_ready",
                "已确认点的实验路网试运行；未知点暂停，非完整场地验收",
            ),
            "motion": (
                "pass" if verified and normal else "not_ready",
                "链路有5秒独立接收证据；当前长片段全量及目标步数未验收",
            ),
            "runtime": (
                "pass" if normal else "not_ready",
                "已装配单次开始与有界调度；结束保存及有效成绩另行核验",
            ),
        }

    def _template(self):
        return StartButtonTemplate.from_private_file(
            self.root, self.settings.start_template, (720, 1280), (229, 1161, 517, 1257)
        )

    def build(self):
        preflight_profile(self.profile, self.root)
        profile = _load(private_file(self.root, self.profile), RunProfile)
        bridge = MuMuBridge(private_file(self.root, profile.receipt_file))
        config = _load(private_file(self.root, profile.route_file), RouteConfig)
        planner = RoutePlanner(config)
        gps = bridge.run("shell", "dumpsys", "location").split("    gps provider:", 1)[1]
        match = re.search(r"last location=Location\[gps ([\d.]+),([\d.]+) ", gps)
        if match is None:
            raise ValueError("没有实际同设备当前位置")
        initial = GeoPoint(lat=float(match[1]), lon=float(match[2]))
        planner.plan(initial, config.baseline[0])
        evidence = self.root / ".local/runs" / ("native-" + secrets.token_hex(8))
        evidence.mkdir()
        (self.root / ".local/setup/native-latest.json").write_text(
            json.dumps({"evidence": str(evidence)}), "utf-8"
        )
        connector = JournalConnector(bridge, self._template(), evidence=evidence)
        worker = MuMuPositionWorker(bridge, DriveEngine(planner, initial))
        dispatcher = RuntimeDispatcher(
            planner,
            NamedCardObserver.from_profile(self.root, self.profile),
            worker,
            pace_range_s_per_km=(180, 600),
            asynchronous_renewals=True,
        )
        clip = prepare_recorded_probe(self.root, self.settings.motion_csv, 150, bounded_stream=True)
        motion = ForegroundMotionClient(bridge, bounded_stream=True, replay_passes=6)

        def refresh_gps(authorize):
            cap = connector.capture()
            view = cap.frame.inspection
            if view.distance_m != 0 or view.elapsed_s != 0 or view.completed_checkpoints != 0:
                raise ValueError("同位置预刷新只允许正式零状态")
            bridge.location_wgs84(initial, authorize=authorize, command_timeout_s=1)

        runner = JournalRunner(
            connector,
            dispatcher,
            motion,
            clip,
            maximum_s=self.settings.maximum_s,
            refresh_gps=refresh_gps,
            wait_for_human=True,
        )
        runner.save_authorized = self.settings.normal_save_authorized

        def prepare_start(authorize):
            runner.start_stamp = datetime.strptime(
                bridge.run("shell", "date", "+%F_%H:%M").strip(), "%Y-%m-%d_%H:%M"
            )
            runner.grade_before = enter_formal_zero(bridge, authorize)

        runner.prepare_start = prepare_start
        return runner
