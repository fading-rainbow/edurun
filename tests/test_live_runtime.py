import json
from pathlib import Path

import pytest

from budao.live_runtime import LiveRuntime, RuntimeSettings, verify_motion_evidence


def settings():
    return {
        "mode": "known_points_trial",
        "motion_csv": ".local/m.csv",
        "independent_motion_evidence": ".local/proof.json",
        "start_template": ".local/start.png",
    }


@pytest.mark.parametrize("strategy", ["route", "paused_visit_trial"])
def test_native_factory_wires_strategy_without_starting_or_calling_the_model(
    tmp_path, monkeypatch, strategy
):
    from types import SimpleNamespace

    from test_raw_motion import clip

    from budao.card_observer import NamedCardObserver
    from budao.checkpoint_visit import PausedCheckpointVisitor
    from budao.models import RouteConfig
    from budao.mumu import MuMuBridge
    from budao.profiles import CheckpointCatalog, CheckpointEntry
    from budao.simulation import demo_config

    directory = tmp_path / ".local"
    (directory / "runs").mkdir(parents=True)
    (directory / "setup").mkdir()
    for name in ("profile.json", "receipt.json", "route.json"):
        (directory / name).write_text("{}")
    runtime = LiveRuntime.__new__(LiveRuntime)
    runtime.root = tmp_path
    runtime.profile = Path(".local/profile.json")
    runtime.settings = RuntimeSettings.model_validate(
        settings()
        | {"maximum_s": 30, "checkpoint_strategy": strategy, "vision_config": ".local/vision.json"}
    )
    route = demo_config()
    position = route.baseline[0]

    class Bridge(MuMuBridge):
        serial = "127.0.0.1:16416"

        def __init__(self, _):
            pass

        def check_device(self):
            pass

        def run(self, *args):
            assert args == ("shell", "dumpsys", "location")
            return (
                "    gps provider:\n"
                f"      last location=Location[gps {position.lat},{position.lon} hAcc=1]\n"
            )

    profile = SimpleNamespace(
        receipt_file=Path(".local/receipt.json"), route_file=Path(".local/route.json")
    )
    observer = NamedCardObserver(
        CheckpointCatalog(
            site_id="synthetic",
            expected_candidate_count=8,
            entries=(CheckpointEntry(name="已知点", approach=position),),
        ),
        frozenset({"已知点"}),
    )
    monkeypatch.setattr("budao.live_runtime.MuMuBridge", Bridge)
    monkeypatch.setattr("budao.live_runtime.preflight_profile", lambda *_: None)
    monkeypatch.setattr(
        "budao.live_runtime._load", lambda _p, t: route if t is RouteConfig else profile
    )
    monkeypatch.setattr(NamedCardObserver, "from_profile", lambda *_: observer)
    sample = clip(tmp_path, seconds=5)
    monkeypatch.setattr("budao.live_runtime.prepare_recorded_probe", lambda *_a, **_kw: sample)
    monkeypatch.setattr(runtime, "_template", lambda: None)
    runner = runtime.build()
    assert (
        not runner.started and not runner.motion.started and runner.dispatcher.worker.thread is None
    )
    if strategy == "route":
        assert runner.visit_checkpoint is None and runner.resolve_checkpoint is not None
    else:
        visitor = runner.visit_checkpoint.__self__
        assert isinstance(visitor, PausedCheckpointVisitor) and runner.resolve_checkpoint is None
        sentinel = object()
        visitor.on_observer(sentinel)
        assert runner.motion is sentinel


