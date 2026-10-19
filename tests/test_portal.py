"""合成入口协议测试，不连接App或声明现场验收。"""

from types import SimpleNamespace

import pytest

from budao.auth_ui import MAIN
from budao.mumu import MuMuBridge
from budao.portal import BEFORE, RecordRecoveryRequired, enter_formal_zero
from budao.target import RUN_ACTIVITY, TARGET


class Bridge(MuMuBridge):
    def __init__(self):
        self.focus, self.phase, self.clicks = MAIN, "main", []

    def check_device(self):
        pass

    def run(self, *_args, **_kwargs):
        return f"mCurrentFocus=Window{{1 u0 {TARGET}/{self.focus}}}"


class UI:
    def __init__(self, bridge):
        self.bridge, self.device = bridge, self

    def __call__(self, **kwargs):
        name = kwargs.get("resourceId", "").split("/")[-1]
        count = 0
        if self.bridge.phase == "main" and name in {"tv_run", "tv_enterRun", "tv_run_data"}:
            count = 1
        elif self.bridge.phase == "rules" and name in {
            "tv_title",
            "tv_cancle",
            "tv_standard_distance_value",
            "tv_standard_log_value",
            "tv_standard_speed_value",
        }:
            count = 1

        def click():
            self.bridge.clicks.append(name)
            if name == "tv_enterRun":
                self.bridge.phase, self.bridge.focus = "rules", BEFORE
            if name == "tv_cancle":
                self.bridge.phase, self.bridge.focus = "zero", RUN_ACTIVITY

        return SimpleNamespace(count=count, click=click, get_text=lambda: "1")

    def dump_hierarchy(self):
        return (
            f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
            f'resource-id="{TARGET}:id/tv_run_data" text="1"/></hierarchy>'
        )


def test_single_portal_clicks_rules_then_returns_zero_not_start(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )
    bridge = Bridge()
    enter_formal_zero(bridge, lambda: True, ui_factory=UI, sleep=lambda _: None)
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]


def test_existing_run_and_no_authorization_do_not_navigate():
    bridge = Bridge()
    bridge.focus = RUN_ACTIVITY
    with pytest.raises(ValueError):
        enter_formal_zero(bridge, lambda: True, ui_factory=UI)
    assert not bridge.clicks
    bridge.focus = MAIN
    with pytest.raises(ValueError):
        enter_formal_zero(bridge, lambda: False, ui_factory=UI)
    assert not bridge.clicks


def test_slow_normal_ui_can_reach_zero_after_old_40s_budget_without_repeating_actions(monkeypatch):
    now, events = [0.0], []

    class SlowUI(UI):
        def dump_hierarchy(self):
            now[0] += 12
            return super().dump_hierarchy()

    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )
    b = Bridge()
    assert (
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=SlowUI,
            clock=lambda: now[0],
            sleep=lambda _: None,
            on_progress=events.append,
        )
        == 1
    )
    assert 40 < now[0] < 90
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]
    assert events[-1]["event"] == "formal_zero_verified"
    assert all(set(e) == {"event", "elapsed_s"} for e in events)


def test_slow_selector_cannot_send_action_after_entry_deadline():
    now = [0.0]

    class ExpiringUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_run":
                now[0] = 100
            return result

    b = Bridge()
    with pytest.raises(ValueError, match="时限"):
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=ExpiringUI,
            clock=lambda: now[0],
            sleep=lambda _: None,
        )
    assert not b.clicks


def test_entry_staying_home_without_observed_ad_is_not_blindly_retried():
    now = [0.0]

    class DroppedEntryUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun":
                original = result.click

                def click():
                    original()
                    self.bridge.phase, self.bridge.focus = "main", MAIN

                result.click = click
            return result

    def clock():
        now[0] += 0.5
        return now[0]

    b = Bridge()
    with pytest.raises(ValueError, match="未到零状态"):
        enter_formal_zero(
            b, lambda: True, ui_factory=DroppedEntryUI, clock=clock, sleep=lambda _: None
        )
    assert b.clicks == ["tv_run", "tv_enterRun"]


