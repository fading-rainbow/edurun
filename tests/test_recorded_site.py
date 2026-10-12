import json
from pathlib import Path

import networkx as nx
import pytest

from budao.models import GeoPoint
from budao.recorded_site import command_trace, draft_network, prepare_recorded_site
from budao.routes import RoutePlanner, distance, path_length


def p(x, y):
    return GeoPoint(lon=x / 100000, lat=y / 100000)


def test_draft_uses_connected_existing_lines_and_continuous_reverse_return():
    traces = ((p(0, 0), p(50, 0), p(100, 0)), (p(50, -50), p(50, 0), p(50, 50)))
    route = draft_network(traces)
    planner = RoutePlanner(route)
    assert nx.is_connected(planner.graph)
    assert distance(route.baseline[0], route.baseline[-1]) == 0
    assert path_length(route.baseline) > 100
    assert route.corridors == traces
    for point in [p(50, -50), p(50, 50), p(100, 0)]:
        assert path_length(planner.plan(route.baseline[0], point)) > 0


def test_disconnected_traces_are_not_fixed_by_invented_bridge():
    with pytest.raises(ValueError, match="不添加"):
        draft_network(((p(0, 0), p(50, 0)), (p(500, 0), p(550, 0))))


def evidence():
    return {
        "record_submitted": False,
        "last_wgs84": p(10, 0).model_dump(),
        "observations": [
            {
                "t": 1,
                "prior_wgs84": p(0, 0).model_dump(),
                "app": {"counter_label": "已打卡", "elapsed_s": 1},
            },
            {
                "t": 2,
                "prior_wgs84": p(5, 0).model_dump(),
                "app": {"counter_label": "已打卡", "elapsed_s": 2},
            },
        ],
    }


def test_actual_command_reader_not_app_reported_position_or_unused_route_vertices(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(evidence()), "utf-8")
    assert command_trace(path) == (p(0, 0), p(5, 0), p(10, 0))


@pytest.mark.parametrize(
    "case", ["submitted", "time", "clock", "mode", "jump", "last_jump", "unknown"]
)
def test_incomplete_stale_or_teleported_command_evidence_is_rejected(tmp_path, case):
    value = evidence()
    if case == "submitted":
        value["record_submitted"] = True
    elif case == "time":
        value["observations"][1]["t"] = 0
    elif case == "clock":
        value["observations"][1]["app"]["elapsed_s"] = 0
    elif case == "mode":
        value["observations"][1]["app"]["counter_label"] = "热量(千卡)"
    elif case == "jump":
        value["observations"][1]["prior_wgs84"] = p(500, 0).model_dump()
    elif case == "last_jump":
        value["last_wgs84"] = p(500, 0).model_dump()
    else:
        del value["observations"][0]["prior_wgs84"]
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(value), "utf-8")
    with pytest.raises(ValueError, match="不推测"):
        command_trace(path)


def test_existing_private_profile_is_never_overwritten(tmp_path):
    root = tmp_path / ".local/profiles/current"
    root.mkdir(parents=True)
    profile = root / "profile.json"
    profile.write_text("KEEP", "utf-8")
    with pytest.raises(ValueError, match="保留原文件"):
        prepare_recorded_site(tmp_path)
    assert profile.read_text() == "KEEP"


def test_site_builder_does_not_claim_physical_roads_or_official_boundary():
    source = (Path(__file__).resolve().parents[1] / "src/budao/recorded_site.py").read_text("utf-8")
    assert "operator_roads_verified=False" in source
    assert (
        '"physical_roads_verified": False' in source
        and '"official_region_verified": False' in source
    )
    assert '"source_position_is_command_not_receiver_field": True' in source
    assert 'path.open("x"' in source
