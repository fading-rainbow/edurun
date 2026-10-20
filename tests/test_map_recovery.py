"""Synthetic geometry only; not emulator navigation or accepted-grade evidence."""

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from budao.checkpoint_vision import CheckpointPixels
from budao.map_recovery import MapRecovery, ReceivedAnchors, TraceRegistration, received_trace
from budao.models import GeoPoint, PixelPoint
from budao.routes import RoutePlanner, distance
from budao.simulation import demo_config


def registration():
    p = RoutePlanner(demo_config())
    base = np.array(p.xy(p.config.baseline[0]))
    pixels = [PixelPoint(x=100, y=100), PixelPoint(x=200, y=100), PixelPoint(x=150, y=150)]
    geos = [p.geo(tuple(base + np.array([v.x - 100, 100 - v.y]) * 0.4)) for v in pixels]
    r = TraceRegistration(p, pixels, geos)
    return p, r, pixels, geos


def test_three_anchor_similarity_and_separate_original_card_range_gate():
    p, r, _, geos = registration()
    assert max(r.fit_residuals_m) < 0.01
    target = PixelPoint(x=160, y=150)
    expected = p.geo(tuple(np.array(p.xy(geos[-1])) + [4, 0]))
    candidate, measured = r.validate_range(target, geos[-1], distance(geos[-1], expected))
    assert distance(candidate, expected) < 0.01 and measured == pytest.approx(4, abs=0.01)
    with pytest.raises(ValueError, match="独立距离"):
        r.validate_range(target, geos[-1], 50)


def test_collinear_or_inconsistent_trace_anchors_do_not_generate_a_path():
    p, _, pixels, geos = registration()
    with pytest.raises(ValueError):
        TraceRegistration(p, pixels, [geos[0], geos[0], geos[0]])
    altered = [geos[0], geos[1], GeoPoint(lon=geos[2].lon + 0.0004, lat=geos[2].lat)]
    with pytest.raises(ValueError):
        TraceRegistration(p, pixels, altered)


def test_received_trace_refuses_old_receiving_clocks_and_missing_corners(tmp_path):
    p = RoutePlanner(demo_config())
    f = tmp_path / "input.jsonl"
    point = p.config.baseline[0]
    rows = [
        {
            "android_observed_ns": 10_000_000_000,
            "gps_reception": {
                "registered": True,
                "has_accuracy": True,
                "accuracy_m": 1,
                "provider": "gps",
                "latitude": point.lat,
                "longitude": point.lon,
                "last_received_ns": 1,
            },
        }
        for _ in range(10)
    ]
    f.write_text("\n".join(json.dumps(x) for x in rows))
    with pytest.raises(ValueError):
        received_trace(f, p)


def trace_file(tmp_path, *, two_turns=False):
    p = RoutePlanner(demo_config())
    x0, y0, x1, y1 = p.region.bounds
    x, y = x0 + (x1 - x0) / 4, y0 + (y1 - y0) / 4
    length = min(x1 - x0, y1 - y0) / 5
    vertices = [(x, y), (x + length, y), (x + length, y + length)]
    if two_turns:
        vertices.append((x + length * 2, y + length))
    points = []
    for start, end in zip(vertices, vertices[1:], strict=False):
        for alpha in np.linspace(0, 1, 15):
            points.append(p.geo(tuple(np.asarray(start) * (1 - alpha) + np.asarray(end) * alpha)))
    rows = [
        {
            "frame_id": f"frame-{i}",
            "motion": {"request_id": "a" * 32},
            "android_observed_ns": 10_000_000_000,
            "gps_reception": {
                "registered": True,
                "has_accuracy": True,
                "accuracy_m": 1,
                "provider": "gps",
                "latitude": point.lat,
                "longitude": point.lon,
                "last_received_ns": 9_900_000_000,
            },
        }
        for i, point in enumerate(points)
    ]
    f = tmp_path / "trace.jsonl"
    f.write_text("\n".join(json.dumps(row) for row in rows))
    return p, f, points


def test_one_actual_turn_requires_explicit_matching_start_frame_and_request(tmp_path):
    p, f, points = trace_file(tmp_path)
    with pytest.raises(ValueError, match="开始帧"):
        received_trace(f, p)
    anchors = received_trace(f, p, start_frame_id="frame-0", start_request_id="a" * 32)
    assert anchors.far_kind == "start_marker"
    assert distance(anchors.far, points[0]) < 0.01
    assert distance(anchors.current, points[-1]) < 0.01
    for frame_id, request in (("other", "a" * 32), ("frame-0", "b" * 32)):
        with pytest.raises(ValueError):
            received_trace(f, p, start_frame_id=frame_id, start_request_id=request)


