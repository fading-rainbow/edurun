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
