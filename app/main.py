"""Chess Fair-Play Gate: Flask app serving an anti-cheat model behind security controls."""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

import joblib
import numpy as np
from flask import Flask, abort, jsonify, redirect, render_template, request
from werkzeug.exceptions import HTTPException

from app.chess_features import (FEATURE_LABELS, EngineUnavailable, GameRejected, analyze_game,
                                engine_available, read_single_game)
from app.lichess import LichessError, head_to_head_report
from app.security import (RateLimiter, ValidationError, check_api_key, out_of_range_features,
                          validate_analyze_payload, validate_features, validate_game_input)
from app.train import META_PATH, MODEL_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fair_play")

MAX_BODY_BYTES = 32 * 1024          # PGN itself is capped at 20 KB in security.py
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
SAMPLE_NAMES = ("fair", "suspicious")
BUSY_MESSAGE = "The analysis engine is busy or unavailable. Please try again shortly."
VERDICTS = {"fair": "Fair play", "assisted": "Suspicious — likely engine-assisted"}


class AnalysisUnavailable(Exception):
    """Engine missing, crashed, timed out, or all analysis slots busy (HTTP 503)."""


def load_model(model_path=MODEL_PATH, meta_path=META_PATH):
    """Verify the artifact's SHA-256 before unpickling (supply-chain / tampering defence)."""
    meta = json.loads(Path(meta_path).read_text())
    digest = hashlib.sha256(Path(model_path).read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, meta["sha256"]):
        raise RuntimeError("Model integrity check failed: artifact hash does not match metadata")
    return joblib.load(model_path), meta


def load_samples(directory=SAMPLES_DIR):
    """Demo games for the UI: {name: (pgn_text, side)}; side comes from the FairPlaySubject header."""
    samples = {}
    for name in SAMPLE_NAMES:
        path = Path(directory) / f"{name}_game.pgn"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            game = read_single_game(text)
        except GameRejected:
            log.warning("Ignoring invalid sample %s", path.name)
            continue
        side = game.headers.get("FairPlaySubject", "white").lower()
        samples[name] = (text, side if side in ("white", "black") else "white")
    return samples