@pytest.mark.parametrize("after_entry", [False, True])
def test_fresh_flow_record_popup_stops_without_restore_discard_or_ad_click(after_entry):
    now, events = [0.0], []

    class RecoveryUI(UI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            name = kwargs.get("resourceId", "").split("/")[-1]
            if self.bridge.phase == "dialog" and name == "ad_close":
                result.count = 1  # A background close button must not override the record prompt.
            if name == "tv_enterRun":

                def click():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = "dialog", MAIN

                result.click = click
            return result

        def dump_hierarchy(self):
            if self.bridge.phase == "dialog":
                return (
                    f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/alert_dialog_content" text="PRIVATE_SENSITIVE"/>'
                    f'<node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/dialog_confirm" text="恢复"/></hierarchy>'
                )
            return super().dump_hierarchy()

    def clock():
        now[0] += 0.1
        return now[0]

    b = Bridge()
    if not after_entry:
        b.phase = "dialog"
    with pytest.raises(RecordRecoveryRequired) as error:
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=RecoveryUI,
            clock=clock,
            sleep=lambda _: None,
            on_progress=events.append,
        )
    assert b.clicks == (["tv_run", "tv_enterRun"] if after_entry else [])
    assert now[0] < 15 and "PRIVATE_SENSITIVE" not in str(error.value)
    assert events[-1]["event"] == "record_recovery_requires_user"
    assert "PRIVATE_SENSITIVE" not in str(events)


def test_unknown_home_dialog_is_not_silently_waited_or_clicked_through():
    class DialogUI(UI):
        def dump_hierarchy(self):
            return (
                f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
                f'resource-id="{TARGET}:id/alert_dialog_content" text="PRIVATE_SENSITIVE"/>'
                f'<node package="{TARGET}" visible-to-user="true" '
                f'resource-id="{TARGET}:id/dialog_confirm" text="确定"/></hierarchy>'
            )

    b, events = Bridge(), []
    with pytest.raises(ValueError, match="首页有未确认") as error:
        enter_formal_zero(b, lambda: True, ui_factory=DialogUI, on_progress=events.append)
    assert not b.clicks and "PRIVATE_SENSITIVE" not in str(error.value)
    assert events[-1]["event"] == "home_dialog_requires_user"


def test_formal_window_closed_is_not_clicked_or_treated_as_free_run():
    class ClosedUI(UI):
        def dump_hierarchy(self):
            return (
                f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
                f'resource-id="{TARGET}:id/alert_dialog_content" '
                'text="当前时间不是学校规定锻炼时段,你可以自由跑,自由跑不会关联成绩"/></hierarchy>'
            )

    b, events = Bridge(), []
    with pytest.raises(ValueError, match="正式锻炼时段"):
        enter_formal_zero(
            b, lambda: True, ui_factory=ClosedUI, sleep=lambda _: None, on_progress=events.append
        )
    assert not b.clicks
    assert events[-1]["event"] == "formal_window_closed"


def test_nonzero_page_not_treated_as_new_session(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 1500,
            "elapsed_s": 650,
            "completed_checkpoints": 3,
            "counter_label": "已打卡",
        },
    )
    with pytest.raises(ValueError, match="零状态"):
        enter_formal_zero(Bridge(), lambda: True, ui_factory=UI, sleep=lambda _: None)


def test_cold_restore_requires_exact_saved_pause_and_never_clicks_real_start(monkeypatch):
    from test_runtime import frame

    saved = frame(10, control="paused", distance_m=150, elapsed=622).inspection
    metrics = {
        k: getattr(saved, k)
        for k in ("distance_m", "elapsed_s", "completed_checkpoints", "counter_label")
    }
    monkeypatch.setattr("budao.portal.parse_run_metrics", lambda _: metrics)
    monkeypatch.setattr("budao.portal.parse_run_control_state", lambda _: "paused")
    monkeypatch.setattr("budao.portal.parse_checkpoint_cards", lambda _: saved.checkpoint_cards)
    b = Bridge()
    baseline = []
    assert (
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=UI,
            sleep=lambda _: None,
            pause_expected=saved,
            on_grade=lambda value, tree: baseline.append(value),
        )
        == 1
    )
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"] and baseline == [1]
    metrics["elapsed_s"] = 0
    with pytest.raises(ValueError, match="暂停凭据不一致"):
        enter_formal_zero(
            Bridge(), lambda: True, ui_factory=UI, sleep=lambda _: None, pause_expected=saved
        )


def test_slow_grade_or_interstitial_waits_without_second_tab_or_start(monkeypatch):
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )

    class DelayedUI(UI):
        grade_reads = 0

        def __call__(self, **kwargs):
            control = super().__call__(**kwargs)
            if kwargs.get("resourceId") == TARGET + ":id/tv_run_data":
                self.grade_reads += 1
                if self.grade_reads <= 3:
                    control.count = 0
            return control

        def dump_hierarchy(self):
            if self.bridge.phase == "main":
                self.grade_reads += 1
                if self.grade_reads <= 3:
                    return "<hierarchy/>"
            return super().dump_hierarchy()

    bridge = Bridge()
    assert enter_formal_zero(bridge, lambda: True, ui_factory=DelayedUI, sleep=lambda _: None) == 1
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]


