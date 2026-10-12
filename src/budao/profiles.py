"""本机独立账号配置与固定点目录预检；不连接设备、不读认证材料、不开始跑步。"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from shapely.geometry import Point

from .models import GeoPoint, RouteConfig, StrictModel
from .mumu import MANAGER_SHA256, PLAYER_NAME
from .routes import RoutePlanner, distance
from .target import parse_checkpoint_cards

SLUG = r"[a-z0-9][a-z0-9_-]{0,31}"


class CheckpointEntry(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    # 曾得到App确认的接近位置，不冒充精确的地图标记中心。
    approach: GeoPoint
    confirmation_evidence: Path | None = None

    @field_validator("name")
    @classmethod
    def exact_name(cls, value: str) -> str:
        if (
            value != value.strip()
            or re.search(r"[\x00-\x1f()]", value)
            or value.startswith("待分配")
        ):
            raise ValueError("只接受与正式卡片一致的完整点名，不猜测或模糊匹配")
        return value


class CheckpointCatalog(StrictModel):
    coordinate_system: Literal["WGS84"] = "WGS84"
    site_id: str = Field(pattern="^" + SLUG + "$")
    expected_candidate_count: int = Field(ge=1, le=64)
    # 用户估计七八个，不等于已经核实总数。
    candidate_count_verified: bool = False
    entries: tuple[CheckpointEntry, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def no_ambiguity(self):
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)) or len(names) > self.expected_candidate_count:
            raise ValueError("点名重复或目录数量超过声明的候选总数")
        return self

    def resolve(self, name: str) -> CheckpointEntry:
        matches = [entry for entry in self.entries if entry.name == name]
        if len(matches) != 1:
            raise ValueError("当前点不在本机目录，不猜测其他相似点")
        return matches[0]


class RunProfile(StrictModel):
    profile_id: str = Field(pattern="^" + SLUG + "$")
    site_id: str = Field(pattern="^" + SLUG + "$")
    receipt_file: Path
    route_file: Path
    catalog_file: Path
    selfie_file: Path | None = None
    # 本轮上传例外不扩展到以后或其他账号。
    upload_policy: Literal["manual"] = "manual"
    operator_roads_verified: bool = False


def private_file(project_root: Path, value: Path, *, must_exist: bool = True) -> Path:
    root = project_root.resolve()
    local = root / ".local"
    if local.resolve() != local:
        raise ValueError(".local被重定向，拒绝跨项目访问")
    path = (value if value.is_absolute() else root / value).resolve()
    if not path.is_relative_to(local):
        raise ValueError("运行配置、路线、凭据和照片只能位于本项目.local")
    if must_exist and not path.is_file():
        raise ValueError("本机配置引用的私有文件缺失")
    return path


def _load(path: Path, model, maximum_bytes: int = 128 * 1024):
    try:
        if path.stat().st_size > maximum_bytes:
            raise ValueError
        return model.model_validate_json(path.read_text("utf-8-sig"))
    except (ValueError, OSError):
        # Pydantic默认错误可能回显未知password/api_key字段，不能输出原配置。
        raise ValueError("本机配置结构或大小不符合要求；不回显原始内容") from None


def confirmed_approach(project_root: Path, entry: CheckpointEntry) -> bool:
    if entry.confirmation_evidence is None:
        return False
    source = private_file(project_root, entry.confirmation_evidence)
    xml_file = private_file(project_root, source.with_suffix(".xml"))
    try:
        if source.stat().st_size > 2 * 1024 * 1024 or xml_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        result = json.loads(source.read_text("utf-8-sig"))
        completed = result["after"]["completed_checkpoints"]
        before = result["before"]["completed_checkpoints"]
        if (
            type(completed) is not int
            or type(before) is not int
            or not 1 <= completed <= 3
            or before != completed - 1
        ):
            raise ValueError
        if result.get("stop_reason") != "checkpoint_increment_confirmed_in_two_frames":
            raise ValueError
        observations = result["observations"][-2:]
        if len(observations) != 2 or not observations[0]["t"] < observations[1]["t"]:
            raise ValueError
        for observation in observations:
            sampled = observation["t"]
            count = observation["app"]["completed_checkpoints"]
            if (
                type(sampled) not in (int, float)
                or not math.isfinite(sampled)
                or sampled < 0
                or type(count) is not int
                or count != completed
            ):
                raise ValueError
        xml = xml_file.read_text("utf-8-sig")
        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise ValueError
        card = parse_checkpoint_cards(xml)[completed - 1]
        if card.state != "completed" or card.name != entry.name:
            raise ValueError
        point = GeoPoint.model_validate(result["last_wgs84"])
        if distance(point, entry.approach) > 0.05:
            raise ValueError
    except (ValueError, KeyError, TypeError, IndexError, OSError, ET.ParseError):
        raise ValueError("打卡接近位置证据不一致；不回显现场数据") from None
    return True


def preflight_profile(profile_file: Path, project_root: Path) -> dict:
    path = private_file(project_root, profile_file)
    profile = _load(path, RunProfile)
    receipt_path = private_file(project_root, profile.receipt_file)
    route_path = private_file(project_root, profile.route_file)
    catalog_path = private_file(project_root, profile.catalog_file)
    routes = _load(route_path, RouteConfig)
    catalog = _load(catalog_path, CheckpointCatalog)
    if catalog.site_id != profile.site_id:
        raise ValueError("账号配置与点目录不属于同一场地")
    planner = RoutePlanner(routes)
    if distance(routes.baseline[0], routes.baseline[-1]) > 0.05:
        raise ValueError("等待路线必须闭合，不能循环时跳回起点")
    if not 5 / 3 <= routes.speed_mps <= 50 / 9:
        raise ValueError("名义速度超出当前正式3–10分钟/km范围")
    if routes.minimum_distance_m + routes.margin_m > 10_000:
        raise ValueError("最低里程和余量超过当前正式最大里程")
    try:
        if receipt_path.stat().st_size > 128 * 1024:
            raise ValueError
        receipt = json.loads(receipt_path.read_text("utf-8-sig"))
        expected_name = (
            PLAYER_NAME
            if profile.profile_id == "current"
            else "BudaoRun_profile_" + profile.profile_id
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("name") != expected_name
            or receipt.get("old_login_data_imported") is not False
            or receipt.get("provisioned_by_this_task") is not True
            or receipt.get("manager_sha256") != MANAGER_SHA256
            or type(receipt.get("index")) is not int
            or not 1 <= receipt["index"] <= 127
            or type(receipt.get("created_timestamp")) is not int
            or receipt["created_timestamp"] <= 0
            or receipt.get("android_version") != "12.0"
            or not re.fullmatch(r"127\.0\.0\.1:[1-9]\d{3,4}", receipt.get("serial", ""))
            or int(receipt["serial"].split(":")[1]) > 65535
        ):
            raise ValueError
    except (ValueError, OSError, TypeError):
        raise ValueError("设备凭据格式/独立创建声明不一致；没有连接或修改设备") from None
    verified = 0
    for entry in catalog.entries:
        position = Point(planner.xy(entry.approach))
        if not planner.region.covers(position) or not planner.roads.buffer(
            routes.snap_tolerance_m
        ).covers(position):
            raise ValueError("目录接近位置越界或离开声明通道")
        planner.plan(routes.baseline[0], entry.approach)
        verified += confirmed_approach(project_root, entry)
    selfie_available = False
    if profile.selfie_file is not None:
        private_file(project_root, profile.selfie_file)  # 不读取或解码照片。
        selfie_available = True
    return {
        "profile_id": profile.profile_id,
        "read_only": True,
        "route_geometry_valid": True,
        "catalog_entries": len(catalog.entries),
        "expected_candidates": catalog.expected_candidate_count,
        "candidate_count_verified": catalog.candidate_count_verified,
        "catalog_complete": catalog.candidate_count_verified
        and len(catalog.entries) == catalog.expected_candidate_count,
        "app_confirmed_approaches": verified,
        "operator_roads_verified": profile.operator_roads_verified,
        "selfie_file_available": selfie_available,
        "photo_contents_read": False,
        "upload_policy": profile.upload_policy,
        "device_connected": False,
        "device_identity_live_verified": False,
        "live_bridge_supported": profile.profile_id == "current",
        "ready_for_unattended_run": False,
    }
