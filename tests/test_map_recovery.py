"""Synthetic geometry only; not emulator navigation or accepted-grade evidence."""

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from budao.checkpoint_vision import CheckpointAdvisor, CheckpointPixels
from budao.map_recovery import (
    MapRecovery,
    ReceivedAnchors,
    ReceivedRangeFix,
    TraceRegistration,
    received_trace,
    same_map_viewport,
)
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


def test_async_card_range_uses_only_a_recent_real_fix_without_moving_estimated_target():
    p, r, _, geos = registration()
    target = PixelPoint(x=160, y=150)
    x, y = p.xy(geos[-1])
    previous = p.geo((x - 5, y))
    fix = ReceivedRangeFix(previous, "actual-receiver-frame", 9_000_000_000)
    estimate, matched = r.validate_range(
        target, geos[-1], 9, recent_fixes=(fix,), range_observed_ns=10_000_000_000
    )
    assert distance(estimate, r.map(target)) < 0.001
    assert matched == pytest.approx(9, abs=0.01) and r.range_reference is fix
    assert r.current_predicted_range_m == pytest.approx(4, abs=0.01)


@pytest.mark.parametrize("received_ns", [7_999_999_999, 10_000_000_001, True])
def test_old_future_or_untyped_callback_cannot_rescue_range(received_ns):
    p, r, _, geos = registration()
    x, y = p.xy(geos[-1])
    fix = ReceivedRangeFix(p.geo((x - 5, y)), "actual-receiver-frame", received_ns)
    with pytest.raises(ValueError):
        r.validate_range(
            PixelPoint(x=160, y=150),
            geos[-1],
            9,
            recent_fixes=(fix,),
            range_observed_ns=10_000_000_000,
        )


def test_range_cannot_search_a_third_historical_fix_or_far_teleport():
    p, r, _, geos = registration()
    x, y = p.xy(geos[-1])
    recent = ReceivedRangeFix(geos[-1], "recent", 9_900_000_000)
    matching = ReceivedRangeFix(p.geo((x - 5, y)), "unsearched-third", 9_000_000_000)
    with pytest.raises(ValueError):
        r.validate_range(
            PixelPoint(x=160, y=150),
            geos[-1],
            9,
            recent_fixes=(recent, recent, matching),
            range_observed_ns=10_000_000_000,
        )
    far = ReceivedRangeFix(p.geo((x - 20, y)), "far", 9_000_000_000)
    with pytest.raises(ValueError):
        r.validate_range(
            PixelPoint(x=160, y=150),
            geos[-1],
            24,
            recent_fixes=(far,),
            range_observed_ns=10_000_000_000,
        )


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


def test_range_references_are_only_two_latest_distinct_callbacks_within_two_seconds(tmp_path):
    p, f, _ = trace_file(tmp_path, two_turns=True)
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    for i, row in enumerate(rows):
        row["android_observed_ns"] = 10_000_000_000 + i * 1_000_000_000
        row["gps_reception"]["last_received_ns"] = row["android_observed_ns"] - 100_000_000
    repeated = rows[-1] | {"frame_id": "later-view-same-callback"}
    rows.append(repeated)
    f.write_text("\n".join(json.dumps(row) for row in rows))
    anchors = received_trace(f, p)
    assert [fix.frame_id for fix in anchors.recent_fixes] == [
        "later-view-same-callback",
        rows[-3]["frame_id"],
    ]
    assert [anchors.range_observed_ns - fix.received_ns for fix in anchors.recent_fixes] == [
        100_000_000,
        1_100_000_000,
    ]