@pytest.mark.parametrize("interstitial", [False, True])
@pytest.mark.parametrize("delayed_transition", [False, True])
def test_safety_entry_is_single_and_requires_exact_zone(
    monkeypatch, interstitial, delayed_transition
):
    from test_ad_recovery import OPAQUE

    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )

    class SafetyUI(UI):
        def __call__(self, **kwargs):
            node = super().__call__(**kwargs)
            name = kwargs.get("resourceId", "").split("/")[-1]
            if self.bridge.phase == "safety":
                node.count = int(
                    name in {"tv_title", "tv_sure"}
                    or (
                        name == "tv_current_run_zone" and kwargs.get("text") == "当前跑区：测试跑区"
                    )
                )

                def click():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = (
                        ("pending_run", MAIN) if delayed_transition else ("rules", BEFORE)
                    )

                node.click = click
            elif name == "tv_enterRun":

                def entry():
                    self.bridge.clicks.append(name)
                    self.bridge.phase, self.bridge.focus = (
                        "ad" if interstitial else "safety",
                        MAIN,
                    )

                node.click = entry
            return node

        def dump_hierarchy(self):
            return OPAQUE if self.bridge.phase == "ad" else super().dump_hierarchy()

    bridge = Bridge()
    delayed_frames = [0]

    def sleep(_):
        if bridge.phase == "pending_run":
            delayed_frames[0] += 1
            if delayed_frames[0] >= 2:
                bridge.phase, bridge.focus = "rules", BEFORE

    def close_ad(current_bridge, *_):
        current_bridge.phase, current_bridge.focus = "safety", MAIN
        return False

    scene = SimpleNamespace(opaque_overlay=lambda tree: tree == OPAQUE, close_ad=close_ad)
    assert (
        enter_formal_zero(
            bridge,
            lambda: True,
            ui_factory=SafetyUI,
            sleep=sleep,
            expected_zone="当前跑区：测试跑区",
            scene_helper=scene,
        )
        == 1
    )
    assert bridge.clicks == ["tv_run", "tv_enterRun", "tv_sure", "tv_cancle"]
    with pytest.raises(ValueError, match="跑区未核对"):
        enter_formal_zero(
            Bridge(),
            lambda: True,
            ui_factory=SafetyUI,
            sleep=lambda _: None,
            expected_zone="当前跑区：其他跑区",
            scene_helper=scene,
        )


@pytest.mark.parametrize("card_state", ["pending", "assigned"])
def test_dialog_restore_autopauses_running_and_calls_restored_callback(monkeypatch, card_state):
    from datetime import datetime

    from test_runtime import frame

    from budao.target import CheckpointCard

    class RestoreBridge(Bridge):
        def __init__(self):
            super().__init__()
            self.phase = "dialog"
            self.focus = MAIN

    class RestoreUI:
        def __init__(self, bridge):
            self.bridge, self.device = bridge, self
            self.long_clicks = []

        def __call__(self, **kwargs):
            name = kwargs.get("resourceId", "").split("/")[-1]
            count = 0
            if self.bridge.phase == "dialog" and name in {"dialog_confirm", "alert_dialog_content"}:
                count = 1
            elif (
                self.bridge.phase in {"running", "paused"} and name == "tv_start_pause_continue_run"
            ):
                count = 1

            def click():
                self.bridge.clicks.append(name)
                if name == "dialog_confirm":
                    self.bridge.phase = "running"
                    self.bridge.focus = RUN_ACTIVITY

            def long_click(**_kw):
                self.long_clicks.append(name)
                self.bridge.phase = "paused"

            return SimpleNamespace(count=count, click=click, long_click=long_click)

        def dump_hierarchy(self):
            if self.bridge.phase == "dialog":
                return (
                    f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/alert_dialog_content" '
                    'text="检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?"/>'
                    f'<node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/dialog_confirm" text="恢复"/></hierarchy>'
                )
            return "<hierarchy/>"

    saved = frame(10, control="paused", distance_m=100, elapsed=500).inspection
    metrics = {
        "distance_m": 110,
        "elapsed_s": 515,
        "completed_checkpoints": 0,
        "counter_label": "已打卡",
    }
    b = RestoreBridge()
    monkeypatch.setattr("budao.portal.parse_run_metrics", lambda _: metrics)
    monkeypatch.setattr(
        "budao.portal.parse_run_control_state",
        lambda _: b.phase if b.phase in {"running", "paused"} else "unknown",
    )
    test_card = CheckpointCard(number=1, name="打卡点1", state=card_state, distance_m=10)
    monkeypatch.setattr("budao.portal.parse_checkpoint_cards", lambda _: [test_card])

    restored_calls = []
    res = enter_formal_zero(
        b,
        lambda: True,
        ui_factory=RestoreUI,
        sleep=lambda _: None,
        pause_expected=saved,
        restore_stamp=datetime(2026, 10, 15, 10, 30),
        recovery_grade=5,
        on_restored=lambda tree, clock: restored_calls.append((tree, clock)),
    )
    assert res == 5
    assert b.clicks == ["dialog_confirm"]
    assert len(restored_calls) == 1


