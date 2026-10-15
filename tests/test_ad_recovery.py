"""合成图像/模拟桥接测试：不连接设备、不读取密钥或真实截图。"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw

from budao.auth_ui import LAUNCHER, MAIN, prepare_current_login
from budao.models import PixelPoint
from budao.mumu import MuMuBridge
from budao.portal import enter_formal_zero
from budao.target import RUN_ACTIVITY, TARGET
from budao.ui_advisor import (
    SceneAssistant,
    UIAdvice,
    UIAdvisor,
    UIAdvisorTemporaryError,
    close_patch_stable,
    opaque_overlay_tree,
    verify_close_glyph,
)

CENTER = (650, 100)
OPAQUE = (
    f'<hierarchy><node package="{TARGET}" resource-id="android:id/content" '
    'bounds="[0,0][720,1280]" visible-to-user="true"/>'
    '<node package="com.android.systemui" '
    'resource-id="com.android.systemui:id/status_bar" bounds="[0,0][720,56]"/></hierarchy>'
)


def ad_image(center=CENTER, radius=19, *, x=True, rim=True, plus=False, background=(210, 120, 40)):
    image = Image.new("RGB", (720, 1280), background)
    draw = ImageDraw.Draw(image)
    cx, cy = center
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(30, 30, 30),
        outline=(190, 190, 190) if rim else None,
    )
    if x:
        draw.line((cx - 9, cy - 9, cx + 9, cy + 9), fill="white", width=3)
        draw.line((cx - 9, cy + 9, cx + 9, cy - 9), fill="white", width=3)
    if plus:
        draw.line((cx - 9, cy, cx + 9, cy), fill="white", width=3)
        draw.line((cx, cy - 9, cx, cy + 9), fill="white", width=3)
    return image


def cta_image():
    image = Image.new("RGB", (720, 1280), (210, 120, 40))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((350, 60, 650, 140), 20, fill=(230, 60, 60))
    for left in range(400, 600, 30):
        draw.rectangle((left, 90, left + 18, 110), fill="white")
    return image


def square_image():
    image = Image.new("RGB", (720, 1280), (210, 120, 40))
    draw = ImageDraw.Draw(image)
    cx, cy = CENTER
    draw.rectangle((cx - 19, cy - 19, cx + 19, cy + 19), fill=(30, 30, 30), outline=(190,) * 3)
    draw.line((cx - 9, cy - 9, cx + 9, cy + 9), fill="white", width=3)
    draw.line((cx - 9, cy + 9, cx + 9, cy - 9), fill="white", width=3)
    return image


def dark_no_circle_image():
    image = Image.new("RGB", (720, 1280), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    cx, cy = CENTER
    draw.line((cx - 9, cy - 9, cx + 9, cy + 9), fill="white", width=3)
    draw.line((cx - 9, cy + 9, cx + 9, cy - 9), fill="white", width=3)
    return image


def test_valid_circular_x_is_verified_with_small_center_tolerance():
    assert verify_close_glyph(ad_image(), *CENTER)[:2] == CENTER
    assert verify_close_glyph(ad_image(rim=False), CENTER[0] + 4, CENTER[1] - 3)[:2] == CENTER
    assert verify_close_glyph(ad_image(radius=21), CENTER[0] - 2, CENTER[1] + 2)[:2] == CENTER


@pytest.mark.parametrize(
    ("image", "target"),
    [
        (ad_image(x=False), CENTER),
        (ad_image(x=False, plus=True), CENTER),
        (ad_image(), (CENTER[0] + 12, CENTER[1])),  # 不允许无约束搜索
        (cta_image(), (500, 100)),
        (square_image(), CENTER),
        (dark_no_circle_image(), CENTER),
        (ad_image(background=(255, 255, 255), x=False), (360, 640)),
        (Image.new("RGB", (720, 1280), "white"), CENTER),
        (ad_image(center=(10, 10)), (10, 10)),  # 靠近边缘无法核实完整圆形
    ],
)
def test_invalid_glyph_cta_or_wrong_location_is_rejected(image, target):
    assert verify_close_glyph(image, *target) is None


def test_patch_stability_detects_animation():
    glyph = verify_close_glyph(ad_image(), *CENTER)
    assert close_patch_stable(ad_image(), ad_image(), glyph)
    moving = ad_image()
    ImageDraw.Draw(moving).rectangle(
        (CENTER[0] - 19, CENTER[1] - 19, CENTER[0] + 19, CENTER[1]), fill="white"
    )
    assert not close_patch_stable(ad_image(), moving, glyph)


class Bridge(MuMuBridge):
    def __init__(self, focus=(TARGET, MAIN), version="4.3.0"):
        self.focus, self.version, self.calls = focus, version, 0

    def check_device(self):
        self.calls += 1

    def run(self, *args):
        if "package" in args:
            return f"versionName={self.version}\nversionCode=230 minSdk=21"
        return f"mCurrentFocus=Window{{1 u0 {self.focus[0]}/{self.focus[1]}}}"


class Advisor(UIAdvisor):
    def __init__(self, advice=None, hook=None):
        self.advice, self.hook, self.requests = advice, hook, []

    def advise(self, png):
        self.requests.append(png)
        if self.hook:
            self.hook()
        return self.advice

    def close(self):
        pass


def good_advice(**changes):
    value = {
        "screen": "ad",
        "confidence": 0.95,
        "action": "close_ad",
        "target": PixelPoint(x=CENTER[0] + 2, y=CENTER[1] - 1),
    }
    return UIAdvice(**(value | changes))


class Screen:
    def __init__(self, frames, trees):
        self.frames, self.trees, self.clicks = list(frames), list(trees), []
        self.device = SimpleNamespace(
            dump_hierarchy=self._tree, click=lambda x, y: self.clicks.append((x, y))
        )

    def _tree(self):
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def screenshot(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def assistant(advice=None, hook=None, ticks=None):
    scene = SceneAssistant(Advisor(advice or good_advice(), hook))
    scene.sleep = lambda _: None
    if ticks is not None:
        values = iter(ticks)
        scene.clock = lambda: next(values)
    return scene


def test_valid_close_sends_exactly_one_local_center_tap_after_fresh_validation():
    bridge, screen = Bridge(), Screen([ad_image()], [OPAQUE])
    scene = assistant()
    assert scene.close_ad(bridge, screen, lambda: True) is True
    assert screen.clicks == [CENTER]
    assert len(scene.client.requests) == 1


def test_optional_ad_upstream_503_is_no_action_not_an_unverified_tap():
    def unavailable():
        raise UIAdvisorTemporaryError("界面模型HTTP503；没有执行动作")

    scene = assistant(hook=unavailable)
    screen = Screen([ad_image()], [OPAQUE])
    assert scene.close_ad(Bridge(), screen, lambda: True) is False
    assert len(scene.client.requests) == 1 and not screen.clicks
    assert scene.calls == 1


@pytest.mark.parametrize(
    "advice",
    [
        good_advice(confidence=0.89),
        good_advice(action="wait", target=None),
        good_advice(action="enter_run"),
        good_advice(screen="run_ready"),
        good_advice(target=PixelPoint(x=720, y=100)),
        good_advice(target=PixelPoint(x=100, y=1280)),
        good_advice(target=PixelPoint(x=360, y=640)),  # 模型坐标不在真实X上
        good_advice(target=PixelPoint(x=CENTER[0] + 15, y=CENTER[1])),
    ],
)
def test_advice_mismatch_or_untrusted_pixel_never_taps(advice):
    screen = Screen([ad_image()], [OPAQUE])
    assert assistant(advice).close_ad(Bridge(), screen, lambda: True) is False
    assert screen.clicks == []


@pytest.mark.parametrize("image", [cta_image(), ad_image(x=False), square_image()])
def test_model_pointing_at_cta_or_invalid_glyph_never_taps(image):
    screen = Screen([image], [OPAQUE])
    advice = good_advice(target=PixelPoint(x=500, y=100))
    assert assistant(advice).close_ad(Bridge(), screen, lambda: True) is False
    assert screen.clicks == []


NORMAL = (
    f'<hierarchy><node package="{TARGET}" resource-id="android:id/content" '
    'bounds="[0,0][720,1280]"/>'
    f'<node package="{TARGET}" resource-id="{TARGET}:id/%s" text="%s" visible-to-user="true" '
    'bounds="[0,0][10,10]"/></hierarchy>'
)


@pytest.mark.parametrize(
    "tree",
    [
        NORMAL % ("tv_mine", "我的"),
        NORMAL
        % (
            "alert_dialog_content",
            "检测到您在2026年10月14日 10:00分开始跑步的记录未完成，是否恢复跑步?",
        ),
        NORMAL % ("dialog_confirm", "恢复"),
        NORMAL % ("tv_enterRun", "开始乐跑"),
        NORMAL % ("tv_end", "结束"),
        OPAQUE.replace("<hierarchy>", '<hierarchy><node package="x.y" resource-id=""/>'),
        OPAQUE.replace("/></hierarchy>", ' password="true"/></hierarchy>'),
        OPAQUE.replace('resource-id="android:id/content"', 'resource-id="android:id/other"'),
        OPAQUE.replace('visible-to-user="true"', 'text="请进行人脸验证，拍摄本人"'),
        "<hierarchy/>",
        "not xml",
    ],
)
def test_protected_restore_dialog_or_normal_page_is_not_eligible_and_never_uploaded(tree):
    assert not opaque_overlay_tree(tree)
    scene = assistant()
    screen = Screen([ad_image()], [tree])
    assert scene.close_ad(Bridge(), screen, lambda: True) is False
    assert not scene.client.requests and not screen.clicks


@pytest.mark.parametrize(
    "bridge",
    [
        Bridge((TARGET, RUN_ACTIVITY)),
        Bridge((LAUNCHER, LAUNCHER + ".LauncherActivity")),
        Bridge(version="4.2"),
    ],
)
def test_wrong_focus_or_version_refused_before_capture(bridge):
    scene = assistant()
    screen = Screen([ad_image()], [OPAQUE])
    with pytest.raises(ValueError):
        scene.close_ad(bridge, screen, lambda: True)
    assert not scene.client.requests and not screen.clicks


def test_authorization_missing_refused_and_revoked_after_slow_model_refused():
    screen = Screen([ad_image()], [OPAQUE])
    with pytest.raises(ValueError):
        assistant().close_ad(Bridge(), screen, lambda: False)
    assert not screen.clicks
    state = {"ok": True}
    scene = assistant(hook=lambda: state.update(ok=False))
    with pytest.raises(ValueError):
        scene.close_ad(Bridge(), screen, lambda: state["ok"])
    assert not screen.clicks and len(scene.client.requests) == 1


def test_focus_change_during_slow_model_refused():
    bridge, screen = Bridge(), Screen([ad_image()], [OPAQUE])
    scene = assistant(hook=lambda: setattr(bridge, "focus", ("com.tencent.mobileqq", "Login")))
    with pytest.raises(ValueError):
        scene.close_ad(bridge, screen, lambda: True)
    assert not screen.clicks


def test_fresh_frame_changes_or_ad_disappears_after_model_never_taps():
    for frames, trees in (
        ([ad_image(), ad_image(x=False)], [OPAQUE]),  # 新帧X消失
        ([ad_image(), ad_image(center=(300, 700))], [OPAQUE]),  # X移走
        ([ad_image()], [OPAQUE, NORMAL % ("tv_mine", "我的")]),  # 广告已关闭/页面变化
        ([ad_image(), ad_image(), ad_image(x=False)], [OPAQUE]),  # 第二帧不稳定
    ):
        screen = Screen(frames, trees)
        with pytest.raises(ValueError):
            assistant().close_ad(Bridge(), screen, lambda: True)
        assert screen.clicks == []


def test_unstable_close_patch_or_stale_sample_never_taps():
    changing = ad_image()
    ImageDraw.Draw(changing).rectangle(
        (CENTER[0] - 19, CENTER[1] - 19, CENTER[0] + 19, CENTER[1] - 12), fill=(30, 30, 30)
    )
    patch = ad_image()
    ImageDraw.Draw(patch).ellipse(
        (CENTER[0] - 8, CENTER[1] + 12, CENTER[0] + 8, CENTER[1] + 18), fill=(250, 250, 250)
    )
    screen = Screen([ad_image(), ad_image(), patch], [OPAQUE])
    # 相同图形但周围明显变化的补丁不被视为稳定
    scene = assistant()
    try:
        scene.close_ad(Bridge(), screen, lambda: True)
    except ValueError:
        assert screen.clicks == []
    screen = Screen([ad_image()], [OPAQUE])
    with pytest.raises(ValueError, match="过期"):
        assistant(ticks=[10, 13]).close_ad(Bridge(), screen, lambda: True)
    assert screen.clicks == []


def test_privacy_rejection_blocks_upload_and_tap():
    notification = OPAQUE.replace(
        "</hierarchy>",
        '<node package="com.android.systemui" '
        'resource-id="com.android.systemui:id/heads_up_container" bounds="bad"/></hierarchy>',
    )
    scene = assistant()
    screen = Screen([ad_image()], [notification])
    with pytest.raises(ValueError):
        scene.close_ad(Bridge(), screen, lambda: True)
    assert not scene.client.requests and not screen.clicks


def test_call_budget_is_bounded():
    scene = assistant()
    scene.calls = 8
    with pytest.raises(ValueError):
        scene.close_ad(Bridge(), Screen([ad_image()], [OPAQUE]), lambda: True)


def test_model_cannot_select_a_glyph_covered_by_a_new_notification():
    notification = OPAQUE.replace(
        "</hierarchy>",
        '<node package="com.android.systemui" '
        'resource-id="com.android.systemui:id/heads_up_container" '
        'bounds="[20,60][700,280]"/></hierarchy>',
    )
    scene = assistant()
    screen = Screen([ad_image()], [OPAQUE, notification])
    assert scene.close_ad(Bridge(), screen, lambda: True) is False
    sent = Image.open(io.BytesIO(scene.client.requests[0]))
    assert sent.getpixel(CENTER) == (0, 0, 0)
    assert not screen.clicks


# ---- auth_ui / portal 集成 ----


class Helper:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    opaque_overlay = staticmethod(opaque_overlay_tree)

    def close_ad(self, bridge, ui, authorize):
        self.calls += 1
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class MainBridge(MuMuBridge):
    def __init__(self, focus=(TARGET, MAIN)):
        self.focus, self.calls = focus, []

    def check_device(self):
        pass

    def run(self, *args):
        self.calls.append(args)
        if "package" in args:
            return "versionName=4.3.0\nversionCode=230 minSdk=21"
        return f"mCurrentFocus=Window{{1 u0 {self.focus[0]}/{self.focus[1]}}}"


class OpaqueUI:
    def __init__(self, tree=OPAQUE):
        self.tree, self.clicked = tree, []
        self.device = self

    def dump_hierarchy(self):
        return self.tree

    def __call__(self, **kwargs):
        return SimpleNamespace(count=0, click=lambda: self.clicked.append(kwargs))


def test_login_preparation_without_helper_is_backward_compatible():
    ui = OpaqueUI()
    result = prepare_current_login(
        MainBridge(),
        ui_factory=lambda _: SimpleNamespace(device=ui),
        clock=lambda: 0,
        sleep=lambda _: None,
    )
    assert result["state"] == "unknown" and not ui.clicked


@pytest.mark.parametrize(
    "outcome", [False, ValueError("PRIVATE_DETAIL"), RuntimeError("PRIVATE_KEY")]
)
def test_login_preparation_fails_closed_on_unconfirmed_close(outcome):
    helper, ui = Helper([outcome, outcome]), OpaqueUI()
    now = [0.0]

    def clock():
        now[0] += 0.5
        return now[0]

    result = prepare_current_login(
        MainBridge(),
        ui_factory=lambda _: SimpleNamespace(device=ui),
        clock=clock,
        sleep=lambda _: None,
        scene_helper=helper,
    )
    assert result["state"] == "unknown" and helper.calls == (2 if outcome is False else 1)
    assert "PRIVATE" not in str(result)


def test_login_preparation_ad_attempts_are_bounded_and_spaced():
    helper, ui = Helper([True, True, True]), OpaqueUI()
    now = {"t": 0.0}

    def clock():
        now["t"] += 0.5
        return now["t"]

    result = prepare_current_login(
        MainBridge(),
        ui_factory=lambda _: SimpleNamespace(device=ui),
        clock=clock,
        sleep=lambda _: None,
        scene_helper=helper,
    )
    assert helper.calls == 2 and result["state"] == "unknown"


def test_login_preparation_ignores_helper_for_normal_page_and_known_resources():
    helper = Helper([True])
    ui = OpaqueUI(NORMAL % ("tv_title", "我的"))
    result = prepare_current_login(
        MainBridge(),
        ui_factory=lambda _: SimpleNamespace(device=ui),
        clock=lambda: 0,
        sleep=lambda _: None,
        scene_helper=helper,
    )
    assert helper.calls == 0 and result["state"] == "unknown"


class PortalBridge(MainBridge):
    pass


def test_portal_closes_opaque_ad_at_most_twice_then_fails_without_entry_clicks():
    helper, ui = Helper([True, True, True]), OpaqueUI()
    now = {"t": 0.0}

    def clock():
        now["t"] += 0.4
        return now["t"]

    with pytest.raises(ValueError):
        enter_formal_zero(
            PortalBridge(),
            lambda: True,
            ui_factory=lambda _: SimpleNamespace(device=ui, __call__=ui.__call__),
            clock=clock,
            sleep=lambda _: None,
            scene_helper=helper,
        )
    assert helper.calls == 2 and not ui.clicked


@pytest.mark.parametrize("outcome", [False, RuntimeError("PRIVATE_KEY")])
def test_portal_unconfirmed_close_fails_closed_without_leaking(outcome):
    helper, ui = Helper([outcome, outcome]), OpaqueUI()
    now = [0.0]

    def clock():
        now[0] += 0.5
        return now[0]

    with pytest.raises(ValueError) as error:
        enter_formal_zero(
            PortalBridge(),
            lambda: True,
            ui_factory=lambda _: ui,
            clock=clock,
            sleep=lambda _: None,
            scene_helper=helper,
        )
    assert helper.calls == (2 if outcome is False else 1)
    assert "PRIVATE" not in str(error.value) and not ui.clicked


def test_login_transient_ad_no_action_can_be_followed_by_normal_logged_in_page(monkeypatch):
    ui = OpaqueUI()

    class TransientHelper(Helper):
        def close_ad(self, *_args):
            self.calls += 1
            ui.tree = NORMAL % ("tv_title", "我的")
            return False

    helper = TransientHelper([])
    monkeypatch.setattr(
        "budao.auth_ui.inspect_login_ui",
        lambda *_: {"state": "unknown" if ui.tree == OPAQUE else "logged_in_ui"},
    )
    result = prepare_current_login(
        MainBridge(),
        ui_factory=lambda _: ui,
        clock=lambda: 0,
        sleep=lambda _: None,
        scene_helper=helper,
    )
    assert result["state"] == "logged_in_ui"
    assert helper.calls == 1 and not ui.clicked


def test_portal_transient_ad_no_action_continues_to_real_rules_and_zero(monkeypatch):
    from test_portal import UI as EntryUI
    from test_portal import Bridge as EntryBridge

    from budao.portal import BEFORE

    b, events = EntryBridge(), []

    class TransitionUI(EntryUI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if b.phase == "transition":
                result.count = 0
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun":
                original = result.click

                def click():
                    original()
                    b.phase, b.focus = "transition", MAIN

                result.click = click
            return result

        def dump_hierarchy(self):
            return OPAQUE if b.phase == "transition" else super().dump_hierarchy()

    class TransientHelper(Helper):
        def close_ad(self, *_args):
            self.calls += 1
            b.phase, b.focus = "rules", BEFORE
            return False

    helper = TransientHelper([])
    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )
    result = enter_formal_zero(
        b,
        lambda: True,
        ui_factory=TransitionUI,
        clock=lambda: 0,
        sleep=lambda _: None,
        scene_helper=helper,
        on_progress=events.append,
    )
    assert result == 1 and helper.calls == 1
    assert b.clicks == ["tv_run", "tv_enterRun", "tv_cancle"]
    assert any(e["event"] == "ad_not_actioned_wait_for_observation" for e in events)


@pytest.mark.parametrize("intercept_count,grade_changes", [(1, False), (2, False), (1, True)])
@pytest.mark.parametrize("return_delay", [0, 2])
def test_ad_returned_to_same_home_reenters_preparation_once_not_actual_start(
    monkeypatch, intercept_count, grade_changes, return_delay
):
    from test_portal import UI as EntryUI
    from test_portal import Bridge as EntryBridge

    b, events = EntryBridge(), []
    state = {"entries": 0, "grade": "1", "t": 0.0, "delay": 0}

    class InterceptUI(EntryUI):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if b.phase in {"ad", "returning"}:
                result.count = 0
            if kwargs.get("resourceId") == TARGET + ":id/tv_enterRun":
                original = result.click

                def click():
                    original()
                    state["entries"] += 1
                    if state["entries"] <= intercept_count:
                        b.phase, b.focus = "ad", MAIN

                result.click = click
            return result

        def dump_hierarchy(self):
            if b.phase == "ad":
                return OPAQUE
            if b.phase == "returning":
                if state["delay"] > 0:
                    state["delay"] -= 1
                    return (
                        f'<hierarchy><node package="{TARGET}" '
                        f'resource-id="{TARGET}:id/loading"/></hierarchy>'
                    )
                b.phase = "main"
            return super().dump_hierarchy().replace('text="1"', f'text="{state["grade"]}"')

    class ReturningHelper(Helper):
        def close_ad(self, *_):
            self.calls += 1
            b.phase, b.focus = "returning" if return_delay else "main", MAIN
            state["delay"] = return_delay
            if grade_changes:
                state["grade"] = "2"
            return False

    helper = ReturningHelper([])

    def clock():
        state["t"] += 0.2
        return state["t"]

    monkeypatch.setattr(
        "budao.portal.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )

    def run():
        return enter_formal_zero(
            b,
            lambda: True,
            ui_factory=InterceptUI,
            clock=clock,
            sleep=lambda _: None,
            scene_helper=helper,
            on_progress=events.append,
        )

    if grade_changes or intercept_count == 2:
        with pytest.raises(ValueError):
            run()
    else:
        assert run() == 1
        assert b.clicks == ["tv_run", "tv_enterRun", "tv_enterRun", "tv_cancle"]
        assert any(e["event"] == "ad_returned_home_preparation_retry" for e in events)
    assert state["entries"] == (1 if grade_changes else 2)
    assert "tv_start_pause_continue_run" not in b.clicks


# ---- live_runtime ----


def test_fresh_prepare_login_lazily_passes_and_closes_private_helper(tmp_path, monkeypatch):
    from test_live_runtime import settings

    from budao.live_runtime import LiveRuntime

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(
        json.dumps(settings() | {"vision_config": ".local/vision.json"}), "utf-8"
    )
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    closed, seen = [], {}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    advisor = UIAdvisor("https://test.invalid/v1", "test-model", "PRIVATE_TEST_KEY", client)
    monkeypatch.setattr(
        UIAdvisor, "from_private_config", classmethod(lambda cls, root, path: advisor)
    )
    monkeypatch.setattr(client, "close", lambda: closed.append(True))

    def fake_prepare(bridge, **kwargs):
        seen.update(kwargs)
        return {"state": "unknown"}

    monkeypatch.setattr("budao.live_runtime.prepare_current_login", fake_prepare)
    assert runtime.prepare_login(object())["state"] == "unknown"
    assert isinstance(seen["scene_helper"], SceneAssistant) and closed == [True]

    def broken(cls, root, path):
        raise ValueError("本机视觉配置缺项；不回显密钥")

    monkeypatch.setattr(UIAdvisor, "from_private_config", classmethod(broken))
    seen.clear()
    runtime.prepare_login(object())
    assert seen["scene_helper"] is None


def test_fresh_prepare_login_without_vision_config_has_no_helper(tmp_path, monkeypatch):
    from test_live_runtime import settings

    from budao.live_runtime import LiveRuntime

    directory = tmp_path / ".local"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(settings()), "utf-8")
    runtime = LiveRuntime(tmp_path, Path(".local/profile.json"), Path(".local/run.json"))
    seen = {}
    monkeypatch.setattr(
        "budao.live_runtime.prepare_current_login",
        lambda bridge, **kw: seen.update(kw) or {"state": "unknown"},
    )
    runtime.prepare_login(object())
    assert seen == {"scene_helper": None}


def test_png_module_import_sanity():
    out = io.BytesIO()
    ad_image().save(out, format="PNG")
    assert out.getvalue().startswith(b"\x89PNG")