def split_backtracked_trace(tmp_path):
    p, template_file, _ = trace_file(tmp_path)
    template = json.loads(template_file.read_text().splitlines()[0])
    x0, y0, x1, y1 = p.region.bounds
    x, y = x0 + (x1 - x0) / 4, y0 + (y1 - y0) / 4
    length = min(x1 - x0, y1 - y0) / 5
    vertices = [
        (x, y),
        (x + length, y),
        (x + length, y + length),
        (x + length, y + length * 0.3),
        (x + length, y + length * 0.7),
    ]
    points = []
    for a, b in zip(vertices, vertices[1:], strict=False):
        for alpha in np.linspace(0, 1, 15):
            points.append(p.geo(tuple(np.asarray(a) * (1 - alpha) + np.asarray(b) * alpha)))
    rows = []
    for i, point in enumerate(points):
        observed = (100 + i) * 1_000_000_000
        rows.append(
            template
            | {
                "frame_id": f"actual-{i}",
                "sampled_at": 100 + i,
                "android_observed_ns": observed,
                "gps_reception": template["gps_reception"]
                | {
                    "longitude": point.lon,
                    "latitude": point.lat,
                    "last_received_ns": observed - 100_000_000,
                },
            }
        )
    parent, child = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    parent.write_text("\n".join(json.dumps(row) for row in rows[:20]))
    child.write_text("\n".join(json.dumps(row) for row in rows[20:]))
    return p, parent, child, points, rows


def test_causal_merge_keeps_root_start_and_ignores_invisible_backtracking_reversals(tmp_path):
    p, parent, child, points, _ = split_backtracked_trace(tmp_path)
    originals = (parent.read_bytes(), child.read_bytes())
    anchors = received_trace(
        child, p, extra_paths=(parent,), start_frame_id="actual-0", start_request_id="a" * 32
    )
    assert anchors.far_kind == "start_marker"
    assert distance(anchors.far, points[0]) < 0.01
    assert distance(anchors.current, points[-1]) < 0.01
    assert distance(anchors.near, points[14]) < 0.01
    assert originals == (parent.read_bytes(), child.read_bytes())


@pytest.mark.parametrize(
    "change", ["device_clock", "host_clock", "missing_host_clock", "conflicting_frame"]
)
def test_merge_does_not_sort_rebooted_clocks_or_relabel_conflicting_frames(tmp_path, change):
    p, parent, child, _, rows = split_backtracked_trace(tmp_path)
    new = [dict(row) for row in rows[20:]]
    if change == "device_clock":
        new[0]["android_observed_ns"] = 1
    elif change == "host_clock":
        new[0]["sampled_at"] = 0
    elif change == "missing_host_clock":
        new[0].pop("sampled_at")
    else:
        new[0]["frame_id"] = rows[0]["frame_id"]
    child.write_text("\n".join(json.dumps(row) for row in new))
    with pytest.raises(ValueError):
        received_trace(
            child, p, extra_paths=(parent,), start_frame_id="actual-0", start_request_id="a" * 32
        )


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


def viewport(*, shift=0, marker_shift=0):
    image = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.line(
        [(40 + shift, 300), (200 + shift, 300), (200 + shift, 160)], fill=(0, 255, 0), width=8
    )
    x, y = 200 + marker_shift, 100
    draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill="black")
    draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="white")
    return image


def test_base_tile_loading_is_not_viewport_motion_when_actual_overlays_are_unchanged():
    before, after = viewport(), viewport()
    ImageDraw.Draw(after).rectangle((310, 20, 590, 200), fill=(180, 210, 240))
    rmse = np.sqrt(np.mean((np.asarray(before).astype(float) - np.asarray(after)) ** 2))
    assert rmse > 3
    assert same_map_viewport(before, after, PixelPoint(x=200, y=100))


@pytest.mark.parametrize("shift,marker_shift", [(4, 0), (0, 3), (10, 10)])
def test_trace_or_marker_movement_still_invalidates_viewport(shift, marker_shift):
    assert not same_map_viewport(
        viewport(), viewport(shift=shift, marker_shift=marker_shift), PixelPoint(x=200, y=100)
    )


