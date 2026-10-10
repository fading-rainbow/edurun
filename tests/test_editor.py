from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from budao.editor import EditorServer
from budao.simulation import demo_config


@contextmanager
def editor(tmp_path):
    with EditorServer(0, tmp_path / ".local" / "routes") as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=3)


def headers(server):
    return {"Origin": server.origin, "X-Local-Token": server.token}


def test_local_page_and_validated_no_overwrite_save(tmp_path):
    with (
        editor(tmp_path) as server,
        httpx.Client(base_url=server.origin, trust_env=False) as client,
    ):
        page = client.get("/")
        assert page.status_code == 200 and "路线工作台" in page.text
        assert "__LOCAL_TOKEN__" not in page.text
        assert client.get("/vendor/leaflet.js").status_code == 200
        config = demo_config().model_dump(mode="json")
        result = client.post("/api/validate", headers=headers(server), json=config)
        assert result.status_code == 200 and result.json()["baseline_metres"] > 799
        assert not server.output.exists()
        paths = []
        for _ in range(2):
            result = client.post("/api/save", headers=headers(server), json=config)
            assert result.status_code == 200
            path = Path(result.json()["saved_local_path"])
            assert path.is_relative_to(server.output)
            assert json.loads(path.read_text())["coordinate_system"] == "WGS84"
            paths.append(path)
        assert paths[0] != paths[1]


@pytest.mark.parametrize("origin", ["http://evil.example", "null", ""])
def test_external_origin_cannot_save_routes(tmp_path, origin):
    with (
        editor(tmp_path) as server,
        httpx.Client(base_url=server.origin, trust_env=False) as client,
    ):
        response = client.post(
            "/api/save",
            headers={"Origin": origin, "X-Local-Token": server.token},
            json=demo_config().model_dump(mode="json"),
        )
        assert response.status_code == 403 and not server.output.exists()


def test_missing_token_invalid_routes_and_private_paths(tmp_path):
    with (
        editor(tmp_path) as server,
        httpx.Client(base_url=server.origin, trust_env=False) as client,
    ):
        assert client.post("/api/save", json={}).status_code == 403
        assert client.get("/.local/setup/runtime.json").status_code == 404
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
        config = demo_config().model_dump(mode="json")
        config["baseline"][1] = {"lon": 1, "lat": 1}
        response = client.post("/api/save", headers=headers(server), json=config)
        assert response.status_code == 400 and not server.output.exists()


def test_save_directory_must_be_private(tmp_path):
    with pytest.raises(ValueError):
        EditorServer(0, tmp_path / "tracked-routes")
