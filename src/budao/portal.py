"""4.3.0正常入口；零状态或显式绑定的当天暂停记录，不开始新跑步。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime

from .auth_ui import LAUNCHER, MAIN, SPLASH, target_task_tops
from .finish_connector import field
from .mumu import MuMuBridge
from .target import (
    RUN_ACTIVITY,
    TARGET,
    parse_checkpoint_cards,
    parse_run_control_state,
    parse_run_metrics,
)
from .ui import KNOWN_AD_COMPONENT, AppUI, dismiss_known_ad, focused_component

BEFORE = TARGET + ".activities.school_run.RunBeforeActivity"


def enter_formal_zero(
    bridge: MuMuBridge,
    authorize: Callable[[], bool],
    *,
    ui_factory=AppUI,
    clock=time.monotonic,
    sleep=time.sleep,
    expected_zone: str | None = None,
    scene_helper=None,
    pause_expected=None,
    on_grade=None,
    restore_stamp=None,
    recovery_grade=None,
    on_restored=None,
):
    if not isinstance(bridge, MuMuBridge) or not callable(authorize):
        raise ValueError("正式入口仅支持独立MuMu及明确许可")
    ui = ui_factory(bridge)

    def focus():
        return focused_component(bridge.run("shell", "dumpsys", "window", "displays"))

    def click(name, text, expected):
        bridge.check_device()
        if authorize() is not True or focus() != expected:
            raise ValueError("入口许可或前台改变")
        button = ui.device(resourceId=TARGET + ":id/" + name, text=text)
        if button.count != 1:
            raise ValueError("入口控件未知或不唯一")
        button.click()

    initial = focus()
    if initial == (LAUNCHER, LAUNCHER + ".LauncherActivity"):
        tasks = bridge.run("shell", "dumpsys", "activity", "activities")
        tops = target_task_tops(tasks)
        if not tops or any(t != MAIN for t in tops):
            raise ValueError("目标不是安全首页，不覆盖旧会话")
        bridge.run(
            "shell",
            "am",
            "start",
            "-n",
            TARGET + "/.activities.splash.SplashActivity",
            authorize=authorize,
        )
    elif initial != (TARGET, MAIN):
        raise ValueError("正式入口必须从已登录首页开始，不接管旧运行")
    deadline, run_selected, entered, rules_seen = clock() + 40, False, False, False
    grade_frames = []
    safety_confirmed = False
    advised = False
    restored = False
    ad_attempts, last_close = 0, None
    while clock() < deadline and authorize() is True:
        current = focus()
        if current is None or current == (TARGET, SPLASH):
            sleep(0.2)
            continue
        if current == KNOWN_AD_COMPONENT:
            dismiss_known_ad(bridge, ui, authorize=authorize)
            sleep(0.2)
            continue
        if current not in {(TARGET, MAIN), (TARGET, BEFORE), (TARGET, RUN_ACTIVITY)}:
            raise ValueError("必要认证或未知页面；不读取、不覆盖")
        tree = ui.device.dump_hierarchy()
        if focus() != current:
            raise ValueError("采集期间前台改变")
        if current == (TARGET, MAIN):
            close = ui.device(resourceId=TARGET + ":id/ad_close", description="关闭")
            if close.count == 1:
                bridge.check_device()
                if focus() != current or authorize() is not True:
                    raise ValueError("广告关闭许可改变")
                close.click()
            elif (
                scene_helper is not None
                and ad_attempts < 2
                and callable(getattr(scene_helper, "close_ad", None))
                and scene_helper.opaque_overlay(tree)
            ):
                # 不透明全屏广告没有任何目标App资源ID；最多两次建议，两次点击间隔≥3秒。
                if last_close is not None and clock() - last_close < 3:
                    sleep(0.2)
                    continue
                ad_attempts += 1
                began = clock()
                try:
                    sent = scene_helper.close_ad(bridge, ui, authorize)
                except RuntimeError:
                    raise ValueError("广告关闭建议不可用；不继续入口") from None
                if sent is not True:
                    raise ValueError("广告关闭未获本地确认；不继续入口")
                deadline += min(30, clock() - began)
                last_close = clock()
                sleep(0.2)
                continue
            elif pause_expected is not None and field(tree, "dialog_confirm") == "恢复":
                content = field(tree, "alert_dialog_content")
                match = re.fullmatch(
                    r"检测到您在(\d{4})年(\d{1,2})月(\d{1,2})日 (\d{2}):(\d{2})分"
                    r"开始跑步的记录未完成，是否恢复跑步\?",
                    content or "",
                )
                if restored or not match or not isinstance(restore_stamp, datetime):
                    raise ValueError("恢复提示缺少本次新开始日期，不恢复")
                date = datetime(*map(int, match.groups()))
                if (
                    date.date() != restore_stamp.date()
                    or not 0
                    <= (date - restore_stamp.replace(second=0, microsecond=0)).total_seconds()
                    <= 120
                ):
                    raise ValueError("恢复提示不是本助手当前新会话，不点击")
                if not grade_frames and type(recovery_grade) is int:
                    grade_frames.append(recovery_grade)
                click("dialog_confirm", "恢复", current)
                restored, entered, run_selected = True, True, True
            elif ui.device(resourceId=TARGET + ":id/tv_title", text="安全提示").count == 1:
                if (
                    not entered
                    or safety_confirmed
                    or not expected_zone
                    or ui.device(
                        resourceId=TARGET + ":id/tv_current_run_zone", text=expected_zone
                    ).count
                    != 1
                ):
                    raise ValueError("安全提示的本次跑区未核对，不进入")
                click("tv_sure", "进入乐跑", current)
                safety_confirmed = True
            elif not run_selected:
                click("tv_run", "乐跑", current)
                run_selected = True
            elif not entered:
                grade = field(tree, "tv_run_data")
                # 点击标签后，成绩数据/插屏广告异步到达；缺字段只等待，不重复点击标签。
                if grade is None or not re.fullmatch(r"[0-9]{1,4}", grade):
                    sleep(0.2)
                    continue
                grade_frames.append(int(grade))
                if len(grade_frames) >= 2:
                    if grade_frames[-1] != grade_frames[-2]:
                        raise ValueError("开始前成绩发生变化；不开始")
                    if on_grade is not None:
                        on_grade(grade_frames[-1], tree)
                    click("tv_enterRun", "开始乐跑", current)
                    entered = True
        else:
            # 不自动确认恢复、放弃记录、权限更改或身份验证提示。
            title = ui.device(resourceId=TARGET + ":id/tv_title", text="当前跑区合格标准")
            if title.count == 1:
                expected = {
                    "tv_standard_distance_value": "≥1.5公里",
                    "tv_standard_log_value": "3次",
                    "tv_standard_speed_value": "3`0``~10`0``分钟/公里",
                }
                if any(
                    ui.device(resourceId=TARGET + ":id/" + k, text=v).count != 1
                    for k, v in expected.items()
                ):
                    raise ValueError("本次正式规则变化；不开始")
                click("tv_cancle", "我知道了", current)
                rules_seen = True
            elif current == (TARGET, RUN_ACTIVITY):
                metrics = parse_run_metrics(tree)
                if pause_expected is not None:
                    if restored and parse_run_control_state(tree) == "running":
                        # Original app's normal Restore automatically restarts
                        # its timer. Suspend immediately before further checks.
                        click_button = ui.device(
                            resourceId=TARGET + ":id/tv_start_pause_continue_run"
                        )
                        bridge.check_device()
                        if click_button.count != 1 or focus() != current or authorize() is not True:
                            raise ValueError("正常恢复后的暂停控件未知，未发送其他动作")
                        click_button.long_click(duration=2)
                        sleep(0.2)
                        continue
                    if restored:
                        cards = parse_checkpoint_cards(tree)
                        if (
                            pause_expected.completed_checkpoints != 0
                            or metrics["completed_checkpoints"] != 0
                            or not pause_expected.distance_m - 10
                            <= metrics["distance_m"]
                            <= pause_expected.distance_m + 30
                            or not pause_expected.elapsed_s
                            <= metrics["elapsed_s"]
                            <= pause_expected.elapsed_s + 30
                            or parse_run_control_state(tree) != "paused"
                            or len(cards) != 1
                            or cards[0].number != 1
                            or cards[0].state not in {"pending", "assigned"}
                            or not grade_frames
                        ):
                            raise ValueError("恢复后计数/里程/用时不一致，保留暂停记录")
                        if on_restored is not None:
                            on_restored(tree, clock())
                        return grade_frames[-1]
                    if (
                        not entered
                        or not grade_frames
                        or parse_run_control_state(tree) != "paused"
                        or any(
                            metrics[k] != getattr(pause_expected, k)
                            for k in (
                                "distance_m",
                                "elapsed_s",
                                "completed_checkpoints",
                                "counter_label",
                            )
                        )
                        or parse_checkpoint_cards(tree) != pause_expected.checkpoint_cards
                    ):
                        raise ValueError("冷启动后原记录与本助手暂停凭据不一致；不开始或接管")
                    return grade_frames[-1]
                if (
                    entered
                    and not rules_seen
                    and not advised
                    and scene_helper is not None
                    and metrics["distance_m"] == 0
                    and metrics["elapsed_s"] == 0
                    and metrics["completed_checkpoints"] == 0
                ):
                    advised = True
                    began = clock()
                    if scene_helper.open_rules(bridge, ui, authorize) is not True:
                        raise ValueError("多模态未建议或未命中规则控件，不开始")
                    deadline += min(30, clock() - began)
                    continue
                if (
                    not entered
                    or not rules_seen
                    or metrics["distance_m"] != 0
                    or metrics["elapsed_s"] != 0
                    or metrics["completed_checkpoints"] != 0
                    or metrics["counter_label"] != "已打卡"
                ):
                    raise ValueError("不是本次已核实规则的正式零状态")
                return grade_frames[-1]
        sleep(0.2)
    raise ValueError("正式入口未到零状态；不重复入口或开始")