def test_journal_marks_paused_receipts_without_modifying_their_actual_values(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_runtime import frame

    from budao.live_runtime import JournalConnector
    from budao.start_connector import StartConnector

    snapshot = {
        "raw_motion_probe": {"request_id": "e" * 32, "sent_rows": 0},
        "gps_reception": {"latitude": 30, "longitude": 120},
    }
    cap = SimpleNamespace(
        frame=frame(10),
        hierarchy="<hierarchy/>",
        screenshot=None,
        motion_snapshot=snapshot,
        android_observed_ns=10_000_000_000,
    )
    monkeypatch.setattr(StartConnector, "capture", lambda *_a, **_kw: cap)
    connector = JournalConnector.__new__(JournalConnector)
    connector.evidence = tmp_path
    connector.exclude_route_calibration = True
    connector.capture()
    connector.exclude_route_calibration = False
    connector.capture()
    rows = [json.loads(x) for x in (tmp_path / "input-observations.jsonl").read_text().splitlines()]
    assert (
        rows[0]["exclude_route_calibration"] is True and "exclude_route_calibration" not in rows[1]
    )
    assert rows[0]["gps_reception"] == rows[1]["gps_reception"] == snapshot["gps_reception"]


@pytest.mark.parametrize("failed", [False, True])
def test_cleanup_diagnostics_are_only_written_to_private_evidence(tmp_path, monkeypatch, failed):
    from types import SimpleNamespace

    from budao.live_runtime import JournalRunner
    from budao.session_runner import SessionRunner

    runner = object.__new__(JournalRunner)
    runner.connector = SimpleNamespace(evidence=tmp_path)
    failures = [{"stage": "motion_stop", "type": "TimeoutError", "detail": "PRIVATE_DETAIL"}]
    runner.private_cleanup_failures = failures if failed else []
    result = {"state": "verification_required", "records_submitted": 0}
    monkeypatch.setattr(SessionRunner, "run", lambda *args: result.copy())
    actual = runner.run(lambda: True, lambda _: None)
    path = tmp_path / "cleanup-failures.json"
    assert actual == result and "PRIVATE_DETAIL" not in str(actual)
    assert path.exists() is failed
    if failed:
        assert json.loads(path.read_text()) == failures


@pytest.mark.parametrize(
    "progress,expected",
    [
        ({"distance_m": 270, "elapsed_s": 98}, True),
        ({"distance_m": 280, "elapsed_s": 104}, True),
        ({"distance_m": 249, "elapsed_s": 98}, False),
        ({"distance_m": 281, "elapsed_s": 98}, False),
        ({"distance_m": 280, "elapsed_s": 73}, False),
        ({"distance_m": 280, "elapsed_s": 105}, False),
        ({"distance_m": float("nan"), "elapsed_s": 98}, False),
        ({"distance_m": True, "elapsed_s": 98}, False),
        ({}, False),
    ],
)
def test_progress_can_precede_final_pause_but_not_override_or_invent_it(progress, expected):
    from types import SimpleNamespace

    from budao.live_runtime import pause_progress_matches

    assert (
        pause_progress_matches(progress, SimpleNamespace(distance_m=280, elapsed_s=104)) is expected
    )


def test_explicit_configuration_not_an_api_key_or_arbitrary_runtime(tmp_path):
    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(settings()), "utf-8")
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    assert runtime.settings.mode == "known_points_trial"
    for extra in ({"api_key": "PRIVATE_VALUE"}, {"maximum_s": 901}, {"mode": "unattended"}):
        with pytest.raises(ValueError):
            RuntimeSettings.model_validate(settings() | extra)
    with pytest.raises(ValueError):
        LiveRuntime(tmp_path, Path(".local/p.json"), Path("outside.json"))


def test_missing_or_same_uid_proof_is_not_an_independent_receiver(tmp_path):
    from test_raw_motion import clip

    sample = clip(tmp_path)
    assert not verify_motion_evidence(tmp_path, Path(".local/missing.json"), sample)
    (tmp_path / ".local").mkdir(exist_ok=True)
    proof = {
        "sender": {"package": "com.edurun.diagnostic", "uid": 10041},
        "receiver": {"package": "com.edurun.receiver", "uid": 10041, "capture_done": True},
        "mode_restored_normal": True,
    }
    (tmp_path / ".local/proof.json").write_text(json.dumps(proof), "utf-8")
    assert not verify_motion_evidence(tmp_path, Path(".local/proof.json"), sample)


