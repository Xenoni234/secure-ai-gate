"""Secure AI Gate: Flask app serving an ML model behind security controls."""
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

import joblib
import numpy as np
from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.security import RateLimiter, ValidationError, check_api_key, validate_features
from app.train import META_PATH, MODEL_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("secure_ai")

MAX_BODY_BYTES = 4 * 1024
MAX_REF_LEN = 50


def load_model(model_path=MODEL_PATH, meta_path=META_PATH):
    """Verify the artifact's SHA-256 before unpickling (supply-chain / tampering defence)."""
    meta = json.loads(Path(meta_path).read_text())
    digest = hashlib.sha256(Path(model_path).read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, meta["sha256"]):
        raise RuntimeError("Model integrity check failed: artifact hash does not match metadata")
    return joblib.load(model_path), meta


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        API_KEY=os.environ.get("API_KEY", ""),
        RATE_LIMIT=int(os.environ.get("RATE_LIMIT", "30")),
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        MODEL_PATH=MODEL_PATH,
        META_PATH=META_PATH,
    )
    if config:
        app.config.update(config)
    if not app.config["API_KEY"]:
        raise RuntimeError("API_KEY environment variable must be set")

    model, meta = load_model(app.config["MODEL_PATH"], app.config["META_PATH"])
    api_limiter = RateLimiter(app.config["RATE_LIMIT"])
    ui_limiter = RateLimiter(app.config["RATE_LIMIT"])

    def run_model(values):
        proba = model.predict_proba(np.array([values]))[0]
        idx = int(np.argmax(proba))
        return {
            "label": meta["class_names"][idx],
            # Rounded confidence leaks less information (model inversion / extraction)
            "confidence": round(float(proba[idx]), 2),
            "model_version": meta["version"],
        }

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
        return jsonify(status="ok", model_version=meta["version"])

    @app.get("/")
    def index():
        sample = ", ".join(f"{v:g}" for v in meta["sample"]) if request.args.get("sample") else ""
        return render_template("index.html", features=sample, patient_ref="",
                               result=None, error=None, meta=meta)

    @app.post("/predict-ui")
    def predict_ui():
        if not ui_limiter.allow(request.remote_addr or "unknown"):
            abort(429)
        patient_ref = request.form.get("patient_ref", "")
        features_raw = request.form.get("features", "")
        result, error = None, None
        try:
            if len(patient_ref) > MAX_REF_LEN:
                raise ValidationError(f"Reference must be at most {MAX_REF_LEN} characters")
            try:
                values = [float(x) for x in features_raw.split(",") if x.strip()]
            except ValueError:
                raise ValidationError("Features must be comma-separated numbers") from None
            values = validate_features({"features": values}, meta)
            result = run_model(values)
        except ValidationError as exc:
            error = exc.message
            log.warning("UI request rejected: %s", exc.message)
        return render_template("index.html", features=features_raw, patient_ref=patient_ref,
                               result=result, error=error, meta=meta), (200 if result else 400)

    @app.post("/api/v1/predict")
    def predict_api():
        if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
            abort(413)
        api_key = request.headers.get("X-API-Key")
        if not check_api_key(api_key, app.config["API_KEY"]):
            log.warning("Unauthorized API call from %s", request.remote_addr)
            abort(401)
        if not api_limiter.allow(api_key):
            log.warning("Rate limit exceeded")
            abort(429)
        if not request.is_json:
            abort(415)
        payload = request.get_json(silent=True)
        if payload is None:
            raise ValidationError("Malformed JSON")
        values = validate_features(payload, meta)
        return jsonify(run_model(values))

    @app.errorhandler(ValidationError)
    def handle_validation(exc):
        log.warning("API request rejected: %s", exc.message)
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(HTTPException)
    def handle_http(exc):
        return jsonify(error=exc.name), exc.code

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        log.exception("Unhandled error")          # details go to logs only
        return jsonify(error="Internal server error"), 500  # never leak stack traces

    return app