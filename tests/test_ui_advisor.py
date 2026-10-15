import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from budao.models import PixelPoint
from budao.mumu import MuMuBridge
from budao.target import RUN_ACTIVITY, TARGET
from budao.ui_advisor import SceneAssistant, UIAdvice, UIAdvisor, redacted_ui_image


def png():
    out = io.BytesIO()
    Image.new("RGB", (720, 1280), "white").save(out, format="PNG")
    return out.getvalue()


def test_redaction_covers_system_bar_and_identity_and_refuses_password():
    tree = (
        f'<hierarchy><node resource-id="{TARGET}:id/user_uid" '
        'bounds="[100,100][200,200]"/></hierarchy>'
    )
    value = redacted_ui_image(Image.new("RGB", (720, 1280), "white"), tree)
    image = Image.open(io.BytesIO(value))
    assert image.getpixel((10, 10)) == (0, 0, 0)
    assert image.getpixel((150, 150)) == (0, 0, 0)
    assert image.getpixel((300, 300)) == (255, 255, 255)
    with pytest.raises(ValueError):
        redacted_ui_image(image, '<hierarchy><node password="true"/></hierarchy>')


def test_post_screenshot_notification_is_also_masked_and_verification_refused():
    after = (
        '<hierarchy><node package="com.android.systemui" '
        'resource-id="com.android.systemui:id/heads_up_container" '
        'bounds="[10,80][600,280]"/></hierarchy>'
    )
    image = Image.new("RGB", (720, 1280), "white")
    result = Image.open(io.BytesIO(redacted_ui_image(image, "<hierarchy/>", after_hierarchy=after)))
    assert result.getpixel((50, 150)) == (0, 0, 0)
    assert result.getpixel((650, 500)) == (255, 255, 255)
    with pytest.raises(ValueError):
        redacted_ui_image(
            image,
            "<hierarchy/>",
            after_hierarchy='<hierarchy><node text="人脸验证"/></hierarchy>',
        )


def test_advice_cannot_contain_command_geo_or_save_action():
    for extra in ({"action": "save"}, {"command": "adb shell"}, {"lon": 120}):
        with pytest.raises(ValueError):
            UIAdvice.model_validate(
                {"screen": "run_ready", "confidence": 0.9, "action": "wait", **extra}
            )
    with pytest.raises(ValueError):
        UIAdvice(screen="run_ready", confidence=0.9, action="open_rules")


def test_live_protocol_payload_and_no_error_body_echo():
    requests = []
    advice = UIAdvice(
        screen="run_ready", confidence=0.9, action="open_rules", target=PixelPoint(x=52, y=880)
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": advice.model_dump_json()}}]}
        )

    client = UIAdvisor(
        "https://test.invalid/v1",
        "test-model",
        "PRIVATE_TEST_KEY",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.advise(png()) == advice
    assert requests[0]["store"] is False
    assert requests[0]["response_format"]["type"] == "json_schema"
    bad = UIAdvisor(
        "https://test.invalid/v1",
        "test-model",
        "PRIVATE_TEST_KEY",
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(401, text="PRIVATE_SERVER_BODY"))
        ),
    )
    with pytest.raises(RuntimeError, match="HTTP401") as error:
        bad.advise(png())
    assert "PRIVATE_SERVER_BODY" not in str(error.value)


class Bridge(MuMuBridge):
    def __init__(self):
        self.focus = (TARGET, RUN_ACTIVITY)

    def check_device(self):
        pass

    def run(self, *args):
        if "package" in args:
            return "versionName=4.3.0\nversionCode=230 minSdk=21"
        return f"mCurrentFocus=Window{{1 u0 {self.focus[0]}/{self.focus[1]}}}"


class Advisor(UIAdvisor):
    def __init__(self, bridge, target):
        self.bridge, self.target, self.moved = bridge, target, False

    def advise(self, _):
        if self.moved:
            self.bridge.focus = ("com.tencent.mobileqq", "LoginActivity")
        return UIAdvice(screen="run_ready", confidence=0.9, action="open_rules", target=self.target)


