"""本机显式装配原生入口的有界运行。未知点暂停，不将草案标为全场地验收。"""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .auth_ui import LAUNCHER, MAIN, SPLASH, prepare_current_login, target_task_tops
from .card_observer import NamedCardObserver
from .driver import DriveEngine
from .finish_connector import FinishConnector
from .foreground_motion import ForegroundMotionClient
from .initial_position import (
    plan_initial_position,
    read_gps_position,
    refresh_initial_position,
    refresh_started_anchor,
)
from .models import RouteConfig, StrictModel
from .mumu import MuMuBridge
from .mumu_driver import MuMuPositionWorker
from .portal import enter_formal_zero
from .profiles import RunProfile, _load, preflight_profile, private_file
from .raw_motion import match_recorded_snapshot, prepare_recorded_probe
from .resume import PAUSE_CLOCK_DETAIL, PauseClockExpired, resume_completed_names, resume_once
from .routes import RoutePlanner
from .runtime import CapturedRunFrame, RuntimeDispatcher, StartWitness
from .sensor_probe import NORMAL, sensor_mode
from .session_runner import SessionRunner
from .start_button import StartButtonTemplate
from .start_connector import StartConnector
from .target import (
    RUN_ACTIVITY,
    TARGET,
    TargetInspection,
    parse_checkpoint_cards,
    parse_run_control_state,
    parse_run_metrics,
)
from .ui import focused_component


class RuntimeSettings(StrictModel):
    mode: Literal["known_points_trial"]
    motion_csv: Path
    motion_evidence_csv: Path | None = None
    independent_motion_evidence: Path
    start_template: Path
    maximum_s: int = Field(default=900, ge=30, le=1800)
    rollover_enabled: bool = False
    normal_save_authorized: bool = False
    run_zone_label: str | None = Field(default=None, min_length=1, max_length=128)
    vision_config: Path | None = None
    resume_evidence: Path | None = None
    initialize_fresh_position: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def bounded_total(self):
        if self.initialize_fresh_position and self.resume_evidence is not None:
            raise ValueError("已有会话不能设置新起点")
        if self.maximum_s > 900 and not self.rollover_enabled:
            raise ValueError("超过单片段窗口必须显式启用暂停换窗")
        return self


def pause_progress_matches(status, view):
    """公共进度可能早于实际暂停；只接收有界清理延迟，之后仍须现场两帧复核。"""
    distance_m, elapsed_s = status.get("distance_m"), status.get("elapsed_s")
    return (
        type(distance_m) in (float, int)
        and math.isfinite(distance_m)
        and 0 <= distance_m <= 100_000
        and type(elapsed_s) is int
        and 0 <= elapsed_s <= 86_400
        and view.distance_m is not None
        and view.elapsed_s is not None
        and 0 <= view.distance_m - distance_m <= 30
        and 0 <= view.elapsed_s - elapsed_s <= 30
    )