def test_missing_or_unknown_overlay_cannot_pass_a_viewport_check():
    before = viewport()
    for after in (
        Image.new("RGB", before.size, "white"),
        Image.new("RGB", before.size, (0, 255, 0)),
        Image.new("RGB", (300, 200), "white"),
    ):
        assert not same_map_viewport(before, after, PixelPoint(x=200, y=100))


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
            fit_residuals_m=[0, 0, 0], validate_range=lambda *_a, **_kw: (target, 120)
        ),
    )
    monkeypatch.setattr("budao.map_recovery.refined_target_center", lambda _im, p: p)
    monkeypatch.setattr("budao.map_recovery.same_map_viewport", lambda *_: True)
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


def test_actual_viewport_change_still_blocks_candidate_publication(tmp_path, monkeypatch):
    r, d, _, _, _ = map_component(tmp_path, monkeypatch, [0.99])
    monkeypatch.setattr("budao.map_recovery.same_map_viewport", lambda *_: False)
    with pytest.raises(ValueError, match="视口改变"):
        r.resolve(lambda: True)
    assert not d.observer.navigation_candidates


def test_unseen_markers_allow_only_one_map_zoom_and_two_distinct_observations(
    tmp_path, monkeypatch
):
    r, _d, gestures, calls, _evidence = map_component(tmp_path, monkeypatch, [0.1, 0.99])
    r.resolve(lambda: True)
    assert len(gestures) == 1 and len(calls) == 2


def test_low_confidence_stops_after_two_zooms_without_adding_candidate(tmp_path, monkeypatch):
    r, d, gestures, calls, evidence = map_component(tmp_path, monkeypatch, [0.1, 0.1, 0.1])
    with pytest.raises(ValueError, match="置信度"):
        r.resolve(lambda: True)
    assert len(gestures) == 2 and len(calls) == 3 and not d.observer.navigation_candidates


def test_second_zoom_can_reveal_clipped_start_and_target_without_extra_total_budget(
    tmp_path, monkeypatch
):
    r, d, gestures, calls, _ = map_component(tmp_path, monkeypatch, [0.0, 0.0, 0.99])
    r.resolve(lambda: True)
    assert len(gestures) == 2 and len(calls) == 3
    assert "新点" in d.observer.navigation_candidates


def test_remaining_total_call_budget_can_stop_before_second_zoom(tmp_path, monkeypatch):
    r, d, gestures, calls, evidence = map_component(tmp_path, monkeypatch, [0.0])
    r.calls = 5
    with pytest.raises(ValueError):
        r.resolve(lambda: True)
    assert r.calls == 6 and len(calls) == 1 and not gestures
    assert not d.observer.navigation_candidates
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


def test_existing_road_approach_is_preferred_even_when_model_proposes_other_roads(
    tmp_path, monkeypatch
):
    r, d, _, _, evidence = map_component(tmp_path, monkeypatch, [0.99], approach_only=True)
    original = d.planner
    base_factory = CheckpointAdvisor.from_private_config

    def factory(*args):
        client = base_factory(*args)
        locate = client.locate

        def proposed(*a, **kw):
            advice = locate(*a, **kw)
            return advice.model_copy(update={"road_path": [advice.current, advice.target]})

        client.locate = proposed
        return client

    monkeypatch.setattr("budao.map_recovery.CheckpointAdvisor.from_private_config", factory)
    r.resolve(lambda: True)
    proof = json.loads((evidence / "map-candidate-1.json").read_text())
    assert d.planner is original and proof["using_known_roads"] and proof["approach_only"]
    assert not proof["app_confirmed"] and d.observer.approach_only_names == {"新点"}


def test_exhausted_map_budget_does_not_send_or_zoom(tmp_path, monkeypatch):
    from budao.map_recovery import MAX_MAP_CALLS

    r, _d, gestures, calls, _evidence = map_component(tmp_path, monkeypatch, [])
    r.calls = MAX_MAP_CALLS
    with pytest.raises(ValueError, match="预算"):
        r.resolve(lambda: True)
    assert not gestures and not calls
