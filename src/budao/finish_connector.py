"""用户授权下的正常结束保存；新记录与有效次数增量分开核对。无网络伪造。"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from .auth_ui import MAIN
from .sensor_probe import NORMAL, sensor_mode
from .start_connector import StartConnector
from .target import RUN_ACTIVITY, TARGET
from .ui import KNOWN_AD_COMPONENT, dismiss_known_ad, focused_component

DETAIL = TARGET + ".activities.school_run_record_detail.RunRecordDetailActivity"


def field(tree: str, name: str) -> str | None:
    if len(tree) > 2 * 1024 * 1024 or "<!DOCTYPE" in tree.upper() or "<!ENTITY" in tree.upper():
        raise ValueError("结果树未知")
    values = [
        n.get("text", "")
        for n in ET.fromstring(tree).iter("node")
        if n.get("package") == TARGET
        and n.get("visible-to-user") == "true"
        and n.get("resource-id") == TARGET + ":id/" + name
    ]
    if len(values) > 1:
        raise ValueError("结果字段不唯一")
    return values[0] if values else None


def record_matches(tree, stamp: datetime, distance_m: float, elapsed_s: int):
    try:
        text = field(tree, "tv_run_time")
        expected = f"步道乐跑·{stamp.month}月{stamp.day}日 "
        if not text or not text.startswith(expected) or field(tree, "tv_reason"):
            return False
        hour, minute = map(int, text[len(expected) :].split(":"))
        record_minute = hour * 60 + minute
        duration = field(tree, "chronometer")
        h, m, s = map(int, duration.split(":"))
        return (
            0 <= record_minute - (stamp.hour * 60 + stamp.minute) <= 2
            and abs(float(field(tree, "tv_distance_value")) * 1000 - distance_m) <= 10
            and abs(h * 3600 + m * 60 + s - elapsed_s) <= 3
            and field(tree, "tv_log_num_value") == "3"
            and field(tree, "tv_log_num_or_calorie_tip") == "已打卡"
        )
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class FinishEvidence:
    request_id: str
    record_matches_new_start: bool
    grade_before: int
    grade_after: int | None
    save_action_attempted: bool

    @property
    def accepted(self):
        return (
            bool(re.fullmatch(r"[a-f0-9]{32}", self.request_id))
            and self.record_matches_new_start is True
            and self.save_action_attempted is True
            and type(self.grade_before) is int
            and type(self.grade_after) is int
            and self.grade_after == self.grade_before + 1
        )


class FinishConnector:
    def __init__(self, connector: StartConnector):
        self.connector, self.used = connector, False

    def finish_once(self, *, stamp: datetime, grade_before: int, authorize, expected):
        if self.used or authorize() is not True or type(grade_before) is not int:
            raise ValueError("正常结束需要新任务、开始前成绩基线及明确许可")
        self.used = True
        c, b = self.connector, self.connector.bridge
        if sensor_mode(b.run("shell", "dumpsys", "sensorservice")) != NORMAL:
            raise ValueError("输入尚未清理，不结束")
        if b.run("shell", "date", "+%F").strip() != stamp.strftime("%Y-%m-%d"):
            raise ValueError("已跨日，不上传过期记录")
        for _ in range(2):
            cap = c.capture()
            v = cap.frame.inspection
            if (
                v.run_control_state != "paused"
                or v.completed_checkpoints != 3
                or v.distance_m is None
                or v.distance_m < 1550
                or not v.elapsed_s
                or not 180 <= v.elapsed_s * 1000 / (v.distance_m + 10)
                or not v.elapsed_s * 1000 / (v.distance_m - 10) <= 600
                or v.distance_m != expected["distance_m"]
                or v.elapsed_s != expected["elapsed_s"]
            ):
                raise ValueError("本次暂停页三点、里程或配速未一致确认")
            time.sleep(0.15)
        stop = c.ui.device(resourceId=TARGET + ":id/tv_stoprun")
        if stop.count != 1 or authorize() is not True:
            raise ValueError("结束控件/许可未知")
        b.check_device()
        if focused_component(b.run("shell", "dumpsys", "window", "displays")) != (
            TARGET,
            RUN_ACTIVITY,
        ):
            raise ValueError("结束前前台改变")
        stop.long_click(duration=2)
        tree = c.ui.device.dump_hierarchy()
        if (
            field(tree, "alert_dialog_content") != "确定结束跑步并保存上传记录吗？"
            or field(tree, "dialog_confirm") != "结束"
            or authorize() is not True
        ):
            raise ValueError("结束确认文案未知；不保存")
        button = c.ui.device(resourceId=TARGET + ":id/dialog_confirm", text="结束")
        b.check_device()
        if (
            authorize() is not True
            or button.count != 1
            or focused_component(b.run("shell", "dumpsys", "window", "displays"))
            != (TARGET, RUN_ACTIVITY)
        ):
            raise ValueError("保存前控件/前台改变")
        (c.evidence / "save-action-attempted.json").write_text(
            '{"normal_save_attempted":true,"retry_allowed":false}', "utf-8"
        )
        button.click()  # 一次正常保存；超时也不重发。
        matched, grade_after, deadline = False, None, time.monotonic() + 60
        while time.monotonic() < deadline:
            focus = focused_component(b.run("shell", "dumpsys", "window", "displays"))
            if focus == KNOWN_AD_COMPONENT:
                dismiss_known_ad(b, c.ui)
                time.sleep(0.2)
                continue
            if focus not in {(TARGET, DETAIL), (TARGET, MAIN), (TARGET, RUN_ACTIVITY)}:
                break  # 不读取密码、摄像头或其它应用。
            tree = c.ui.device.dump_hierarchy()
            if focus != focused_component(b.run("shell", "dumpsys", "window", "displays")):
                break
            close = c.ui.device(resourceId=TARGET + ":id/ad_close", description="关闭")
            if close.count == 1:
                b.check_device()
                close.click()
            elif focus == (TARGET, DETAIL):
                known = c.ui.device(resourceId=TARGET + ":id/tv_i_know_run_result", text="我知道了")
                if known.count == 1:
                    b.check_device()
                    known.click()
                elif record_matches(tree, stamp, expected["distance_m"], expected["elapsed_s"]):
                    matched = True
                    (c.evidence / "saved-record.xml").write_text(tree, "utf-8")
                    c.ui.screenshot().save(c.evidence / "saved-record.png")
                    c.ui.device.press("back")
                else:
                    # 明确无效记录同样保留，不再次上传。
                    (c.evidence / "saved-record-unconfirmed.xml").write_text(tree, "utf-8")
                    break
            elif focus == (TARGET, MAIN) and matched:
                value = field(tree, "tv_run_data")
                if value and re.fullmatch(r"[0-9]{1,4}", value):
                    count = int(value)
                    if count == grade_before + 1:
                        if grade_after == count:
                            (c.evidence / "grade-after.xml").write_text(tree, "utf-8")
                            break
                        grade_after = count
            time.sleep(0.25)
        return FinishEvidence(c.last_action_request, matched, grade_before, grade_after, True)
