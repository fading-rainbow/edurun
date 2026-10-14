"""本机显式装配原生入口的有界运行。未知点暂停，不将草案标为全场地验收。"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from .auth_ui import LAUNCHER, SPLASH, prepare_current_login, target_task_tops
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
from .resume import resume_once
from .routes import RoutePlanner
from .runtime import CapturedRunFrame, RuntimeDispatcher, StartWitness
from .sensor_probe import NORMAL, sensor_mode
from .session_runner import SessionRunner
from .start_button import StartButtonTemplate
from .start_connector import StartConnector
from .target import RUN_ACTIVITY, TARGET
from .ui import focused_component


class RuntimeSettings(StrictModel):
    mode: Literal["known_points_trial"]
    motion_csv: Path
    motion_evidence_csv: Path | None = None
    independent_motion_evidence: Path
    start_template: Path
    maximum_s: int = Field(default=900, ge=30, le=900)
    normal_save_authorized: bool = False
    run_zone_label: str | None = Field(default=None, min_length=1, max_length=128)
    vision_config: Path | None = None
    resume_evidence: Path | None = None


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
        if value.motion_snapshot is not None:
            probe = value.motion_snapshot.get("raw_motion_probe", {})
            compact = {
                "frame_id": value.frame.frame_id,
                "sampled_at": value.frame.sampled_at,
                "android_observed_ns": value.android_observed_ns,
                "gps_reception": value.motion_snapshot.get("gps_reception"),
                "motion": {
                    k: probe.get(k)
                    for k in (
                        "request_id",
                        "state",
                        "clip_sha256",
                        "sent_rows",
                        "maximum_gap_ns",
                        "lease_deadline_ns",
                        "last_observation_ns",
                        "last_send_ns",
                    )
                },
            }
            with (self.evidence / "input-observations.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(compact) + "\n")
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
            w = self.dispatcher.worker
            state = w.state  # immutable published snapshot, do not wait for vendor IO
            with (self.connector.evidence / "drive-observations.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "sampled_at": self.clock(),
                            "state": asdict(state) | {"position": state.position.model_dump()},
                            "commands_acknowledged": w.commands_acknowledged,
                            "output_state_unknown": w.output_state_unknown,
                        }
                    )
                    + "\n"
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

    def _resume_ticket(self, bridge):
        directory = private_file(self.root, self.settings.resume_evidence, must_exist=False)
        if not directory.is_dir() or any(
            (directory / name).exists()
            for name in (
                "save-action-attempted.json",
                "resume-action-attempted.json",
                "recovery-claimed.json",
            )
        ):
            raise ValueError("原生暂停记录不存在或已消费，不自动接管")
        status = json.loads((directory / "status.json").read_text("utf-8-sig"))
        witness_file = directory / "start-witness.json"
        witness = StartWitness.model_validate_json(witness_file.read_text("utf-8-sig"))
        saved = CapturedRunFrame.model_validate_json(
            (directory / "frames.jsonl").read_text("utf-8-sig").splitlines()[-1]
        )
        if (
            status.get("state") != "paused"
            or status.get("mode_restored_normal") is not True
            or status.get("app_pause_confirmed") is not True
            or status.get("records_submitted") != 0
            or witness.serial != bridge.serial
            or saved.inspection.run_control_state != "paused"
            or saved.inspection.completed_checkpoints != 0
            or saved.inspection.elapsed_s is None
            or not 0 < saved.inspection.elapsed_s < 1800
            or status.get("distance_m") != saved.inspection.distance_m
            or saved.sampled_at <= witness.second_running.sampled_at
            or not 0 <= time.monotonic() - saved.sampled_at < 86400
        ):
            raise ValueError("只续接本助手已清理并确认暂停的零打卡记录")
        meta_path = directory / "session-meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text("utf-8-sig"))
            if meta["original_request"] != witness.request_id:
                raise ValueError("开始元数据与新会话证据不一致")
            stamp = datetime.fromisoformat(meta["start_stamp"])
            grade = meta["grade_before"]
        else:
            # Compatibility for the already-started pre-journal session. Its
            # write-time clock is only a recovery estimate, never a new start
            # or an invented pre-start grade baseline.
            android_s = int(bridge.run("shell", "date", "+%s"))
            if abs(time.time() - android_s) > 3 or bridge.run(
                "shell", "date", "+%z"
            ).strip() != datetime.now().astimezone().strftime("%z"):
                raise ValueError("旧证据缺少开始日期，设备与主机时钟未对齐")
            stamp = datetime.fromtimestamp(
                witness_file.stat().st_mtime - witness.second_running.inspection.elapsed_s
            )
            grade = None
        if bridge.run("shell", "date", "+%F").strip() != stamp.strftime("%Y-%m-%d"):
            raise ValueError("暂停会话已跨日")
        return directory, witness, saved, stamp, grade

    def prepare_login(self, bridge):
        if self.settings.resume_evidence is None:
            return prepare_current_login(bridge)
        self._resume_ticket(bridge)
        focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
        if focus == (LAUNCHER, LAUNCHER + ".LauncherActivity"):
            tops = target_task_tops(bridge.run("shell", "dumpsys", "activity", "activities"))
            if tops != [RUN_ACTIVITY]:
                raise ValueError("助手背后的任务并非唯一的本次暂停运行页")
            bridge.run(
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-n",
                TARGET + "/" + SPLASH,
                "-f",
                "0x10000000",
            )
        if focused_component(bridge.run("shell", "dumpsys", "window", "displays")) != (
            TARGET,
            RUN_ACTIVITY,
        ):
            raise ValueError("当前不是已绑定暂停记录，不读取认证页")
        return {"state": "logged_in_ui", "server_auth_proven": False}

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
        ticket = self._resume_ticket(bridge) if self.settings.resume_evidence is not None else None
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

        if ticket is not None:
            directory, original, saved, stamp, grade = ticket
            runner.start_stamp, runner.grade_before = stamp, grade
            runner.original_request = original.request_id
            (evidence / "start-witness.json").write_text(
                original.model_dump_json(indent=2), "utf-8"
            )
            (evidence / "session-meta.json").write_text(
                json.dumps(
                    {
                        "start_stamp": stamp.isoformat(),
                        "grade_before": grade,
                        "original_request": original.request_id,
                    }
                ),
                "utf-8",
            )
            (evidence / "recovery-parent.json").write_text(
                json.dumps(
                    {
                        "parent": str(directory),
                        "grade_baseline_available": grade is not None,
                        "new_start_attempted": False,
                    }
                ),
                "utf-8",
            )

            def continue_start(_new_motion_request, authorize):
                with (directory / "recovery-claimed.json").open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps({"child": str(evidence), "retry_allowed": False}))
                return resume_once(connector, original, saved, stamp, authorize)

            runner.resume_start = continue_start
            return runner

        def prepare_start(authorize):
            scene = None
            if self.settings.vision_config is not None:
                from .ui_advisor import SceneAssistant, UIAdvisor

                scene = SceneAssistant(
                    UIAdvisor.from_private_config(self.root, self.settings.vision_config)
                )
            runner.start_stamp = datetime.strptime(
                bridge.run("shell", "date", "+%F_%H:%M").strip(), "%Y-%m-%d_%H:%M"
            )
            try:
                runner.grade_before = enter_formal_zero(
                    bridge,
                    authorize,
                    expected_zone=self.settings.run_zone_label,
                    scene_helper=scene,
                )
                (evidence / "session-meta.json").write_text(
                    json.dumps(
                        {
                            "start_stamp": runner.start_stamp.isoformat(),
                            "grade_before": runner.grade_before,
                            "original_request": runner.original_request,
                        }
                    ),
                    "utf-8",
                )
            finally:
                if scene is not None:
                    scene.client.close()

        runner.prepare_start = prepare_start
        return runner