def test_model_pixel_must_hit_exact_live_rule_control(monkeypatch):
    monkeypatch.setattr(
        "budao.ui_advisor.parse_run_metrics",
        lambda _: {
            "distance_m": 0,
            "elapsed_s": 0,
            "completed_checkpoints": 0,
            "counter_label": "已打卡",
        },
    )
    bridge = Bridge()
    calls = []
    button = SimpleNamespace(
        count=1,
        info={"bounds": {"left": 23, "right": 81, "top": 851, "bottom": 909}},
        click=lambda: calls.append("rule"),
    )

    class Device:
        def dump_hierarchy(self):
            return "<hierarchy/>"

        def __call__(self, **kwargs):
            assert kwargs["resourceId"] == TARGET + ":id/img_show_must_know_tip"
            return button

    ui = SimpleNamespace(device=Device(), screenshot=lambda: Image.open(io.BytesIO(png())))
    assert SceneAssistant(Advisor(bridge, PixelPoint(x=52, y=880))).open_rules(
        bridge, ui, lambda: True
    )
    assert not SceneAssistant(Advisor(bridge, PixelPoint(x=360, y=1200))).open_rules(
        bridge, ui, lambda: True
    )
    moved = Advisor(bridge, PixelPoint(x=52, y=880))
    moved.moved = True
    with pytest.raises(ValueError):
        SceneAssistant(moved).open_rules(bridge, ui, lambda: True)
    assert calls == ["rule"]


def test_config_outside_private_project_refused(tmp_path):
    with pytest.raises(ValueError):
        UIAdvisor.from_private_config(tmp_path, Path("public.json"))


def _tree(*nodes):
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


def _node(package, rid="", bounds="[0,0][10,10]", text="", extra=""):
    return (
        f'<node package="{package}" resource-id="{rid}" bounds="{bounds}" text="{text}" {extra}/>'
    )


SYSTEMUI_PKG = "com.android.systemui"
SYNTHETIC_SCREEN = (720, 1280)


def _synthetic_image():
    image = Image.new("RGB", SYNTHETIC_SCREEN, "white")
    # 合成“私人头像/文字”标记，仅本地测试使用。
    for x in range(60, 120):
        for y in range(150, 210):
            image.putpixel((x, y), (255, 0, 0))
    return image


@pytest.mark.parametrize(
    ("rid", "bounds", "marker"),
    [
        (f"{SYSTEMUI_PKG}:id/heads_up_container", "[20,100][700,300]", (90, 180)),
        (f"{SYSTEMUI_PKG}:id/expandableNotificationRow", "[20,100][700,300]", (90, 180)),
        (f"{SYSTEMUI_PKG}:id/conversation_icon", "[40,140][140,220]", (90, 180)),
    ],
)
def test_systemui_notification_below_status_bar_is_masked(rid, bounds, marker):
    tree = _tree(
        _node(SYSTEMUI_PKG, f"{SYSTEMUI_PKG}:id/status_bar", "[0,0][720,56]"),
        _node(SYSTEMUI_PKG, rid, bounds, "PRIVATE_CHAT_TEXT"),
    )
    value = Image.open(io.BytesIO(redacted_ui_image(_synthetic_image(), tree)))
    assert value.getpixel(marker) == (0, 0, 0)
    assert value.getpixel((10, 10)) == (0, 0, 0)
    assert value.getpixel((400, 800)) == (255, 255, 255)


@pytest.mark.parametrize(
    "bounds",
    [
        "",
        "bad",
        "[0,0][0]",
        "[-1,100][100,200]",
        "[0,100][721,200]",
        "[0,100][100,1281]",
        "[50,50][10,10]",
    ],
)
def test_systemui_notification_missing_or_invalid_bounds_denied(bounds):
    tree = _tree(_node(SYSTEMUI_PKG, f"{SYSTEMUI_PKG}:id/heads_up_container", bounds))
    with pytest.raises(ValueError):
        redacted_ui_image(_synthetic_image(), tree)


def test_unclassified_systemui_node_without_bounds_is_denied():
    tree = _tree(_node(SYSTEMUI_PKG, "", "oops"))
    with pytest.raises(ValueError):
        redacted_ui_image(_synthetic_image(), tree)


