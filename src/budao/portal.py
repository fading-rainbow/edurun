"""已采集4.3.0的正式入口；仅导航到零状态，不点击开始或恢复旧记录。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from .auth_ui import LAUNCHER, MAIN, SPLASH, target_task_tops
from .mumu import MuMuBridge
from .target import RUN_ACTIVITY, TARGET, parse_run_metrics
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
                grade = ui.device(resourceId=TARGET + ":id/tv_run_data")
                if grade.count > 1:
                    raise ValueError("当前已完成次数未确认；不开始")
                # 点击标签后，成绩数据/插屏广告异步到达；缺字段只等待，不重复点击标签。
                if grade.count == 0 or not re.fullmatch(r"[0-9]{1,4}", grade.get_text()):
                    sleep(0.2)
                    continue
                grade_frames.append(int(grade.get_text()))
                if len(grade_frames) >= 2:
                    if grade_frames[-1] != grade_frames[-2]:
                        raise ValueError("开始前成绩发生变化；不开始")
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