def test_two_well_separated_real_turns_need_no_start_marker_fallback(tmp_path):
    p, f, points = trace_file(tmp_path, two_turns=True)
    anchors = received_trace(f, p)
    assert anchors.far_kind == "turn"
    assert distance(anchors.near, points[29]) < 0.01
    assert distance(anchors.far, points[14]) < 0.01


def test_arbitrary_trace_endpoint_cannot_be_substituted_for_original_start(tmp_path):
    p, f, _points = trace_file(tmp_path)
    with pytest.raises(ValueError):
        received_trace(f, p, start_frame_id="frame-5", start_request_id="a" * 32)


def test_rounded_received_turn_can_use_long_support_without_inventing_corner(tmp_path):
    p, f, _ = trace_file(tmp_path)
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    start = GeoPoint(
        lon=rows[0]["gps_reception"]["longitude"], lat=rows[0]["gps_reception"]["latitude"]
    )
    x, y = p.xy(start)
    vertices = [(x, y), (x + 35, y), (x + 38.8, y + 3.8), (x + 38.8, y + 40)]
    points = []
    for a, b in zip(vertices, vertices[1:], strict=False):
        for alpha in np.linspace(0, 1, 15):
            points.append(p.geo(tuple(np.asarray(a) * (1 - alpha) + np.asarray(b) * alpha)))
    template = rows[0]
    received = []
    for i, point in enumerate(points):
        received.append(
            template
            | {
                "frame_id": f"rounded-{i}",
                "gps_reception": template["gps_reception"]
                | {"longitude": point.lon, "latitude": point.lat},
            }
        )
    f.write_text("\n".join(json.dumps(row) for row in received))
    original = f.read_bytes()
    anchors = received_trace(f, p, start_frame_id="rounded-0", start_request_id="a" * 32)
    assert anchors.far_kind == "start_marker"
    assert min(distance(anchors.near, point) for point in points) < 0.01
    assert distance(anchors.far, points[0]) < 0.01
    assert f.read_bytes() == original


def test_short_noise_on_straight_trace_cannot_become_supported_turn(tmp_path):
    p, f, _ = trace_file(tmp_path)
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    start = GeoPoint(
        lon=rows[0]["gps_reception"]["longitude"], lat=rows[0]["gps_reception"]["latitude"]
    )
    x, y = p.xy(start)
    for i, row in enumerate(rows):
        point = p.geo((x + i * 2, y + (0.5 if i % 2 else 0)))
        row["gps_reception"].update(longitude=point.lon, latitude=point.lat)
    f.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="转角不足"):
        received_trace(f, p, start_frame_id="frame-0", start_request_id="a" * 32)


