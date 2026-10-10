from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from .android import AndroidBridge, EmulatorConsole
from .models import RouteConfig
from .probe import probe
from .routes import RoutePlanner, import_track, path_length
from .simulation import demo_config, simulate


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
    for name in ("device", "stop", "install", "probe"):
        sub = commands.add_parser(name)
        sub.add_argument("--serial", required=True)
        if name in {"install", "probe"}:
            sub.add_argument("--apk", type=Path, required=True)
        if name == "probe":
            sub.add_argument("--seconds", type=float, default=12)
            sub.add_argument("--output", type=Path, default=Path(".local/runs/probe.json"))
    args = parser.parse_args()
    try:
        if args.command == "simulate":
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
            bridge = AndroidBridge(args.serial)
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
