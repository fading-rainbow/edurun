import json
import secrets

import pytest

from budao.research_mock import CHECKPOINTS, MockProtocol, run_mock_demo, signature


@pytest.fixture
def lab():
    engine = MockProtocol(clock=lambda: 1800000000)
    _, session = engine.handle("POST", "/mock/bootstrap", {}, b"")
    key = bytes.fromhex(session["demo_key"])

    def call(path, body=None, *, stamp="1800000000", nonce=None, tamper=False):
        raw = json.dumps(body or {}, sort_keys=True, separators=(",", ":")).encode()
        nonce = nonce or secrets.token_hex(16)
        headers = {
            "X-Lab-Session": session["session"],
            "X-Lab-Time": stamp,
            "X-Lab-Nonce": nonce,
            "X-Lab-Signature": signature(key, "POST", path, stamp, nonce, raw),
        }
        return engine.handle("POST", path, headers, raw + b" " if tamper else raw)

    return engine, call


def ready(call):
    assert call("/mock/identity-fixture", {"synthetic": True, "result": "pass"})[0] == 200
    assert call("/mock/run/start")[0] == 200


def finish(call):
    for point, distance, elapsed in zip(
        CHECKPOINTS, (300, 650, 1000), (180, 390, 600), strict=True
    ):
        assert (
            call(
                "/mock/run/progress",
                {"checkpoint": point, "distance_m": distance, "elapsed_s": elapsed},
            )[0]
            == 200
        )
    status, result = call("/mock/run/finish", {"valid": True})
    assert status == 200 and result["state"]["phase"] == "pending_review"
    assert result["state"]["effective_count"] == 0


def test_identity_and_incomplete_client_claims_do_not_bypass_server(lab):
    _, call = lab
    assert call("/mock/run/start")[1]["code"] == "IDENTITY_REQUIRED"
    ready(call)
    assert call("/mock/run/finish", {"valid": True})[1]["code"] == "INCOMPLETE"


def test_replay_is_rejected(lab):
    _, call = lab
    nonce = secrets.token_hex(16)
    assert (
        call("/mock/identity-fixture", {"synthetic": True, "result": "pass"}, nonce=nonce)[0] == 200
    )
    assert (
        call("/mock/identity-fixture", {"synthetic": True, "result": "pass"}, nonce=nonce)[1][
            "code"
        ]
        == "REPLAY"
    )


def test_tampered_body_is_rejected_before_nonce_consumption(lab):
    _, call = lab
    nonce = secrets.token_hex(16)
    assert call("/mock/run/start", nonce=nonce, tamper=True)[1]["code"] == "SIGNATURE"
    assert call("/mock/run/start", nonce=nonce)[1]["code"] == "IDENTITY_REQUIRED"


@pytest.mark.parametrize("stamp", ["1799999800", "1800000200", "not-a-time"])
def test_clock_window(lab, stamp):
    _, call = lab
    assert call("/mock/run/start", stamp=stamp)[1]["code"] == "TIMESTAMP"


def test_checkpoint_order_and_monotonic_progress(lab):
    _, call = lab
    ready(call)
    assert (
        call("/mock/run/progress", {"checkpoint": "mock-b", "distance_m": 100, "elapsed_s": 60})[1][
            "code"
        ]
        == "CHECKPOINT_ORDER"
    )
    assert (
        call("/mock/run/progress", {"checkpoint": "mock-a", "distance_m": 300, "elapsed_s": 180})[0]
        == 200
    )
    assert (
        call("/mock/run/progress", {"checkpoint": "mock-b", "distance_m": 200, "elapsed_s": 190})[
            1
        ]["code"]
        == "PROGRESS_RANGE"
    )


@pytest.mark.parametrize("distance", [float("nan"), float("inf"), True, -1, 10**1000])
def test_invalid_progress(lab, distance):
    _, call = lab
    ready(call)
    assert (
        call(
            "/mock/run/progress", {"checkpoint": "mock-a", "distance_m": distance, "elapsed_s": 180}
        )[0]
        == 422
    )


def test_review_accepts_once_only(lab):
    _, call = lab
    ready(call)
    finish(call)
    result = call("/mock/review-fixture", {"synthetic": True, "decision": "accept"})[1]
    assert result["state"]["effective_count"] == 1
    duplicate = call("/mock/review-fixture", {"synthetic": True, "decision": "accept"})[1]
    assert duplicate["code"] == "WRONG_STATE"
    assert duplicate["state"]["effective_count"] == 1


def test_review_rejection_never_counts(lab):
    _, call = lab
    ready(call)
    finish(call)
    result = call("/mock/review-fixture", {"synthetic": True, "decision": "reject"})[1]
    assert result["state"]["phase"] == "rejected"
    assert result["state"]["effective_count"] == 0


def test_real_http_demo_is_isolated_and_omits_demo_keys():
    result = run_mock_demo()
    assert result["passed"] is True
    assert result["loopback_only"] is True
    assert result["real_app_records_created"] == 0
    assert result["pending_review_effective_count"] == 0
    assert result["accepted_effective_count"] == 1
    assert "demo_key" not in json.dumps(result)
    assert all(event["path"].startswith("/mock/") for event in result["events"])


def test_http_header_names_are_case_insensitive_and_non_ascii_signature_is_rejected():
    engine = MockProtocol(clock=lambda: 1800000000)
    _, session = engine.handle("POST", "/mock/bootstrap", {}, b"")
    raw, stamp, nonce = b"{}", "1800000000", secrets.token_hex(16)
    headers = {
        "x-lab-session": session["session"],
        "x-lab-time": stamp,
        "x-lab-nonce": nonce,
        "x-lab-signature": "非ASCII",
    }
    assert engine.handle("POST", "/mock/run/start", headers, raw)[1]["code"] == "SIGNATURE"
    headers["x-lab-signature"] = signature(
        bytes.fromhex(session["demo_key"]), "POST", "/mock/run/start", stamp, nonce, raw
    )
    assert engine.handle("POST", "/mock/run/start", headers, raw)[1]["code"] == "IDENTITY_REQUIRED"
