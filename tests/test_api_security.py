"""API security tests: auth, validation, malicious PGN, DoS limits, leakage, integrity."""
import io
import json
import re

import chess.pgn
import pytest

from app.chess_features import FEATURE_NAMES
from app.main import create_app
from app.train import META_PATH, MODEL_PATH
from tests.conftest import TEST_CONFIG, TEST_KEY

PREDICT = "/api/v1/predict"
ANALYZE = "/api/v1/analyze"
XSS = "<script>alert(1)</script>"
INJECTIONS = ["' OR 1=1 --", "; rm -rf /", "{{7*7}}", XSS, "__import__('os').system('id')"]


def feature_body(meta):
    """A typical, in-distribution feature vector (midpoint of the training range)."""
    return {"features": {name: (lo + hi) / 2 for name, lo, hi in
                         zip(meta["feature_names"], meta["feature_min"], meta["feature_max"])}}


def first_plies(pgn_text, n):
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    board = game.board()
    for move in list(game.mainline_moves())[:n]:
        board.push(move)
    return str(chess.pgn.Game.from_board(board))


def with_white_name(pgn_text, name):
    return re.sub(r'^\[White ".*"\]$', lambda _: f'[White "{name}"]', pgn_text, count=1, flags=re.M)


# ---------- basics + authentication ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json["status"] == "ok"


@pytest.mark.parametrize("url,body", [(PREDICT, {"features": {}}),
                                      (ANALYZE, {"pgn": "1. e4", "side": "white"})])
def test_missing_api_key_rejected(client, url, body):
    assert client.post(url, json=body).status_code == 401


@pytest.mark.parametrize("url", [PREDICT, ANALYZE])
def test_wrong_api_key_rejected(client, url):
    assert client.post(url, json={}, headers={"X-API-Key": "wrong"}).status_code == 401


# ---------- /api/v1/predict (no engine) ----------

def test_valid_predict(client, headers, meta):
    r = client.post(PREDICT, json=feature_body(meta), headers=headers)
    assert r.status_code == 200
    assert r.json["label"] in ("fair", "assisted")
    assert 0 <= r.json["confidence"] <= 1
    assert r.json["model_version"] == meta["version"]


def test_confidence_is_rounded(client, headers, meta):
    """Coarse confidences make model extraction / threshold probing harder."""
    c = client.post(PREDICT, json=feature_body(meta), headers=headers).json["confidence"]
    assert round(c, 2) == c


def test_missing_feature_rejected(client, headers, meta):
    b = feature_body(meta)
    del b["features"]["acpl"]
    assert client.post(PREDICT, json=b, headers=headers).status_code == 400


def test_extra_feature_rejected(client, headers, meta):
    b = feature_body(meta)
    b["features"]["elo"] = 2000
    assert client.post(PREDICT, json=b, headers=headers).status_code == 400


def test_features_must_be_object(client, headers):
    r = client.post(PREDICT, json={"features": [0.5] * len(FEATURE_NAMES)}, headers=headers)
    assert r.status_code == 400


def test_boolean_rejected(client, headers, meta):
    b = feature_body(meta)
    b["features"]["acpl"] = True
    assert client.post(PREDICT, json=b, headers=headers).status_code == 400


@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_strings_rejected(client, headers, meta, payload):
    b = feature_body(meta)
    b["features"]["acpl"] = payload
    r = client.post(PREDICT, json=b, headers=headers)
    assert r.status_code == 400
    assert payload not in r.get_data(as_text=True)   # input never reflected back


