"""Security controls: API-key auth, rate limiting, strict input validation."""
import hmac
import math
import threading
import time
from collections import defaultdict, deque


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
    """Sliding-window limiter. Slows down model-extraction and brute-force attempts."""

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


def validate_features(payload, meta, ood_margin=3.0):
    """Reject malformed, non-numeric, non-finite and out-of-distribution inputs."""
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    if set(payload) - {"features"}:
        raise ValidationError("Unexpected fields in request")

    feats = payload.get("features")
    if not isinstance(feats, list):
        raise ValidationError("'features' must be a list")
    if len(feats) != meta["n_features"]:
        raise ValidationError(f"Expected {meta['n_features']} features, got {len(feats)}")

    values = []
    for v in feats:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValidationError("All features must be numbers")
        if not math.isfinite(v):
            raise ValidationError("Features must be finite")
        values.append(float(v))

    for i, (v, lo, hi) in enumerate(zip(values, meta["feature_min"], meta["feature_max"])):
        span = hi - lo
        if v < lo - ood_margin * span or v > hi + ood_margin * span:
            raise ValidationError(
                f"Feature '{meta['feature_names'][i]}' is outside the expected range", status=422
            )
    return values