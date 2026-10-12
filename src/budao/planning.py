"""离线卡片快照→固定目录→已声明路网建议。没有位置输出或界面操作。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import GeoPoint, RouteConfig, StrictModel
from .profiles import (
    CheckpointCatalog,
    RunProfile,
    _load,
    confirmed_approach,
    preflight_profile,
    private_file,
)
from .routes import RoutePlanner, path_length
from .target import face_verification_required, parse_checkpoint_cards, parse_run_metrics


class RouteProposal(StrictModel):
    action: Literal["observe", "navigate", "hold", "review_finish"]
    reason: str
    checkpoint_number: int | None = Field(default=None, ge=1, le=3)
    path: tuple[GeoPoint, ...] = ()
    path_m: float = Field(default=0, ge=0)
    plan_only: Literal[True] = True
    snapshot_freshness_verified: Literal[False] = False
    execution_allowed: Literal[False] = False
    upload_allowed: Literal[False] = False


def propose_route(
    config: RouteConfig,
    catalog: CheckpointCatalog,
    position: GeoPoint,
    hierarchy: str,
    confirmed_names: frozenset[str],
) -> RouteProposal:
    """历史快照只能产生建议；不能充当当前会话、计时或新打卡的证明。"""
    try:
        if "<!DOCTYPE" in hierarchy.upper() or "<!ENTITY" in hierarchy.upper():
            raise ValueError
        if face_verification_required(hierarchy):
            return RouteProposal(action="hold", reason="human_verification_required")
        metrics = parse_run_metrics(hierarchy)
        cards = parse_checkpoint_cards(hierarchy)
        if (
            metrics["distance_m"] is None
            or metrics["elapsed_s"] is None
            or metrics["elapsed_s"] <= 0
            or metrics["gps_label"] != "优"
        ):
            return RouteProposal(action="hold", reason="metrics_not_ready")
        planner = RoutePlanner(config)
        # 同一位置的连接检查，同时核对当前位置在区域及声明通道附近。
        planner.plan(position, position)
        assigned = [card for card in cards if card.state == "assigned"]
        if assigned:
            card = assigned[0]
            entry = catalog.resolve(card.name)
            if entry.name not in confirmed_names:
                return RouteProposal(action="hold", reason="approach_evidence_missing")
            path = planner.plan(position, entry.approach)
            return RouteProposal(
                action="navigate",
                reason="exact_catalog_match_on_declared_roads",
                checkpoint_number=card.number,
                path=path,
                path_m=path_length(path),
            )
        if metrics["completed_checkpoints"] == 3:
            if metrics["distance_m"] >= config.minimum_distance_m + config.margin_m:
                return RouteProposal(action="review_finish", reason="snapshot_thresholds_met")
            return RouteProposal(action="observe", reason="distance_remaining")
        return RouteProposal(action="observe", reason="checkpoint_not_assigned_yet")
    except (ValueError, TypeError, ET.ParseError):
        return RouteProposal(action="hold", reason="unknown_or_inconsistent_snapshot_or_route")


def plan_saved_snapshot(
    project_root: Path,
    profile_file: Path,
    snapshot_file: Path,
    position_file: Path,
    output_file: Path,
) -> dict:
    """只读私有输入，独占创建私有建议文件；控制台不回显实际点名/坐标。"""
    preflight_profile(profile_file, project_root)
    profile = _load(private_file(project_root, profile_file), RunProfile)
    config = _load(private_file(project_root, profile.route_file), RouteConfig)
    catalog = _load(private_file(project_root, profile.catalog_file), CheckpointCatalog)
    position = _load(private_file(project_root, position_file), GeoPoint)
    snapshot = private_file(project_root, snapshot_file)
    output = private_file(project_root, output_file, must_exist=False)
    if snapshot.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("历史界面快照超过2MiB")
    if output.exists() or not output.parent.is_dir():
        raise ValueError("建议输出必须使用已有私有目录内的新文件，不覆盖历史")
    confirmed_names = frozenset(
        entry.name for entry in catalog.entries if confirmed_approach(project_root, entry)
    )
    proposal = propose_route(
        config, catalog, position, snapshot.read_text("utf-8-sig"), confirmed_names
    )
    with output.open("x", encoding="utf-8") as stream:
        stream.write(proposal.model_dump_json(indent=2))
    return {
        "plan_only": True,
        "action": proposal.action,
        "reason": proposal.reason,
        "path_points": len(proposal.path),
        "execution_allowed": False,
        "upload_allowed": False,
        "device_connected": False,
    }
