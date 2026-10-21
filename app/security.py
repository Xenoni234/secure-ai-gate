"""Security controls: API-key auth, rate limiting, strict input validation."""
import hmac
import math
import threading
import time
from collections import defaultdict, deque

from app.chess_features import GameRejected, parse_side, read_single_game

MAX_PGN_BYTES = 20 * 1024
OOD_MARGIN = 0.25   # tolerated distance outside the training range, as a share of that range


class ValidationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def check_api_key(provided, expected):
    """Constant-time comparison to prevent timing attacks."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


class RateLimiter:
    """Sliding-window limiter. Slows down model probing and engine-analysis abuse (DoS)."""

    def __init__(self, limit, window_seconds=60):
        self.limit = limit
        self.window = window_seconds
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


def validate_game_input(pgn_text, side):
    """Untrusted PGN: size-capped, exactly one legal standard game. Returns (game, side)."""
    if not isinstance(pgn_text, str) or not pgn_text.strip():
        raise ValidationError("'pgn' must be a non-empty string")
    if len(pgn_text.encode("utf-8", errors="replace")) > MAX_PGN_BYTES:
        raise ValidationError(f"PGN must be at most {MAX_PGN_BYTES // 1024} KB", status=413)
    try:
        parse_side(side)
        game = read_single_game(pgn_text)
    except GameRejected as exc:
        raise ValidationError(str(exc)) from None   # fixed messages, never the raw input
    return game, side.strip().lower()


def validate_analyze_payload(payload):
    """JSON body for /api/v1/analyze. Returns (pgn_text, side, game)."""
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    if set(payload) - {"pgn", "side"}:
        raise ValidationError("Unexpected fields in request")
    game, side = validate_game_input(payload.get("pgn"), payload.get("side"))
    return payload["pgn"], side, game


def out_of_range_features(values, meta, margin=OOD_MARGIN):
    """Names of features that fall outside the training range (+/- margin)."""
    names = []
    for name, v, lo, hi in zip(meta["feature_names"], values, meta["feature_min"], meta["feature_max"]):
        span = (hi - lo) or 1.0
        if v < lo - margin * span or v > hi + margin * span:
            names.append(name)
    return names


def validate_features(payload, meta):
    """Reject malformed, non-numeric, non-finite and out-of-distribution feature vectors."""
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    if set(payload) - {"features"}:
        raise ValidationError("Unexpected fields in request")

    feats = payload.get("features")
    names = meta["feature_names"]
    if not isinstance(feats, dict):
        raise ValidationError("'features' must be an object mapping feature name to number")
    if set(feats) != set(names):
        raise ValidationError("'features' must contain exactly: " + ", ".join(names))

    values = []
    for name in names:
        v = feats[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValidationError("All features must be numbers")
        if not math.isfinite(v):
            raise ValidationError("Features must be finite")
        values.append(float(v))

    outliers = out_of_range_features(values, meta)
    if outliers:
        raise ValidationError(f"Feature '{outliers[0]}' is outside the expected range", status=422)
    return values
