"""当前4.3.0的只读控件采集；不点击、不上传图片、不把计数当有效记录。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .android import AndroidBridge
from .models import StrictModel
from .ui import AppUI, focused_component

TARGET = "com.lptiyu.tanke"
RUN_ACTIVITY = TARGET + ".activities.school_run.MapRunActivity"


class CheckpointCard(StrictModel):
    """现场核对的顺序卡片。距离来自App，不是模型估计或地理坐标。"""

    number: int = Field(ge=1, le=3)
    state: Literal["pending", "assigned", "completed"]
    name: str | None = None
    distance_m: float | None = Field(default=None, ge=0, le=100_000)
    allocation_remaining_m: int | None = Field(default=None, ge=0, le=100_000)


def face_verification_required(hierarchy: str) -> bool:
    """只识别现场出现的正式跑步中断提示；不读取摄像头、人脸或认证材料。"""
    root = ET.fromstring(hierarchy)
    expected = {
        "alert_dialog_content": "本次跑步需要进行人脸验证\n验证成功后可继续跑步",
        "dialog_confirm": "去验证",
    }
    for name, text in expected.items():
        nodes = [
            n
            for n in root.iter("node")
            if n.get("package") == TARGET
            and n.get("resource-id") == TARGET + ":id/" + name
            and n.get("visible-to-user") == "true"
        ]
        if len(nodes) != 1 or nodes[0].get("text", "").replace("\r\n", "\n") != text:
            return False
    return True


def parse_checkpoint_cards(hierarchy: str) -> tuple[CheckpointCard, ...]:
    """只接收正式模式一致的完整可见卡片；不读取App私有数据或打开地图接口。"""
    metrics = parse_run_metrics(hierarchy)
    completed = metrics["completed_checkpoints"]
    if metrics["counter_label"] != "已打卡" or completed is None:
        raise ValueError("不是已核对的正式打卡计数")
    root = ET.fromstring(hierarchy)

    def visible(node: ET.Element, name: str) -> bool:
        return (
            node.get("package") == TARGET
            and node.get("resource-id") == TARGET + ":id/" + name
            and node.get("visible-to-user") == "true"
        )

    rows = [n for n in root.iter("node") if visible(n, "rl_log_point")]
    if len(rows) != min(3, completed + 1):
        raise ValueError("卡片缺失、多余或未全部可见")
    cards = []
    for number, row in enumerate(rows, 1):
        names = [n.get("text", "") for n in row.iter("node") if visible(n, "tv_point_name")]
        statuses = [n.get("text", "") for n in row.iter("node") if visible(n, "tv_point_status")]
        if len(names) != 1 or len(statuses) > 1:
            raise ValueError("单行卡片字段不唯一")
        name = names[0]
        status = statuses[0] if statuses else None
        if number <= completed:
            if status != "已打卡" or not name or "(" in name or len(name) > 100:
                raise ValueError("完成卡片与正式计数不一致")
            cards.append(CheckpointCard(number=number, state="completed", name=name))
            continue
        pending = re.fullmatch(r"待分配\(再跑([0-9]{1,6})米后分配打卡点\)", name)
        assigned = re.fullmatch(r"([^()\r\n]{1,100})\(距你([0-9]{1,6}(?:\.[0-9]{1,2})?)米\)", name)
        if pending and status is None:
            cards.append(
                CheckpointCard(
                    number=number, state="pending", allocation_remaining_m=int(pending[1])
                )
            )
        elif assigned and status == "未打卡":
            cards.append(
                CheckpointCard(
                    number=number,
                    state="assigned",
                    name=assigned[1],
                    distance_m=float(Decimal(assigned[2])),
                )
            )
        else:
            raise ValueError("未知或互相矛盾的打卡卡片，拒绝猜测")
    return tuple(cards)


class TargetInspection(StrictModel):
    read_only: Literal[True] = True
    package: str | None = None
    activity: str | None = None
    version: str | None = None
    state: Literal["metrics", "unavailable"] = "unavailable"
    distance_m: float | None = Field(default=None, ge=0)
    elapsed_s: int | None = Field(default=None, ge=0)
    completed_checkpoints: int | None = Field(default=None, ge=0, le=3)
    gps_label: str | None = None
    counter_label: str | None = None
    checkpoint_cards: tuple[CheckpointCard, ...] = ()
    issues: tuple[str, ...] = ()
    # 尚未采集真实开始/结束及本次记录ID控件，不能凭这些数值开放驱动或判有效。
    run_started_proven: Literal[False] = False
    record_valid_proven: Literal[False] = False


def parse_run_metrics(hierarchy: str) -> dict:
    root = ET.fromstring(hierarchy)

    def text(name: str) -> str | None:
        nodes = [
            n
            for n in root.iter("node")
            if n.get("package") == TARGET
            and n.get("resource-id") == TARGET + ":id/" + name
            and n.get("visible-to-user") == "true"
        ]
        return nodes[0].get("text") if len(nodes) == 1 else None

    issues = []
    raw_distance = text("tv_distance_value")
    distance_m = None
    if (
        text("tv_distance_tip") == "公里"
        and raw_distance is not None
        and re.fullmatch(r"\d{1,6}(?:\.\d{1,2})?", raw_distance)
    ):
        distance_m = float(Decimal(raw_distance) * 1000)
    else:
        issues.append("distance_missing_ambiguous_or_invalid_unit")
    elapsed_s = None
    raw_elapsed = text("chronometer") or ""
    if re.fullmatch(r"\d{2}:[0-5]\d:[0-5]\d", raw_elapsed):
        h, m, s = (int(v) for v in raw_elapsed.split(":"))
        elapsed_s = h * 3600 + m * 60 + s
    else:
        issues.append("elapsed_missing_ambiguous_or_invalid")
    counter_label = text("tv_log_num_or_calorie_tip")
    # 实测ID包含or_calorie，不能把自由模式的热量字段当三个打卡点。
    completed = None
    raw_count = text("tv_log_num_value") or ""
    if counter_label == "已打卡" and re.fullmatch(r"[0-3]", raw_count):
        completed = int(raw_count)
    else:
        issues.append("checkpoint_counter_missing_ambiguous_or_wrong_mode")
    gps_label = text("tv_gps_strength")
    if gps_label is None:
        issues.append("gps_status_missing_or_ambiguous")
    elif gps_label == "搜索中":
        issues.append("gps_searching")
    issues.append("start_controls_and_current_record_identity_unverified")
    return {
        "distance_m": distance_m,
        "elapsed_s": elapsed_s,
        "completed_checkpoints": completed,
        "gps_label": gps_label,
        "counter_label": counter_label,
        "issues": tuple(issues),
    }


def inspect_target(
    bridge: AndroidBridge, ui_factory: Callable[[AndroidBridge], AppUI] = AppUI
) -> TargetInspection:
    bridge.check_device()
    before = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
    if before is None:
        return TargetInspection(issues=("foreground_unknown",))
    package, activity = before
    base = {"package": package, "activity": activity}
    if before != (TARGET, RUN_ACTIVITY):
        # 尤其不读取QQ密码/验证码界面的树或截图；也不把首页累计里程当本次跑步。
        return TargetInspection(**base, issues=("not_target_run_page",))
    metadata = bridge.run("shell", "dumpsys", "package", TARGET)
    name = re.search(r"^\s*versionName=(\S+)", metadata, re.MULTILINE)
    code = re.search(r"^\s*versionCode=(\d+)\b", metadata, re.MULTILINE)
    base["version"] = name[1] if name else None
    if not name or name[1] != "4.3.0" or not code or code[1] != "230":
        return TargetInspection(**base, issues=("uncollected_target_version",))
    hierarchy = ui_factory(bridge).device.dump_hierarchy()
    after = focused_component(bridge.run("shell", "dumpsys", "window", "displays"))
    if after != before:
        return TargetInspection(**base, issues=("foreground_changed_during_read",))
    try:
        if face_verification_required(hierarchy):
            return TargetInspection(**base, issues=("human_face_verification_required",))
        metrics = parse_run_metrics(hierarchy)
    except ET.ParseError:
        return TargetInspection(**base, issues=("malformed_hierarchy",))
    state = (
        "metrics"
        if any(metrics[k] is not None for k in ("distance_m", "elapsed_s", "completed_checkpoints"))
        else "unavailable"
    )
    try:
        cards = parse_checkpoint_cards(hierarchy)
    except ValueError:
        cards = ()
    base["checkpoint_cards"] = cards
    return TargetInspection(**base, state=state, **metrics)
