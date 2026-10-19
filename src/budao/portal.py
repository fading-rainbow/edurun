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


class RecordRecoveryRequired(ValueError):
    """没有显式绑定恢复凭据的记录提示；不能等待后盲点或当广告处理。"""


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
    on_progress=None,
):
    if not isinstance(bridge, MuMuBridge) or not callable(authorize):
        raise ValueError("正式入口仅支持独立MuMu及明确许可")
    if on_progress is not None and not callable(on_progress):
        raise ValueError("入口进度仅接受本机回调")
    operation_began = clock()
    deadline, maximum_deadline = operation_began + 90, operation_began + 180

    def progress(event):
        if on_progress is not None:
            on_progress({"event": event, "elapsed_s": round(max(0, clock() - operation_began), 3)})

    progress("connect_ui")
    ui = ui_factory(bridge)

    def focus():
        return focused_component(bridge.run("shell", "dumpsys", "window", "displays"))

    normal_pages = {
        (TARGET, MAIN),
        (TARGET, BEFORE),
        (TARGET, RUN_ACTIVITY),
        (TARGET, SPLASH),
        KNOWN_AD_COMPONENT,
        None,
    }

    def same_page(expected):
        observed = focus()
        if observed == expected:
            return True
        if observed in normal_pages:
            progress("resample_normal_transition")
            return False
        raise ValueError("必要认证或未知前台；不继续动作")

    def click(name, text, expected):
        progress("before_" + name)
        bridge.check_device()
        if clock() >= deadline or authorize() is not True:
            raise ValueError("入口许可或前台改变")
        if not same_page(expected):
            return False
        button = ui.device(resourceId=TARGET + ":id/" + name, text=text)
        count = button.count
        if count == 0:
            progress("resample_missing_control")
            return False
        if count != 1:
            raise ValueError("入口控件未知或不唯一")
        if clock() >= deadline or authorize() is not True:
            raise ValueError("入口控件读取后许可、前台或时限改变")
        if not same_page(expected):
            return False
        button.click()
        progress("after_" + name)
        return True

    initial = focus()
    progress("initial_focus_read")
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
        progress("app_open_requested")
    elif initial != (TARGET, MAIN):
        raise ValueError("正式入口必须从已登录首页开始，不接管旧运行")
    run_selected, entered, rules_seen = False, False, False
    grade_frames = []
    safety_confirmed = False
    advised = False
    restored = False
    ad_attempts, last_close = 0, None
    entry_attempts, ad_interrupted_entry = 0, False
    returned_home_grades = []
    while clock() < deadline and authorize() is True:
        current = focus()
        progress(
            "observe_"
            + {
                (TARGET, MAIN): "home",
                (TARGET, BEFORE): "pre_run",
                (TARGET, RUN_ACTIVITY): "run",
                (TARGET, SPLASH): "splash",
                KNOWN_AD_COMPONENT: "known_ad",
                None: "no_focus",
            }.get(current, "other")
        )
        if current is None or current == (TARGET, SPLASH):
            sleep(0.2)
            continue
        if current == KNOWN_AD_COMPONENT:
            ad_interrupted_entry |= entered
            dismiss_known_ad(bridge, ui, authorize=authorize)
            sleep(0.2)
            continue
        if current not in {(TARGET, MAIN), (TARGET, BEFORE), (TARGET, RUN_ACTIVITY)}:
            raise ValueError("必要认证或未知页面；不读取、不覆盖")
        tree = ui.device.dump_hierarchy()
        if not same_page(current):
            sleep(0.2)
            continue  # 丢弃旧树；正常异步切页不能变成错误，更不能用旧树点击。
        if current == (TARGET, MAIN):
            if pause_expected is None and field(tree, "dialog_confirm") == "恢复":
                progress("record_recovery_requires_user")
                raise RecordRecoveryRequired("原App显示恢复提示；未恢复、放弃或开始新记录")
            close = ui.device(resourceId=TARGET + ":id/ad_close", description="关闭")
            if close.count == 1:
                ad_interrupted_entry |= entered
                bridge.check_device()
                if focus() != current or authorize() is not True:
                    raise ValueError("广告关闭许可改变")
                close.click()
            elif (
                scene_helper is not None
                and callable(getattr(scene_helper, "close_ad", None))
                and scene_helper.opaque_overlay(tree)
            ):
                ad_interrupted_entry |= entered
                # 不透明全屏广告没有任何目标App资源ID；最多两次建议，两次点击间隔≥3秒。
                if last_close is not None and clock() - last_close < 3:
                    sleep(0.2)
                    continue
                if ad_attempts >= 2:
                    raise ValueError("广告建议预算已用完；未执行未确认的关闭")
                ad_attempts += 1
                began = clock()
                try:
                    sent = scene_helper.close_ad(bridge, ui, authorize)
                except RuntimeError:
                    raise ValueError("广告关闭建议不可用；不继续入口") from None
                if sent is not True:
                    # 非动作建议不是一次失败点击；过渡广告可能自行消失。
                    # 只继续采集，不放宽图形阈值、不重复启动，也不点击其他控件。
                    deadline = min(maximum_deadline, deadline + min(30, clock() - began))
                    last_close = clock()
                    progress("ad_not_actioned_wait_for_observation")
                    sleep(0.2)
                    continue
                deadline = min(maximum_deadline, deadline + min(30, clock() - began))
                last_close = clock()
                sleep(0.2)
                continue
            elif field(tree, "alert_dialog_content") == (
                "当前时间不是学校规定锻炼时段,你可以自由跑,自由跑不会关联成绩"
            ):
                progress("formal_window_closed")
                raise ValueError("当前不在正式锻炼时段；未开始自由跑")
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
                if not click("dialog_confirm", "恢复", current):
                    continue
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
                if not click("tv_sure", "进入乐跑", current):
                    continue
                safety_confirmed = True
                ad_interrupted_entry = False
                returned_home_grades.clear()
            elif (
                field(tree, "alert_dialog_content") is not None
                or field(tree, "dialog_confirm") is not None
            ):
                progress("home_dialog_requires_user")
                raise ValueError("首页有未确认的记录或认证提示；未发送其他动作")
            elif not run_selected:
                if not click("tv_run", "乐跑", current):
                    continue
                run_selected = True
            elif entered and ad_interrupted_entry:
                # 某些插屏吞掉的是“进入准备页”请求：广告退场后仍为相同首页，
                # 并非MapRun中的真实开始。仅在两帧首页/基线/标签一致后重进一次。
                if pause_expected is not None or safety_confirmed or rules_seen:
                    raise ValueError("广告后不是可重新进入的新准备流程")
                if any(
                    TARGET + ":id/" + name in tree
                    for name in ("alert_dialog_content", "dialog_confirm")
                ):
                    raise ValueError("广告后有未知记录或认证提示，不重新进入")
                grade = field(tree, "tv_run_data")
                if grade is None:
                    returned_home_grades.clear()
                    progress("waiting_home_after_ad")
                    sleep(0.2)
                    continue  # 广告退场的中间帧不是稳定首页，也不能被当成失败点击。
                if not grade_frames or grade != str(grade_frames[-1]):
                    raise ValueError("广告后首页、标签或原成绩基线未确认")
                if (
                    ui.device(resourceId=TARGET + ":id/tv_run", text="乐跑", selected=True).count
                    != 1
                    or ui.device(resourceId=TARGET + ":id/tv_enterRun", text="开始乐跑").count != 1
                ):
                    returned_home_grades.clear()
                    progress("waiting_home_after_ad")
                    sleep(0.2)
                    continue
                returned_home_grades.append(int(grade))
                if len(returned_home_grades) >= 2:
                    if entry_attempts >= 2:
                        raise ValueError("广告中断准备入口的重进预算已用完；未开始跑步")
                    entered, ad_interrupted_entry = False, False
                    returned_home_grades.clear()
                    progress("ad_returned_home_preparation_retry")
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
                    if not click("tv_enterRun", "开始乐跑", current):
                        continue
                    entered = True
                    entry_attempts += 1
        else:
            if not entered and entry_attempts > 0 and pause_expected is None:
                entered = True  # 上一个实际入口请求在广告退场后才完成；不是新的开始。
            ad_interrupted_entry = False
            returned_home_grades.clear()
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
                if not click("tv_cancle", "我知道了", current):
                    continue
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
                    and metrics["distance_m"] == 0
                    and metrics["elapsed_s"] == 0
                    and metrics["completed_checkpoints"] == 0
                ):
                    if not advised:
                        advised = True
                        if scene_helper is None:
                            raise ValueError("缺少本机规则控件处理器；不开始")
                        began = clock()
                        sent = scene_helper.open_rules(bridge, ui, authorize)
                        progress("rules_open_requested" if sent is True else "rules_no_action")
                        deadline = min(maximum_deadline, deadline + min(30, clock() - began))
                    sleep(0.2)
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
                progress("formal_zero_verified")
                return grade_frames[-1]
        sleep(0.2)
    progress("entry_timeout_or_cancel")
    raise ValueError("正式入口未到零状态；不重复入口或开始")
