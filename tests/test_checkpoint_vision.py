import io
import json

import httpx
import pytest
from PIL import Image, UnidentifiedImageError

from budao.checkpoint_vision import CheckpointAdvisor, CheckpointPixels


def png():
    out = io.BytesIO()
    Image.new("RGB", (400, 300)).save(out, format="PNG")
    return out.getvalue()


def answer():
    return {
        "confidence": 0.93,
        "current": {"x": 80, "y": 180},
        "target": {"x": 90, "y": 100},
        "near_turn": {"x": 130, "y": 160},
        "far_turn": {"x": 180, "y": 190},
        "road_path": [{"x": 80, "y": 180}, {"x": 90, "y": 100}],
    }


def client(value):
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})

    return CheckpointAdvisor(
        "https://example.invalid/v1",
        "model",
        "PRIVATE_TEST_VALUE",
        httpx.Client(transport=httpx.MockTransport(reply)),
    ), requests


def test_map_advice_is_pixels_only_and_uses_explicit_structured_image_request():
    c, requests = client(answer())
    advice = c.locate(png())
    assert advice.target.x == 90
    body = requests[0]
    assert body["store"] is False and body["response_format"]["json_schema"]["strict"]
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "PRIVATE_TEST_VALUE" not in json.dumps(body)
    with pytest.raises(ValueError):
        CheckpointPixels.model_validate(answer() | {"latitude": 30})


@pytest.mark.parametrize(
    "value",
    [
        answer() | {"target": {"x": 401, "y": 20}},
        answer() | {"road_path": [{"x": 1, "y": 301}]},
        {"action": "save", "valid": True},
    ],
)
def test_unbounded_pixels_and_grade_action_outputs_rejected(value):
    c, _ = client(value)
    with pytest.raises(RuntimeError):
        c.locate(png())


def test_invalid_image_is_not_sent():
    c, requests = client(answer())
    with pytest.raises(UnidentifiedImageError):
        c.locate(b"not an image")
    assert not requests