def players_of(game):
    headers = game.headers
    return {key: headers.get(tag, "?")[:80]
            for key, tag in (("white", "White"), ("black", "Black"),
                             ("event", "Event"), ("result", "Result"))}


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        API_KEY=os.environ.get("API_KEY", ""),
        RATE_LIMIT=int(os.environ.get("RATE_LIMIT", "30")),                    # /predict per key
        ANALYZE_RATE_LIMIT=int(os.environ.get("ANALYZE_RATE_LIMIT", "10")),    # engine calls
        MAX_CONCURRENT_ANALYSES=int(os.environ.get("MAX_CONCURRENT_ANALYSES", "2")),
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        MODEL_PATH=MODEL_PATH,
        META_PATH=META_PATH,
        SAMPLES_DIR=SAMPLES_DIR,
    )
    if config:
        app.config.update(config)
    if not app.config["API_KEY"]:
        raise RuntimeError("API_KEY environment variable must be set")

    model, meta = load_model(app.config["MODEL_PATH"], app.config["META_PATH"])
    samples = load_samples(app.config["SAMPLES_DIR"])
    api_limiter = RateLimiter(app.config["RATE_LIMIT"])
    analyze_limiter = RateLimiter(app.config["ANALYZE_RATE_LIMIT"])
    ui_limiter = RateLimiter(app.config["ANALYZE_RATE_LIMIT"])
    # Each analysis spawns a Stockfish process: cap how many run at once (DoS defence).
    analysis_slots = threading.BoundedSemaphore(app.config["MAX_CONCURRENT_ANALYSES"])

    def run_model(values):
        p_assisted = float(model.predict_proba(np.array([values]))[0][1])
        label = "assisted" if p_assisted >= meta["threshold"] else "fair"
        return {
            "label": label,
            "verdict": VERDICTS[label],
            # Rounded confidence leaks less information (model extraction / threshold probing)
            "confidence": round(p_assisted if label == "assisted" else 1 - p_assisted, 2),
            "model_version": meta["version"],
        }

    def run_analysis(pgn_text, side):
        if not analysis_slots.acquire(blocking=False):
            log.warning("All analysis slots busy")
            raise AnalysisUnavailable()
        started = time.monotonic()
        try:
            features = analyze_game(pgn_text, side)
        except GameRejected as exc:
            raise ValidationError(str(exc)) from None
        except EngineUnavailable:
            log.exception("Engine analysis failed")      # details go to logs only
            raise AnalysisUnavailable() from None
        finally:
            analysis_slots.release()
        log.info("Analysed %d moves in %.1fs", features["n_moves_analysed"], time.monotonic() - started)
        values = [features[name] for name in meta["feature_names"]]
        return features, run_model(values), out_of_range_features(values, meta)

    def reject_oversized():
        if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
            abort(413)

    def require_api_key():
        api_key = request.headers.get("X-API-Key")
        if not check_api_key(api_key, app.config["API_KEY"]):
            log.warning("Unauthorized API call from %s", request.remote_addr)
            abort(401)
        return api_key

    def json_payload():
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if payload is None:
            raise ValidationError("Malformed JSON")
        return payload

    def render_page(status=200, **ctx):
        page = {"pgn": "", "side": "white", "result": None, "error": None,
                "players": None, "features": None, "unusual": None,
                "h2h": None, "h2h_error": None, "p1": "", "p2": "", "max_games": 10}
        page.update(ctx)
        return render_template("index.html", meta=meta, samples=samples,
                               feature_labels=FEATURE_LABELS, **page), status

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/health")
    def health():
        return jsonify(status="ok", model_version=meta["version"], engine=engine_available())

    @app.get("/")
    def index():
        name = request.args.get("sample")
        if name in samples:
            pgn_text, side = samples[name]
            return render_page(pgn=pgn_text, side=side)
        return render_page()

    @app.get("/analyze-ui")
    @app.get("/head-to-head-ui")
    def form_pages_redirect():
        # Reloading a result page sends a GET: go back to the form instead of a 405
        return redirect("/", code=303)

    @app.post("/analyze-ui")
    def analyze_ui():
        if not ui_limiter.allow(request.remote_addr or "unknown"):
            abort(429)
        pgn_text = request.form.get("pgn", "")
        side = request.form.get("side", "white")
        ctx = {"pgn": pgn_text, "side": side if side in ("white", "black") else "white"}
        try:
            game, ctx["side"] = validate_game_input(pgn_text, side)
            ctx["players"] = players_of(game)       # autoescaped by Jinja in the template
            features, result, unusual = run_analysis(pgn_text, ctx["side"])
            ctx.update(result=result, unusual=unusual,
                       features={k: round(v, 3) for k, v in features.items()})
            return render_page(**ctx)
        except ValidationError as exc:
            log.warning("UI request rejected: %s", exc.message)
            return render_page(status=exc.status, error=exc.message, **ctx)
        except AnalysisUnavailable:
            return render_page(status=503, error=BUSY_MESSAGE, **ctx)

    def run_head_to_head(p1, p2, max_games):
        if not analysis_slots.acquire(blocking=False):
            raise AnalysisUnavailable()
        try:
            return head_to_head_report(p1, p2, max_games)
        except (ValueError, TypeError) as exc:   # bad usernames / max_games
            raise ValidationError(str(exc)) from None
        except EngineUnavailable:
            log.exception("Engine failed during head-to-head")
            raise AnalysisUnavailable() from None
        finally:
            analysis_slots.release()

    @app.post("/head-to-head-ui")
    def head_to_head_ui():
        if not ui_limiter.allow(request.remote_addr or "unknown"):
            abort(429)
        p1, p2 = request.form.get("player1", ""), request.form.get("player2", "")
        max_games = request.form.get("max_games", "10")
        ctx = {"p1": p1, "p2": p2, "max_games": max_games}
        try:
            return render_page(h2h=run_head_to_head(p1, p2, max_games), **ctx)
        except ValidationError as exc:
            return render_page(status=exc.status, h2h_error=exc.message, **ctx)
        except LichessError as exc:
            return render_page(status=exc.status, h2h_error=exc.message, **ctx)
        except AnalysisUnavailable:
            return render_page(status=503, h2h_error=BUSY_MESSAGE, **ctx)

    @app.post("/api/v1/head-to-head")
    def head_to_head_api():
        reject_oversized()
        api_key = require_api_key()
        if not analyze_limiter.allow(api_key):
            abort(429)
        payload = json_payload()
        if not isinstance(payload, dict) or set(payload) - {"player1", "player2", "max_games"}:
            raise ValidationError("Expected fields: player1, player2, max_games")
        try:
            return jsonify(run_head_to_head(payload.get("player1"), payload.get("player2"),
                                            payload.get("max_games", 10)))
        except LichessError as exc:
            return jsonify(error=exc.message), exc.status

    @app.post("/api/v1/analyze")
    def analyze_api():
        reject_oversized()
        api_key = require_api_key()
        if not analyze_limiter.allow(api_key):
            log.warning("Analyze rate limit exceeded")
            abort(429)
        pgn_text, side, _ = validate_analyze_payload(json_payload())
        features, result, unusual = run_analysis(pgn_text, side)
        return jsonify(side=side, features={k: round(v, 4) for k, v in features.items()},
                       unusual_features=unusual, **result)

    @app.post("/api/v1/predict")
    def predict_api():
        reject_oversized()
        api_key = require_api_key()
        if not api_limiter.allow(api_key):
            log.warning("Rate limit exceeded")
            abort(429)
        values = validate_features(json_payload(), meta)
        return jsonify(run_model(values))

    @app.errorhandler(ValidationError)
    def handle_validation(exc):
        log.warning("API request rejected: %s", exc.message)
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(AnalysisUnavailable)
    def handle_unavailable(exc):
        return jsonify(error=BUSY_MESSAGE), 503

    @app.errorhandler(HTTPException)
    def handle_http(exc):
        return jsonify(error=exc.name), exc.code

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        log.exception("Unhandled error")          # details go to logs only
        return jsonify(error="Internal server error"), 500  # never leak stack traces

    return app