def resume_trace_sources(root: Path, directory: Path, original: StartWitness):
    """最多四代已消费且双向关联的本任务续跑证据；不扫描无关运行目录。"""
    runs = root.resolve() / ".local" / "runs"
    sources, seen = [], set()
    for _ in range(4):
        directory = private_file(root, directory, must_exist=False)
        if directory.parent != runs or directory in seen or not directory.is_dir():
            raise ValueError("续跑接收证据目录越界、循环或缺失")
        seen.add(directory)
        witness_file = private_file(root, directory / "start-witness.json")
        if witness_file.parent != directory or witness_file.stat().st_size > 128 * 1024:
            raise ValueError("续跑开始凭据被重定向或超过限制")
        saved = StartWitness.model_validate_json(witness_file.read_text("utf-8-sig"))
        if saved != original:
            raise ValueError("续跑接收证据不属于同一次原生开始")
        trace = private_file(root, directory / "input-observations.jsonl", must_exist=False)
        if trace.parent != directory:
            raise ValueError("续跑接收轨迹被重定向")
        if trace.is_file():
            sources.append(trace)
        parent_file = private_file(root, directory / "recovery-parent.json", must_exist=False)
        if parent_file.parent != directory:
            raise ValueError("续跑父证据被重定向")
        if not parent_file.exists():
            return tuple(sources)
        if parent_file.stat().st_size > 4096:
            raise ValueError("续跑父证据超过大小限制")
        relation = json.loads(parent_file.read_text("utf-8-sig"))
        if relation.get("new_start_attempted") is not False:
            raise ValueError("续跑父证据不是同一会话")
        parent = private_file(root, Path(relation["parent"]), must_exist=False)
        if parent.parent != runs or parent in seen or not parent.is_dir():
            raise ValueError("续跑父目录越界、循环或缺失")
        claim_file = private_file(root, parent / "recovery-claimed.json")
        if claim_file.parent != parent or claim_file.stat().st_size > 4096:
            raise ValueError("续跑归属凭据超过大小限制")
        claim = json.loads(claim_file.read_text("utf-8-sig"))
        child = private_file(root, Path(claim["child"]), must_exist=False)
        if child != directory or claim.get("retry_allowed") is not False:
            raise ValueError("续跑父子归属未双向确认")
        directory = parent
    raise ValueError("续跑接收证据链超过四代，不自动继续")


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
        if self.private_cleanup_failures:
            (self.connector.evidence / "cleanup-failures.json").write_text(
                json.dumps(self.private_cleanup_failures, ensure_ascii=False), "utf-8"
            )
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
        if (directory / "resume-action-attempted.json").exists():
            from .resume import ResumeWitness

            continued = ResumeWitness.model_validate_json(
                (directory / "resume-witness.json").read_text("utf-8-sig")
            )
            if (
                continued.original != witness
                or saved.sampled_at <= continued.second_running.sampled_at
            ):
                raise ValueError("上一继续动作未确认或缺少之后的新暂停，不重试")
        if (
            status.get("state") not in {"paused", "error"}
            or (
                status.get("state") == "error"
                and (
                    status.get("run_started") is not True
                    or status.get("start_action_attempted") is not True
                    or status.get("cleanup_unconfirmed", False) is not False
                )
            )
            or status.get("mode_restored_normal") is not True
            or status.get("app_pause_confirmed") is not True
            or type(status.get("records_submitted")) is not int
            or status.get("records_submitted") != 0
            or status.get("record_valid_proven", False) is not False
            or status.get("one_click_workflow_verified", False) is not False
            or witness.serial != bridge.serial
            or saved.inspection.run_control_state != "paused"
            or saved.inspection.completed_checkpoints not in range(4)
            or saved.inspection.elapsed_s is None
            or not 0 < saved.inspection.elapsed_s < 1800
            or not pause_progress_matches(status, saved.inspection)
            or saved.sampled_at <= witness.second_running.sampled_at
        ):
            raise ValueError("只续接本助手已清理并确认暂停的同一进度记录")
        resume_completed_names(saved.inspection)
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
        if not 0 <= time.monotonic() - saved.sampled_at < 86400:
            raise PauseClockExpired(PAUSE_CLOCK_DETAIL)
        return directory, witness, saved, stamp, grade

    def prepare_login(self, bridge):
        if self.settings.resume_evidence is None:
            scene = None
            try:
                if self.settings.vision_config is not None:
                    from .ui_advisor import SceneAssistant, UIAdvisor

                    try:
                        scene = SceneAssistant(
                            UIAdvisor.from_private_config(self.root, self.settings.vision_config)
                        )
                    except (ValueError, OSError):
                        scene = None  # 可选助手不可用；不改变原有登录准备，也不回显配置
                return prepare_current_login(bridge, scene_helper=scene)
            finally:
                if scene is not None:
                    scene.client.close()
        try:
            directory, original, saved, stamp, _ = self._resume_ticket(bridge)
        except PauseClockExpired:
            return {
                "state": "pause_clock_expired",
                "detail": PAUSE_CLOCK_DETAIL,
                "server_auth_proven": False,
            }
        except ValueError as error:
            if "跨日" in str(error):
                return {
                    "state": "expired_record",
                    "detail": "过期记录，需用户处理",
                    "server_auth_proven": False,
                }
            raise
        focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
        if focus == (LAUNCHER, LAUNCHER + ".LauncherActivity"):
            tops = target_task_tops(bridge.run("shell", "dumpsys", "activity", "activities"))
            if tops not in ([], [RUN_ACTIVITY], [MAIN]):
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
        focus = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
        if focus == (TARGET, MAIN):
            from .finish_connector import field

            previous_grade = None
            old_grade = directory / "grade-before-recovery.xml"
            if (
                old_grade.exists()
                and datetime.fromtimestamp(old_grade.stat().st_mtime).date() == stamp.date()
            ):
                text = field(old_grade.read_text("utf-8-sig"), "tv_run_data")
                if text and re.fullmatch(r"[0-9]{1,4}", text):
                    previous_grade = int(text)

            def save_grade(count, tree):
                (directory / "grade-before-recovery.xml").write_text(tree, "utf-8")
                self._resume_grade = (original.request_id, time.monotonic(), count)

            def restored(tree, sampled):
                view = TargetInspection(
                    package=TARGET,
                    activity=RUN_ACTIVITY,
                    version="4.3.0",
                    state="metrics",
                    run_control_state=parse_run_control_state(tree),
                    checkpoint_cards=parse_checkpoint_cards(tree),
                    **parse_run_metrics(tree),
                )
                frame = CapturedRunFrame(
                    frame_id=secrets.token_hex(16), sampled_at=sampled, inspection=view
                )
                self._restored_pause = (original.request_id, frame)
                (directory / "normal-restored-pause.json").write_text(
                    frame.model_dump_json(indent=2), "utf-8"
                )

            count = enter_formal_zero(
                bridge,
                lambda: True,
                expected_zone=self.settings.run_zone_label,
                pause_expected=saved.inspection,
                on_grade=save_grade,
                restore_stamp=stamp,
                recovery_grade=previous_grade,
                on_restored=restored,
            )
            self._resume_grade = (original.request_id, time.monotonic(), count)
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
        runtime_ok = normal
        runtime_detail = "已装配单次开始与有界调度；结束保存及有效成绩另行核验"
        try:
            position = self._initial_position(bridge)
            if position.initialization_required:
                runtime_detail = "新会话起点待正式零状态后设置并实收核验；尚未移动或开始"
        except ValueError:
            runtime_ok = False
            runtime_detail = "当前位置不在可达通道或GPS未就绪；未开始乐跑"
        diagnostic_permissions = bridge.run("shell", "dumpsys", "package", "com.edurun.diagnostic")
        if any(
            len(
                re.findall(
                    r"(?m)^\s*android\.permission\." + name + r": granted=true\b",
                    diagnostic_permissions,
                )
            )
            != 1
            for name in (
                "ACCESS_FINE_LOCATION",
                "ACCESS_COARSE_LOCATION",
                "ACCESS_BACKGROUND_LOCATION",
            )
        ):
            runtime_ok = False
            runtime_detail = "诊断组件后台定位权限未就绪；未开始乐跑"
        if self.settings.resume_evidence is not None:
            try:
                self._resume_ticket(bridge)
            except PauseClockExpired:
                runtime_ok = False
                runtime_detail = PAUSE_CLOCK_DETAIL
            except ValueError as error:
                if "跨日" in str(error):
                    runtime_ok = False
                    runtime_detail = "过期记录，需用户处理"
                else:
                    runtime_ok = False
                    runtime_detail = "暂停凭据未就绪；未开始乐跑"
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
                "pass" if runtime_ok else "not_ready",
                runtime_detail,
            ),
        }

    def _template(self):
        return StartButtonTemplate.from_private_file(
            self.root, self.settings.start_template, (720, 1280), (229, 1161, 517, 1257)
        )

    def _initial_position(self, bridge):
        profile = _load(private_file(self.root, self.profile), RunProfile)
        config = _load(private_file(self.root, profile.route_file), RouteConfig)
        planner = RoutePlanner(config)
        observed = read_gps_position(bridge.run("shell", "dumpsys", "location"))
        return plan_initial_position(
            planner, observed, initialize_fresh=self.settings.initialize_fresh_position
        )

    def build(self):
        preflight_profile(self.profile, self.root)
        profile = _load(private_file(self.root, self.profile), RunProfile)
        bridge = MuMuBridge(private_file(self.root, profile.receipt_file))
        config = _load(private_file(self.root, profile.route_file), RouteConfig)
        planner = RoutePlanner(config)
        position = plan_initial_position(
            planner,
            read_gps_position(bridge.run("shell", "dumpsys", "location")),
            initialize_fresh=self.settings.initialize_fresh_position,
        )
        initial = position.planned
        ticket = self._resume_ticket(bridge) if self.settings.resume_evidence is not None else None
        evidence = self.root / ".local/runs" / ("native-" + secrets.token_hex(8))
        evidence.mkdir()
        (evidence / "initial-position-plan.json").write_text(
            json.dumps(
                {
                    "observed_cached_position": position.observed.model_dump(),
                    "planned_position": position.planned.model_dump(),
                    "initialization_required": position.initialization_required,
                    "received_position_proven": False,
                    "new_start_attempted": False,
                }
            ),
            "utf-8",
        )
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
            refresh_initial_position(connector, initial, authorize)

        def refresh_started_gps(witness, authorize):
            refresh_started_anchor(connector, witness, worker.engine.position, authorize)

        runner = JournalRunner(
            connector,
            dispatcher,
            motion,
            clip,
            maximum_s=self.settings.maximum_s,
            rollover_enabled=self.settings.rollover_enabled,
            refresh_gps=refresh_gps,
            refresh_started_gps=refresh_started_gps,
            wait_for_human=True,
        )
        runner.save_authorized = self.settings.normal_save_authorized
        recovery = None
        if self.settings.vision_config is not None:
            from .map_recovery import MapRecovery

            sources = resume_trace_sources(self.root, ticket[0], ticket[1]) if ticket else ()
            recovery = MapRecovery(
                self.root, self.settings.vision_config, connector, dispatcher, trace_sources=sources
            )
            runner.resolve_checkpoint = recovery.resolve

        if ticket is not None:
            directory, original, saved, stamp, grade = ticket
            restored = getattr(self, "_restored_pause", None)
            if (
                restored is not None
                and restored[0] == original.request_id
                and 0 <= time.monotonic() - restored[1].sampled_at < 120
            ):
                saved = restored[1]
            baseline = getattr(self, "_resume_grade", None)
            if (
                baseline is not None
                and baseline[0] == original.request_id
                and 0 <= time.monotonic() - baseline[1] < 120
            ):
                grade = baseline[2]
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
                return resume_once(
                    connector, original, saved, stamp, authorize, pending_refresh_m=3
                )

            runner.resume_start = continue_start
            if recovery is not None:

                def prepare_map(authorize):
                    if any(
                        p.state == "assigned" and p.name not in dispatcher.observer.allowed_names
                        for p in saved.inspection.checkpoint_cards
                    ):
                        recovery.resolve(authorize)

                runner.prepare_start = prepare_map
            return runner

        def prepare_start(authorize):
            def portal_progress(value):
                with (evidence / "portal-events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(value) + "\n")

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
                    on_progress=portal_progress,
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