@pytest.mark.parametrize(
    "case,dialog_text,stamp,pause_completed,metrics_override,card_override,expected_err",
    [
        (
            "date_mismatch",
            "检测到您在2026年10月14日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {},
            None,
            "不是本助手当前新会话",
        ),
        (
            "time_drift_gt_120s",
            "检测到您在2026年10月15日 10:35分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {},
            None,
            "不是本助手当前新会话",
        ),
        (
            "regex_mismatch",
            "检测到您的记录未完成",
            "2026-10-15T10:30:00",
            0,
            {},
            None,
            "缺少本次新开始日期",
        ),
        (
            "completed_pause_expected",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            1,
            {},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "completed_metrics",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {"completed_checkpoints": 1},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "distance_too_small",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {"distance_m": 89},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "distance_too_large",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {"distance_m": 131},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "elapsed_backward",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {"elapsed_s": 499},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "elapsed_too_large",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {"elapsed_s": 531},
            None,
            "计数/里程/用时不一致",
        ),
        (
            "cards_completed",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {},
            "completed",
            "计数/里程/用时不一致",
        ),
        (
            "cards_wrong_number",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {},
            "wrong_number",
            "计数/里程/用时不一致",
        ),
        (
            "cards_empty",
            "检测到您在2026年10月15日 10:30分开始跑步的记录未完成，是否恢复跑步?",
            "2026-10-15T10:30:00",
            0,
            {},
            "empty",
            "计数/里程/用时不一致",
        ),
    ],
)
def test_dialog_restore_rejections(
    monkeypatch,
    case,
    dialog_text,
    stamp,
    pause_completed,
    metrics_override,
    card_override,
    expected_err,
):
    from datetime import datetime

    from test_runtime import frame

    from budao.target import CheckpointCard

    class RestoreBridge(Bridge):
        def __init__(self):
            super().__init__()
            self.phase = "dialog"
            self.focus = MAIN

    class RestoreUI:
        def __init__(self, bridge):
            self.bridge, self.device = bridge, self

        def __call__(self, **kwargs):
            name = kwargs.get("resourceId", "").split("/")[-1]
            count = (
                1
                if (
                    (
                        self.bridge.phase == "dialog"
                        and name in {"dialog_confirm", "alert_dialog_content"}
                    )
                    or (
                        self.bridge.phase in {"running", "paused"}
                        and name == "tv_start_pause_continue_run"
                    )
                )
                else 0
            )

            def click():
                if name == "dialog_confirm":
                    self.bridge.phase = "running"
                    self.bridge.focus = RUN_ACTIVITY

            def long_click(**_kw):
                self.bridge.phase = "paused"

            return SimpleNamespace(count=count, click=click, long_click=long_click)

        def dump_hierarchy(self):
            if self.bridge.phase == "dialog":
                return (
                    f'<hierarchy><node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/alert_dialog_content" text="{dialog_text}"/>'
                    f'<node package="{TARGET}" visible-to-user="true" '
                    f'resource-id="{TARGET}:id/dialog_confirm" text="恢复"/></hierarchy>'
                )
            return "<hierarchy/>"

    saved = frame(10, control="paused", distance_m=100, elapsed=500).inspection
    if pause_completed != 0:
        saved = saved.model_copy(update={"completed_checkpoints": pause_completed})
    metrics = {
        "distance_m": 105,
        "elapsed_s": 510,
        "completed_checkpoints": 0,
        "counter_label": "已打卡",
    }
    metrics.update(metrics_override)
    b = RestoreBridge()
    monkeypatch.setattr("budao.portal.parse_run_metrics", lambda _: metrics)
    monkeypatch.setattr(
        "budao.portal.parse_run_control_state",
        lambda _: b.phase if b.phase in {"running", "paused"} else "unknown",
    )
    if card_override == "completed":
        cards = [CheckpointCard(number=1, name="点1", state="completed", distance_m=0)]
    elif card_override == "wrong_number":
        cards = [CheckpointCard(number=2, name="点2", state="assigned", distance_m=10)]
    elif card_override == "empty":
        cards = []
    else:
        cards = [CheckpointCard(number=1, name="点1", state="assigned", distance_m=10)]
    monkeypatch.setattr("budao.portal.parse_checkpoint_cards", lambda _: cards)

    with pytest.raises(ValueError, match=expected_err):
        enter_formal_zero(
            b,
            lambda: True,
            ui_factory=RestoreUI,
            sleep=lambda _: None,
            pause_expected=saved,
            restore_stamp=datetime.fromisoformat(stamp),
            recovery_grade=5,
        )
