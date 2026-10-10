import io
import json

import httpx
import pytest
from PIL import Image

from budao.vision import VisionClient, crop_image


def image():
    output = io.BytesIO()
    Image.new("RGB", (100, 100)).save(output, format="PNG")
    return output.getvalue()


def client(content, status=200):
    def handler(request):
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["response_format"]["type"] == "json_schema"
        assert "tools" not in body
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})

    return VisionClient(
        "https://vision.example.invalid/v1",
        "test-vision",
        "test-only-not-a-secret",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_schema_protocol_is_mocked_not_live():
    result = client(
        json.dumps(
            {
                "screen": "running",
                "confidence": 0.9,
                "checkpoint_number": 1,
                "checkpoint_pixel": {"x": 10, "y": 20},
            }
        )
    )
    assert result.observe(image()).checkpoint_pixel.x == 10
    result.close()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        '{"screen":"running","confidence":1,"lon":0}',
        '{"screen":"running","confidence":1,"checkpoint_number":1,"checkpoint_pixel":{"x":999,"y":10}}',
    ],
)
def test_invalid_visual_output_stops(content):
    with pytest.raises(RuntimeError):
        client(content).observe(image())


def test_http_failure_redacts_service_body():
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client("private-response", status=401).observe(image())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid?credential=value",
    ],
)
def test_endpoint_secrets_rejected(url):
    with pytest.raises(ValueError):
        VisionClient(url, "test", "placeholder")


def test_explicit_crop_only():
    original = Image.new("RGB", (200, 300))
    assert Image.open(io.BytesIO(crop_image(original, (10, 20, 110, 120)))).size == (100, 100)
    with pytest.raises(ValueError):
        crop_image(original, (-1, 0, 200, 300))
