from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from .android import AVD_NAME, PROJECT_AVD_NAMES, AndroidBridge, EmulatorConsole
from .editor import serve_editor
from .launcher import serve_launcher
from .models import RouteConfig
from .motion_data import prepare_realworld
from .mumu import MuMuBridge
from .planning import plan_saved_snapshot
from .probe import probe
from .profiles import preflight_profile
from .replay import replay_diagnostic
from .routes import RoutePlanner, import_track, path_length
from .simulation import demo_config, simulate
from .startup_compat import configure_startup_compat
from .target import inspect_target


def apk_metadata(path: Path) -> dict:
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    with zipfile.ZipFile(path) as archive:
        libraries = [name for name in archive.namelist() if name.startswith("lib/")]
        raw = [name for name in archive.namelist() if name.startswith("res/raw/")]
    return {
        "sha256": checksum,
        "bytes": path.stat().st_size,
        "native_abis": sorted({name.split("/")[1] for name in libraries}),
        "raw_resources": raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BudaoRun：独立模拟器实验，不自动提交真实跑步")
    commands = parser.add_subparsers(dest="command", required=True)
    editor = commands.add_parser("editor")
    editor.add_argument("--port", type=int, default=8765)
    launcher = commands.add_parser("launcher-service", help="本机原生启动助手的配对/准备检查服务")
    launcher.add_argument(
        "--profile", type=Path, default=Path(".local/profiles/current/profile.json")
    )
    launcher.add_argument(
        "--config", type=Path, default=Path(".local/launcher/current/service.json")
    )
    simulate_parser = commands.add_parser("simulate")
    simulate_parser.add_argument("--runs", type=int, default=30)
    simulate_parser.add_argument("--seed", type=int, default=0)
    demo = commands.add_parser("demo-config")
    demo.add_argument("--output", type=Path, required=True)
    route = commands.add_parser("validate-route")
    route.add_argument("config", type=Path)
    track = commands.add_parser("inspect-track")
    track.add_argument("track", type=Path)
    apk = commands.add_parser("inspect-apk")
    apk.add_argument("apk", type=Path)
    export = commands.add_parser("export-qit-routes")
    export.add_argument("apk", type=Path)
    export.add_argument("--output", type=Path, default=Path(".local/routes/qitrun"))
    replay = commands.add_parser("replay-diagnostic")
    replay.add_argument("--serial", required=True)
    replay.add_argument("--avd", choices=sorted(PROJECT_AVD_NAMES), default=AVD_NAME)
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--seconds", type=float, default=30)
    replay.add_argument("--motion-csv", type=Path)
    replay.add_argument("--motion-hz", type=int, choices=(25, 50), default=25)
    replay.add_argument("--output", type=Path, default=Path(".local/runs/replay.json"))
    public_motion = commands.add_parser("prepare-public-motion")
    public_motion.add_argument("--directory", type=Path, default=Path(".local/motion/realworld"))
    public_motion.add_argument("--seconds", type=int, default=60)
    mumu = commands.add_parser("inspect-mumu", help="只读独立MuMu实例及本项目诊断快照")
    mumu.add_argument("--receipt", type=Path, default=Path(".local/setup/mumu-owned-instance.json"))
    mumu_target = commands.add_parser("inspect-mumu-target", help="只读本项目MuMu上的正式打卡卡片")
    mumu_target.add_argument(
        "--receipt", type=Path, default=Path(".local/setup/mumu-owned-instance.json")
    )
    profile = commands.add_parser(
        "preflight-profile", help="只读本机账号/路线/固定点配置，不连接设备"
    )
    profile.add_argument("--profile", type=Path, required=True)
    plan = commands.add_parser("plan-snapshot", help="离线卡片快照生成私有路线建议，不移动或提交")
    for name in ("profile", "snapshot", "position", "output"):
        plan.add_argument("--" + name, type=Path, required=True)
    for name in ("start-mumu", "stop-mumu"):
        sub = commands.add_parser(name, help="只启动/停止现场凭据确认的项目MuMu，不启动乐跑")
        sub.add_argument(
            "--receipt", type=Path, default=Path(".local/setup/mumu-owned-instance.json")
        )
    for name in ("device", "stop", "install", "probe", "inspect-target", "startup-compat"):
        sub = commands.add_parser(name)
        sub.add_argument("--serial", required=True)
        sub.add_argument("--avd", choices=sorted(PROJECT_AVD_NAMES), default=AVD_NAME)
        if name in {"install", "probe"}:
            sub.add_argument("--apk", type=Path, required=True)
        if name == "probe":
            sub.add_argument("--seconds", type=float, default=12)
            sub.add_argument("--output", type=Path, default=Path(".local/runs/probe.json"))
        if name == "startup-compat":
            mode = sub.add_mutually_exclusive_group(required=True)
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "editor":
            serve_editor(args.port)
            return 0
        elif args.command == "launcher-service":
            serve_launcher(Path.cwd(), args.profile, args.config)
            return 0
        elif args.command == "simulate":
            if not 1 <= args.runs <= 1000:
                raise ValueError("runs应在1–1000之间")
            results = [simulate(args.seed + index) for index in range(args.runs)]
            result = {
                "synthetic_only": True,
                "passed": all(r["passed"] for r in results),
                "runs": len(results),
                "real_records_submitted": 0,
                "results": results,
            }
        elif args.command == "demo-config":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.exists():
                raise ValueError("保留已有配置，不覆盖")
            args.output.write_text(demo_config().model_dump_json(indent=2), "utf-8")
            result = {"synthetic_only": True, "output": str(args.output)}
        elif args.command == "validate-route":
            config = RouteConfig.model_validate_json(args.config.read_text("utf-8-sig"))
            planner = RoutePlanner(config)
            result = {
                "valid": True,
                "baseline_metres": path_length(config.baseline),
                "nodes": planner.graph.number_of_nodes(),
                "edges": planner.graph.number_of_edges(),
            }
        elif args.command == "inspect-track":
            points = import_track(args.track)
            result = {
                "points": len(points),
                "metres": round(path_length(points), 2),
                "coordinate_system": "WGS84 per file specification; verify provenance",
            }
        elif args.command == "inspect-apk":
            result = apk_metadata(args.apk)
        elif args.command == "prepare-public-motion":
            result = prepare_realworld(args.directory, args.seconds)
        elif args.command == "inspect-mumu":
            bridge = MuMuBridge(args.receipt)
            snapshot = bridge.diagnostic_snapshot()
            result = {
                "read_only": True,
                "project_instance": bridge.avd_name,
                "gnss": snapshot.get("gnss"),
                "sensors": [
                    {key: sensor.get(key) for key in ("type", "name", "event_count", "received_hz")}
                    for sensor in snapshot.get("sensors", [])
                    if sensor.get("registered")
                ],
                "target_login_verified": False,
                "formal_record_validated": False,
            }
        elif args.command == "inspect-mumu-target":
            result = inspect_target(MuMuBridge(args.receipt)).model_dump()
        elif args.command == "preflight-profile":
            result = preflight_profile(args.profile, Path.cwd())
        elif args.command == "plan-snapshot":
            result = plan_saved_snapshot(
                Path.cwd(), args.profile, args.snapshot, args.position, args.output
            )
        elif args.command == "start-mumu":
            bridge = MuMuBridge(args.receipt, allow_stopped=True)
            bridge.start_owned()
            result = {"project_instance_started": True, "target_app_launch_requested": False}
        elif args.command == "stop-mumu":
            bridge = MuMuBridge(args.receipt)
            bridge.stop_owned()
            result = {"project_instance_stopped": True, "data_wipe_requested": False}
        elif args.command == "export-qit-routes":
            if ".local" not in args.output.resolve().parts:
                raise ValueError("真实路线只能导出到忽略的.local目录")
            names = ["huancaochang.kml", "xueyuan_bei.kml", "xueyuan_nan.kml"]
            args.output.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(args.apk) as source:
                for name in names:
                    if source.getinfo("res/raw/" + name).file_size > 2 * 1024 * 1024:
                        raise ValueError("APK路线超过2MiB，不解压异常大型数据")
                    data = source.read("res/raw/" + name)
                    destination = args.output / name
                    if destination.exists() and destination.read_bytes() != data:
                        raise ValueError("已有路线不同，拒绝覆盖")
                    destination.write_bytes(data)
            result = {"exported": names, "private_local_only": True}
        else:
            bridge = AndroidBridge(args.serial, avd_name=args.avd)
            if args.command == "device":
                with EmulatorConsole(bridge) as console:
                    result = {
                        "project_emulator": True,
                        "avd": bridge.avd_name,
                        "abis": bridge.run("shell", "getprop", "ro.product.cpu.abilist"),
                        "sensors": console.sensors(),
                    }
            elif args.command == "stop":
                bridge.check_device()
                bridge.run("emu", "kill")
                result = {"stopped_only_project_avd": True}
            elif args.command == "install":
                bridge.install(args.apk)
                result = {"installed_on_project_avd_only": True, "app_not_started": True}
            elif args.command == "inspect-target":
                result = inspect_target(bridge).model_dump()
            elif args.command == "startup-compat":
                result = configure_startup_compat(
                    bridge,
                    Path(".local/setup/startup-compat") / f"{bridge.avd_name}.json",
                    restore=args.restore,
                )
            elif args.command == "replay-diagnostic":
                config = RouteConfig.model_validate_json(args.config.read_text("utf-8-sig"))
                result = replay_diagnostic(
                    bridge, config, args.output, args.seconds, args.motion_csv, args.motion_hz
                )
            else:
                result = probe(bridge, args.apk, args.output, args.seconds)
                # 私人诊断详细数据留本地；终端只报告结论。
                result = {
                    key: value
                    for key, value in result.items()
                    if key not in {"snapshot", "console_sensors"}
                }
                result["output"] = str(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