@pytest.mark.parametrize(
    "rid",
    [
        f"{SYSTEMUI_PKG}:id/notification_panel",
        f"{SYSTEMUI_PKG}:id/notification_shade",
        f"{SYSTEMUI_PKG}:id/quick_settings_panel",
        "",
    ],
)
def test_full_systemui_shade_or_overlay_denied(rid):
    tree = _tree(_node(SYSTEMUI_PKG, rid, "[0,0][720,1280]"))
    with pytest.raises(ValueError):
        redacted_ui_image(_synthetic_image(), tree)


def test_status_bar_and_navigation_bar_remain_usable():
    tree = _tree(
        _node(SYSTEMUI_PKG, f"{SYSTEMUI_PKG}:id/status_bar", "[0,0][720,56]"),
        '<node package="com.android.systemui" resource-id="com.android.systemui:id/'
        'navigation_bar_frame" bounds="[0,1180][720,1280]">'
        '<node package="com.android.systemui" resource-id="" bounds="[0,1180][240,1280]"/>'
        '<node package="com.android.systemui" resource-id="com.android.systemui:id/home" '
        'bounds="[240,1180][480,1280]"/></node>',
        _node(TARGET, TARGET + ":id/map", "[0,200][720,1100]"),
    )
    value = Image.open(io.BytesIO(redacted_ui_image(_synthetic_image(), tree)))
    assert value.getpixel((300, 600)) == (255, 255, 255)
    assert value.getpixel((300, 1230)) == (255, 255, 255)
    assert value.getpixel((10, 10)) == (0, 0, 0)


def test_notification_inside_navigation_or_status_container_still_masked():
    tree = _tree(
        '<node package="com.android.systemui" resource-id="com.android.systemui:id/'
        'status_bar_container" bounds="[0,0][720,700]">'
        '<node package="com.android.systemui" resource-id="com.android.systemui:id/'
        'heads_up_status_bar_view" bounds="[20,100][700,300]"/></node>'
    )
    value = Image.open(io.BytesIO(redacted_ui_image(_synthetic_image(), tree)))
    assert value.getpixel((90, 180)) == (0, 0, 0)


@pytest.mark.parametrize(
    "tree",
    [
        _tree(_node(TARGET, TARGET + ":id/t", text="请进行人脸验证，拍摄本人")),
        _tree(_node(TARGET, TARGET + ":id/t", text="身份验证")),
        _tree(_node(TARGET, TARGET + ":id/t", text="请输入验证码")),
        _tree(_node(TARGET, TARGET + ":id/t", extra='content-desc="拍摄本人"')),
        _tree(_node("com.android.camera", "", "[0,0][720,1280]")),
        _tree(_node(TARGET, TARGET + ":id/liveness_view")),
        _tree(_node(TARGET, TARGET + ":id/face_verify_btn")),
        _tree(_node("com.tencent.mobileqq", "", "[0,100][720,400]")),
        "<hierarchy><node",
    ],
)
def test_verification_camera_foreign_or_malformed_trees_denied_before_upload(tree):
    with pytest.raises(ValueError):
        redacted_ui_image(_synthetic_image(), tree)


def test_exact_face_verification_dialog_denied_by_helper():
    tree = _tree(
        _node(
            TARGET,
            TARGET + ":id/alert_dialog_content",
            text="本次跑步需要进行人脸验证&#10;验证成功后可继续跑步",
            extra='visible-to-user="true"',
        ),
        _node(TARGET, TARGET + ":id/dialog_confirm", text="去验证", extra='visible-to-user="true"'),
    )
    with pytest.raises(ValueError):
        redacted_ui_image(_synthetic_image(), tree)


def test_normal_target_page_still_redacted_without_api_calls():
    tree = _tree(
        _node(TARGET, TARGET + ":id/user_uid", "[100,100][200,200]", "12345"),
        _node(TARGET, TARGET + ":id/map", "[0,300][720,900]"),
    )
    value = Image.open(io.BytesIO(redacted_ui_image(_synthetic_image(), tree)))
    assert value.getpixel((150, 150)) == (0, 0, 0)
    assert value.getpixel((400, 600)) == (255, 255, 255)
