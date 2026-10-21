"""Evasion-attack evaluation (OWASP ML01: Input Manipulation).

Cheaters are adversaries: a "smart cheater" uses the engine only part of the time to stay
under the radar. This measures how detection degrades as the cheat ratio drops.
"""
import json
from pathlib import Path

import numpy as np

from app.main import load_model
from app.train import false_accusation_rate, load_split, predict_labels

BUCKETS = [(0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7)]
MIN_ENGINE_DETECTION = 0.95


def in_bucket(ratio, lo, hi):
    upper = ratio <= hi + 1e-9 if hi == BUCKETS[-1][1] else ratio < hi
    return (ratio >= lo) & upper


def test_evasion_detection_curve(trained_model):
    model, meta = load_model()
    _, X_te, _, y_te, _, ratio_te = load_split()
    pred = predict_labels(model, X_te, meta["threshold"])

    curve = []
    for lo, hi in BUCKETS:
        mask = (y_te == 1) & in_bucket(ratio_te, lo, hi)
        n = int(mask.sum())
        curve.append({"cheat_ratio": f"{lo:.1f}-{hi:.1f}", "n": n,
                      "detection_rate": round(float(pred[mask].mean()), 4) if n else None})
    engine_mask = (y_te == 1) & (ratio_te >= 0.999)
    engine_rate = float(pred[engine_mask].mean())
    curve.append({"cheat_ratio": "1.0 (full engine)", "n": int(engine_mask.sum()),
                  "detection_rate": round(engine_rate, 4)})
    far = false_accusation_rate(y_te, pred)

    results = {"threshold": meta["threshold"], "false_accusation_rate": round(far, 4),
               "detection_by_cheat_ratio": curve}
    Path("reports").mkdir(exist_ok=True)
    Path("reports/adversarial.json").write_text(json.dumps(results, indent=2))
    print("Evasion curve:", json.dumps(results))

    assert engine_mask.sum() > 0
    assert engine_rate >= MIN_ENGINE_DETECTION
    rates = [c["detection_rate"] for c in curve[:-1] if c["detection_rate"] is not None]
    assert len(rates) >= 2
    assert rates[-1] >= rates[0]       # more engine use -> easier to catch
    assert engine_rate >= rates[0]     # full engine is easier to catch than light cheating


def test_feature_noise_robustness(trained_model):
    """Small measurement noise on features must not flip verdicts (stable decisions)."""
    model, meta = load_model()
    X_tr, X_te, _, _, _, _ = load_split()
    rng = np.random.default_rng(0)
    std = X_tr.std(axis=0)
    base = predict_labels(model, X_te, meta["threshold"])
    flip_rates = [
        (predict_labels(model, X_te + rng.normal(0, 0.02 * std, X_te.shape), meta["threshold"]) != base).mean()
        for _ in range(20)
    ]
    assert np.mean(flip_rates) <= 0.05
