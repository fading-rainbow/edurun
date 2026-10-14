"""Synthetic geometry only; not emulator navigation or accepted-grade evidence."""

import json

import numpy as np
import pytest

from budao.map_recovery import TraceRegistration, received_trace
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