def test_injected_feature_name_not_reflected(client, headers):
    r = client.post(PREDICT, json={"features": {XSS: 1}}, headers=headers)
    assert r.status_code == 400
    assert XSS not in r.get_data(as_text=True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_rejected(client, headers, meta, bad):
    b = feature_body(meta)
    b["features"]["acpl"] = bad
    raw = json.dumps(b)   # emits NaN / Infinity literals
    r = client.post(PREDICT, data=raw, content_type="application/json", headers=headers)
    assert r.status_code == 400


def test_out_of_distribution_rejected(client, headers, meta):
    """Impossible feature values are blocked before reaching the model."""
    b = feature_body(meta)
    b["features"]["acpl"] = 1e6
    assert client.post(PREDICT, json=b, headers=headers).status_code == 422


def test_unexpected_fields_rejected(client, headers, meta):
    b = feature_body(meta)
    b["admin"] = True
    assert client.post(PREDICT, json=b, headers=headers).status_code == 400


def test_non_json_rejected(client, headers):
    r = client.post(PREDICT, data="features=1", content_type="text/plain", headers=headers)
    assert r.status_code == 415


def test_oversized_payload_rejected(client, headers):
    r = client.post(PREDICT, json={"features": [1.0] * 10000}, headers=headers)
    assert r.status_code == 413


# ---------- PGN validation: untrusted input (rejected before the engine starts) ----------

def analyze(client, headers, pgn, side="white", **extra):
    return client.post(ANALYZE, json={"pgn": pgn, "side": side, **extra}, headers=headers)


def test_malformed_pgn_rejected(client, headers):
    assert analyze(client, headers, "this is not a chess game").status_code == 400


def test_illegal_move_rejected(client, headers):
    r = analyze(client, headers, "1. e4 e5 2. Qh8 Nc6 *")
    assert r.status_code == 400
    assert "illegal" in r.json["error"].lower()


def test_too_short_game_rejected(client, headers, samples):
    pgn, side = samples["fair"]
    assert analyze(client, headers, first_plies(pgn, 12), side).status_code == 400


def test_too_few_analysable_moves_rejected(client, headers, samples):
    """30 plies passes the size check but leaves < 10 moves after skipping the opening."""
    pgn, side = samples["fair"]
    r = analyze(client, headers, first_plies(pgn, 30), side)
    assert r.status_code == 400
    assert "too short" in r.json["error"].lower()


def test_two_games_rejected(client, headers, samples):
    pgn, side = samples["fair"]
    r = analyze(client, headers, pgn + "\n\n" + pgn, side)
    assert r.status_code == 400
    assert "one game" in r.json["error"]


def test_oversized_pgn_rejected(client, headers, samples):
    pgn, side = samples["fair"]
    padded = pgn + "\n{" + "x" * 21000 + "}"
    assert analyze(client, headers, padded, side).status_code == 413


def test_invalid_side_rejected(client, headers, samples):
    pgn, _ = samples["fair"]
    assert analyze(client, headers, pgn, "red").status_code == 400


def test_unexpected_analyze_fields_rejected(client, headers, samples):
    pgn, side = samples["fair"]
    assert analyze(client, headers, pgn, side, depth=40).status_code == 400


@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_in_pgn_not_reflected(client, headers, payload):
    r = analyze(client, headers, payload)
    assert r.status_code == 400
    assert payload not in r.get_data(as_text=True)


# ---------- engine abuse / failure (DoS) ----------

def test_analysis_slots_exhausted_returns_503(trained_model, samples):
    app = create_app({**TEST_CONFIG, "MAX_CONCURRENT_ANALYSES": 0})
    pgn, side = samples["fair"]
    r = app.test_client().post(ANALYZE, json={"pgn": pgn, "side": side}, headers={"X-API-Key": TEST_KEY})
    assert r.status_code == 503


def test_engine_failure_returns_generic_503(client, headers, samples, monkeypatch, tmp_path):
    missing = tmp_path / "missing-stockfish"
    monkeypatch.setenv("STOCKFISH_PATH", str(missing))
    pgn, side = samples["fair"]
    r = analyze(client, headers, pgn, side)
    assert r.status_code == 503
    text = r.get_data(as_text=True)
    assert "missing-stockfish" not in text and "Traceback" not in text


# ---------- full analysis with Stockfish ----------

@pytest.mark.engine
def test_analyze_valid_pgn(client, headers, samples):
    pgn, side = samples["fair"]
    r = analyze(client, headers, pgn, side)
    assert r.status_code == 200
    assert r.json["label"] in ("fair", "assisted")
    assert set(r.json["features"]) == set(FEATURE_NAMES)
    assert round(r.json["confidence"], 2) == r.json["confidence"]


@pytest.mark.engine
def test_samples_get_expected_verdicts(client, headers, samples):
    """The demo samples the Selenium tests rely on are classified as intended."""
    for name, expected in (("fair", "fair"), ("suspicious", "assisted")):
        pgn, side = samples[name]
        assert analyze(client, headers, pgn, side).json["label"] == expected, name


@pytest.mark.engine
def test_xss_in_pgn_header_is_escaped_in_ui(client, samples):
    pgn, side = samples["fair"]
    r = client.post("/analyze-ui", data={"pgn": with_white_name(pgn, XSS), "side": side})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert XSS not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------- headers, leakage, rate limiting, integrity ----------

def test_security_headers(client):
    r = client.get("/")
    for h in ["X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
              "Referrer-Policy", "Cache-Control"]:
        assert h in r.headers
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]


def test_errors_do_not_leak_internals(client):
    for r in [client.get("/does-not-exist"), client.get(PREDICT), client.get(ANALYZE)]:
        text = r.get_data(as_text=True)
        assert r.status_code in (404, 405)
        assert "Traceback" not in text and "File \"" not in text


def test_rate_limiting(trained_model, meta):
    app = create_app({**TEST_CONFIG, "RATE_LIMIT": 3})
    c = app.test_client()
    codes = [c.post(PREDICT, json=feature_body(meta), headers={"X-API-Key": TEST_KEY}).status_code
             for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_analyze_rate_limiting(trained_model):
    """Engine analysis is expensive, so it has its own, stricter budget."""
    app = create_app({**TEST_CONFIG, "ANALYZE_RATE_LIMIT": 2})
    c = app.test_client()
    codes = [c.post(ANALYZE, json={"pgn": "1. e4 *", "side": "white"},
                    headers={"X-API-Key": TEST_KEY}).status_code for _ in range(4)]
    assert codes[:2] == [400, 400]      # counted even when the PGN is rejected
    assert codes[2:] == [429, 429]


def test_tampered_model_is_rejected(tmp_path, trained_model):
    bad = tmp_path / "model.joblib"
    bad.write_bytes(MODEL_PATH.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="integrity"):
        create_app({**TEST_CONFIG, "MODEL_PATH": bad, "META_PATH": META_PATH})