def map_component(tmp_path, monkeypatch, confidences, *, approach_only=False):
    """合成协议接头；像素配准几何由前面的独立测试覆盖，不连接实际设备。"""
    from test_runtime import frame, witness

    from budao.target import TARGET

    planner = RoutePlanner(demo_config())
    start, target = planner.config.baseline[:2]
    if approach_only:
        area = (
            planner.roads.buffer(25)
            .difference(planner.roads.buffer(15))
            .intersection(planner.region)
        )
        target = planner.geo(area.representative_point().coords[0])
    evidence = tmp_path / ".local"
    evidence.mkdir()
    (evidence / "start-witness.json").write_text(witness().model_dump_json())
    (evidence / "input-observations.jsonl").write_text("")
    gestures, calls = [], []
    selector = SimpleNamespace(count=1, gesture=lambda *a, **kw: gestures.append(a))
    bridge = SimpleNamespace(
        serial=witness().serial, run=lambda *_: "Mode: NORMAL", check_device=lambda: None
    )
    view = frame(10, assigned="新点", distance_m=280, elapsed=104, control="paused")
    view = view.model_copy(
        update={
            "inspection": view.inspection.model_copy(
                update={"issues": ("start_controls_and_current_record_identity_unverified",)}
            )
        }
    )
    connector = SimpleNamespace(
        evidence=evidence,
        bridge=bridge,
        ui=SimpleNamespace(device=lambda **_: selector),
        capture=lambda **_: SimpleNamespace(
            frame=view,
            screenshot=Image.new("RGB", (720, 1280), "white"),
            hierarchy=f'<hierarchy><node package="{TARGET}"/></hierarchy>',
        ),
    )
    dispatcher = SimpleNamespace(
        planner=planner,
        route_key=None,
        worker=SimpleNamespace(
            state=SimpleNamespace(moving=False),
            engine=SimpleNamespace(position=start, planner=planner),
            lock=threading.RLock(),
        ),
        observer=SimpleNamespace(
            allowed_names=set(), navigation_candidates={}, approach_only_names=set()
        ),
    )
    monkeypatch.setattr(
        "budao.map_recovery.received_trace",
        lambda *_a, **_kw: ReceivedAnchors(start, target, start),
    )
    monkeypatch.setattr(
        "budao.map_recovery.TraceRegistration",
        lambda *_: SimpleNamespace(
            fit_residuals_m=[0, 0, 0], validate_range=lambda *_: (target, 120)
        ),
    )
    monkeypatch.setattr("budao.map_recovery.refined_target_center", lambda _im, p: p)
    confidence = iter(confidences)

    def locate(_png, **kwargs):
        calls.append(kwargs)
        return CheckpointPixels(
            confidence=next(confidence),
            current=PixelPoint(x=100, y=100),
            target=PixelPoint(x=200, y=100),
            near_turn=PixelPoint(x=100, y=200),
            far_turn=PixelPoint(x=200, y=200),
            road_path=[],
        )

    monkeypatch.setattr(
        "budao.map_recovery.CheckpointAdvisor.from_private_config",
        lambda *_: SimpleNamespace(locate=locate, close=lambda: None),
    )
    recovery = MapRecovery(tmp_path, evidence / "unused-private-config.json", connector, dispatcher)
    return recovery, dispatcher, gestures, calls, evidence


def test_visible_markers_use_existing_roads_without_guessing_hidden_map_roads(
    tmp_path, monkeypatch
):
    r, d, gestures, calls, evidence = map_component(tmp_path, monkeypatch, [0.99])
    original = d.planner
    r.resolve(lambda: True)
    assert not gestures and len(calls) == 1 and d.planner is original
    proof = json.loads((evidence / "map-candidate-1.json").read_text())
    assert proof["using_known_roads"] and proof["app_confirmed"] is False
    assert (
        json.loads((evidence / "map-advice-1.json").read_text())["navigation_authorized"] is False
    )


def test_unseen_markers_allow_only_one_map_zoom_and_two_distinct_observations(
    tmp_path, monkeypatch
):
    r, _d, gestures, calls, _evidence = map_component(tmp_path, monkeypatch, [0.1, 0.99])
    r.resolve(lambda: True)
    assert len(gestures) == 1 and len(calls) == 2


def test_low_confidence_stops_after_one_zoom_without_adding_candidate(tmp_path, monkeypatch):
    r, d, gestures, calls, evidence = map_component(tmp_path, monkeypatch, [0.1, 0.1])
    with pytest.raises(ValueError, match="置信度"):
        r.resolve(lambda: True)
    assert len(gestures) == 1 and len(calls) == 2 and not d.observer.navigation_candidates
    assert (evidence / "map-recovery-failures.jsonl").exists()


def test_invisible_road_allows_only_existing_road_approach_without_claiming_checkin(
    tmp_path, monkeypatch
):
    r, d, gestures, _calls, evidence = map_component(
        tmp_path, monkeypatch, [0.99], approach_only=True
    )
    r.resolve(lambda: True)
    proof = json.loads((evidence / "map-candidate-1.json").read_text())
    assert proof["using_known_roads"] and proof["approach_only"] and not proof["app_confirmed"]
    assert 15 <= proof["approach_gap_m"] <= 25 and not gestures
    assert d.observer.approach_only_names == {"新点"}


def test_exhausted_map_budget_does_not_send_or_zoom(tmp_path, monkeypatch):
    from budao.map_recovery import MAX_MAP_CALLS

    r, _d, gestures, calls, _evidence = map_component(tmp_path, monkeypatch, [])
    r.calls = MAX_MAP_CALLS
    with pytest.raises(ValueError, match="预算"):
        r.resolve(lambda: True)
    assert not gestures and not calls
