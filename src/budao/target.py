"""当前4.3.0的只读控件采集；不点击、不上传图片、不把计数当有效记录。"""

from __future__ import annotations

import math
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
GPS_READY_LABEL = "GPS信号优"  # 当前4.3.0实际控件完整文本，不是测试夹具的简写。


class CheckpointCard(StrictModel):
    """现场核对的顺序卡片。距离来自App，不是模型估计或地理坐标。"""

    number: int = Field(ge=1, le=3)
    state: Literal["pending", "assigned", "completed"]
    name: str | None = None
    distance_m: float | None = Field(default=None, ge=0, le=100_000)
    allocation_remaining_m: int | None = Field(default=None, ge=0, le=100_000)


def same_anchor_cards(current, reference) -> bool:
    """同锚点的卡片身份不变；仅已分配点的实测距离允许最多5米刷新误差。

    不改写任一观察，不允许状态/名称/分配倒计程改变，也不是打卡确认。
    所有比较均相对原始凭据，不能逐帧累积容差。
    """
    if not current or len(current) != len(reference):
        return False
    for actual, old in zip(current, reference, strict=True):
        if not isinstance(actual, CheckpointCard) or not isinstance(old, CheckpointCard):
            return False
        if actual.model_dump(exclude={"distance_m"}) != old.model_dump(exclude={"distance_m"}):
            return False
        if actual.state != "assigned":
            if actual.distance_m != old.distance_m:
                return False
        elif actual.distance_m is None or old.distance_m is None:
            if actual.distance_m is not old.distance_m:
                return False
        elif not (
            math.isfinite(actual.distance_m)
            and math.isfinite(old.distance_m)
            and 0 <= actual.distance_m <= 100_000
            and 0 <= old.distance_m <= 100_000
            and abs(actual.distance_m - old.distance_m) <= 5
        ):
            return False
    return True


def same_resume_cards(current, reference, *, pending_refresh_m=0) -> bool:
    """保留原始卡片值；仅显式续跑允许待分配倒计程最多减少3米，不积累容差。"""
    if pending_refresh_m not in (0, 3) or type(pending_refresh_m) is not int:
        return False
    if same_anchor_cards(current, reference):
        return True
    if pending_refresh_m == 0 or not 1 <= len(current) == len(reference) <= 3:
        return False
    if current[:-1] != reference[:-1]:
        return False
    actual, old = current[-1], reference[-1]
    return (
        isinstance(actual, CheckpointCard)
        and isinstance(old, CheckpointCard)
        and old.state == "pending"
        and old.name is None
        and actual.model_dump(exclude={"allocation_remaining_m"})
        == old.model_dump(exclude={"allocation_remaining_m"})
        and type(old.allocation_remaining_m) is int
        and old.allocation_remaining_m > pending_refresh_m
        and type(actual.allocation_remaining_m) is int
        and 0 < actual.allocation_remaining_m <= old.allocation_remaining_m
        and old.allocation_remaining_m - actual.allocation_remaining_m <= pending_refresh_m
    )


def same_startup_anchor_cards(current, reference) -> bool:
    """仅供新开始、零里程衔接：待分配倒计程可相对原凭据减少至多3米。

    原App的公里显示按10米量化，静止GPS小幅刷新可先影响整数倒计程。
    不用于一般续跑、改变卡片身份/状态或逐帧累积容差，不改写观察。
    """
    if same_anchor_cards(current, reference):
        return True
    if len(current) != 1 or len(reference) != 1:
        return False
    actual, old = current[0], reference[0]
    return (
        isinstance(actual, CheckpointCard)
        and isinstance(old, CheckpointCard)
        and old.number == 1
        and old.state == "pending"
        and old.name is None
        and old.distance_m is None
        and actual.model_dump(exclude={"allocation_remaining_m"})
        == old.model_dump(exclude={"allocation_remaining_m"})
        and type(old.allocation_remaining_m) is int
        and old.allocation_remaining_m > 3
        and type(actual.allocation_remaining_m) is int
        and 0 < actual.allocation_remaining_m <= old.allocation_remaining_m
        and old.allocation_remaining_m - actual.allocation_remaining_m <= 3
    )


RunControlState = Literal["unknown", "not_started_or_countdown", "running", "paused"]


def parse_run_control_state(hierarchy: str) -> RunControlState:
    """识别已采集的控件组合，不证明当前会话身份，更不授权驱动或点击。"""
    if "<!DOCTYPE" in hierarchy.upper() or "<!ENTITY" in hierarchy.upper():
        return "unknown"
    root = ET.fromstring(hierarchy)
    visible = [n for n in root.iter("node") if n.get("visible-to-user") == "true"]
    if any(n.get("package") not in (TARGET, "com.android.systemui") for n in visible):
        return "unknown"
    # 遮挡后的背景数字/控件不可作为运行状态。
    if any(
        n.get("package") == TARGET
        and n.get("resource-id")
        in {
            TARGET + ":id/" + name
            for name in ("ad_close", "alert_dialog_content", "dialog_confirm")
        }
        for n in visible
    ):
        return "unknown"

    def controls(name):
        return [
            n
            for n in visible
            if n.get("package") == TARGET and n.get("resource-id") == TARGET + ":id/" + name
        ]

    start = controls("tv_start_pause_continue_run")
    stop = controls("tv_stoprun")
    if len(start) != 1 or len(stop) > 1:
        return "unknown"
    if any(
        n.get("class") != "android.widget.ProgressBar" or n.get("enabled") != "true"
        for n in start + stop
    ):
        return "unknown"
    elapsed = parse_run_metrics(hierarchy)["elapsed_s"]
    if elapsed is None:
        return "unknown"
    if elapsed == 0:
        return "not_started_or_countdown"
    return "paused" if stop else "running"


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
    run_control_state: RunControlState = "unknown"
    issues: tuple[str, ...] = ()
    # 按钮组合不能证明开始事件和本次记录ID，不能凭数值/组合开放驱动或判有效。
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
        control_state = parse_run_control_state(hierarchy)
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
    base["run_control_state"] = control_state
    return TargetInspection(**base, state=state, **metrics)