def test_preflight_reports_unreachable_gps_before_factory_or_pending_fresh_setup(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from test_raw_motion import clip

    from budao.mumu import MuMuBridge

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(settings()), "utf-8")
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))

    class Bridge(MuMuBridge):
        background_granted = True

        def __init__(self):
            pass

        def run(self, *args):
            if args == ("shell", "dumpsys", "package", "com.edurun.diagnostic"):
                permissions = {
                    "ACCESS_FINE_LOCATION": True,
                    "ACCESS_COARSE_LOCATION": True,
                    "ACCESS_BACKGROUND_LOCATION": self.background_granted,
                }
                return "\n".join(
                    f"android.permission.{name}: granted={str(granted).lower()}"
                    for name, granted in permissions.items()
                )
            assert args == ("shell", "dumpsys", "sensorservice")
            return "Mode: NORMAL"

    monkeypatch.setattr("budao.live_runtime.verify_motion_evidence", lambda *_: True)
    monkeypatch.setattr(
        "budao.live_runtime.prepare_recorded_probe", lambda *_a, **_kw: clip(tmp_path)
    )
    monkeypatch.setattr(runtime, "_template", lambda: None)

    def outside(_bridge):
        raise ValueError("private location details must not reach UI")

    monkeypatch.setattr(runtime, "_initial_position", outside)
    route = {"catalog_entries": 1, "app_confirmed_approaches": 1}
    blocked = runtime.preflight(route, Bridge())
    assert blocked["runtime"] == (
        "not_ready",
        "当前位置不在可达通道或GPS未就绪；未开始乐跑",
    )
    monkeypatch.setattr(
        runtime, "_initial_position", lambda _: SimpleNamespace(initialization_required=True)
    )
    pending = runtime.preflight(route, Bridge())
    assert pending["runtime"] == (
        "pass",
        "新会话起点待正式零状态后设置并实收核验；尚未移动或开始",
    )
    Bridge.background_granted = False
    blocked_background = runtime.preflight(route, Bridge())
    assert blocked_background["runtime"] == ("not_ready", "诊断组件后台定位权限未就绪；未开始乐跑")


def test_long_run_requires_explicit_bounded_rollover_not_unlimited_clip():
    assert (
        RuntimeSettings.model_validate(
            settings() | {"maximum_s": 1800, "rollover_enabled": True}
        ).maximum_s
        == 1800
    )
    with pytest.raises(ValueError):
        RuntimeSettings.model_validate(settings() | {"maximum_s": 1801, "rollover_enabled": True})


def test_native_recovery_brings_existing_task_via_exported_launcher_not_run_activity(
    tmp_path, monkeypatch
):
    from budao.auth_ui import LAUNCHER, SPLASH
    from budao.target import RUN_ACTIVITY, TARGET

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    monkeypatch.setattr(runtime, "_resume_ticket", lambda _: (directory, None, None, None, None))

    class Bridge:
        def __init__(self):
            self.started, self.calls = False, []

        def run(self, *args):
            self.calls.append(args)
            if args[:3] == ("shell", "am", "start"):
                self.started = True
                return ""
            if "activities" in args:
                return (
                    "ACTIVITY MANAGER ACTIVITIES\nHist #1: ActivityRecord{a u0 "
                    + TARGET
                    + "/"
                    + RUN_ACTIVITY
                    + " t60}\nHist #0: ActivityRecord{b u0 "
                    + TARGET
                    + "/.activities.main.MainActivity t60}"
                )
            focus = TARGET + "/" + RUN_ACTIVITY if self.started else LAUNCHER + "/.LauncherActivity"
            return "mCurrentFocus=Window{a u0 " + focus + "}"

    b = Bridge()
    assert runtime.prepare_login(b)["state"] == "logged_in_ui"
    starts = [a for a in b.calls if a[:3] == ("shell", "am", "start")]
    assert len(starts) == 1 and TARGET + "/" + SPLASH in starts[0]
    assert TARGET + "/" + RUN_ACTIVITY not in starts[0]


@pytest.mark.parametrize("clock_expired", [False, True])
def test_expired_resume_evidence_reports_specific_fixed_reason_in_login_and_preflight(
    tmp_path, monkeypatch, clock_expired
):
    from test_raw_motion import clip

    from budao.resume import PAUSE_CLOCK_DETAIL, PauseClockExpired

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))

    def expired_ticket(_bridge):
        if clock_expired:
            raise PauseClockExpired("private details must not reach public report")
        raise ValueError("暂停会话已跨日")

    monkeypatch.setattr(runtime, "_resume_ticket", expired_ticket)
    monkeypatch.setattr("budao.live_runtime.verify_motion_evidence", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "budao.live_runtime.prepare_recorded_probe", lambda *_a, **_kw: clip(tmp_path)
    )
    monkeypatch.setattr(runtime, "_template", lambda: None)

    from budao.mumu import MuMuBridge

    class DeviceBridge(MuMuBridge):
        def __init__(self):
            pass

        def check_device(self):
            pass

        def run(self, *args):
            if "sensorservice" in args:
                return "Mode: 0"
            return ""

    b = DeviceBridge()
    login_result = runtime.prepare_login(b)
    expected_detail = PAUSE_CLOCK_DETAIL if clock_expired else "过期记录，需用户处理"
    assert login_result == {
        "state": "pause_clock_expired" if clock_expired else "expired_record",
        "detail": expected_detail,
        "server_auth_proven": False,
    }

    route_info = {
        "catalog_entries": 1,
        "app_confirmed_approaches": 1,
    }
    preflight_result = runtime.preflight(route_info, b)
    assert preflight_result["runtime"] == ("not_ready", expected_detail)


def test_restored_pause_persisted_and_used_in_build(tmp_path, monkeypatch):
    import time

    from test_runtime import frame, witness

    from budao.runtime import CapturedRunFrame
    from budao.target import RUN_ACTIVITY, TARGET, CheckpointCard

    directory = tmp_path / ".local" / "pending"
    directory.mkdir(parents=True)
    (tmp_path / ".local" / "run.json").write_text(
        json.dumps(settings() | {"resume_evidence": ".local/pending"})
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))

    original_w = witness()
    old_frame = frame(10, control="paused", distance_m=50, elapsed=300)
    ticket = (directory, original_w, old_frame, None, None)
    monkeypatch.setattr(runtime, "_resume_ticket", lambda _: ticket)

    from test_target import hierarchy

    tree = hierarchy(
        tv_distance_value="0.08",
        tv_distance_tip="公里",
        chronometer="00:05:20",
        tv_log_num_value="0",
        tv_log_num_or_calorie_tip="已打卡",
        tv_start_pause_continue_run="继续",
    )

    class EnterBridge:
        def __init__(self):
            self.restored = False

        def run(self, *args):
            focus = (
                f"{TARGET}/{RUN_ACTIVITY}"
                if self.restored
                else f"{TARGET}/.activities.main.MainActivity"
            )
            return f"mCurrentFocus=Window{{a u0 {focus}}}"

    eb = EnterBridge()

    def mock_enter_formal_zero(*_args, on_restored=None, **_kwargs):
        if on_restored:
            on_restored(tree, time.monotonic())
        eb.restored = True
        return 7

    monkeypatch.setattr("budao.live_runtime.enter_formal_zero", mock_enter_formal_zero)
    monkeypatch.setattr("budao.live_runtime.target_task_tops", lambda _: [])
    test_card = CheckpointCard(number=1, name="点1", state="assigned", distance_m=10)
    monkeypatch.setattr("budao.live_runtime.parse_checkpoint_cards", lambda _: (test_card,))

    runtime.prepare_login(eb)

    # Check normal-restored-pause.json was written
    restored_file = directory / "normal-restored-pause.json"
    assert restored_file.exists()
    restored_frame = CapturedRunFrame.model_validate_json(restored_file.read_text("utf-8"))
    assert restored_frame.inspection.distance_m == 80
    assert restored_frame.inspection.elapsed_s == 320
    assert runtime._restored_pause[0] == original_w.request_id
    assert runtime._restored_pause[1].frame_id == restored_frame.frame_id
