"""Train the fair-play classifier, save it with a SHA-256 fingerprint, and enforce quality gates.

Features were pre-computed by scripts/generate_dataset.py, so no chess engine is needed here
and CI training takes seconds.
"""
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, recall_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.chess_features import FEATURE_NAMES

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "fairplay.csv"
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"
META_PATH = ARTIFACT_DIR / "model_meta.json"
RANDOM_STATE = 42
CLASS_NAMES = ["fair", "assisted"]

MIN_ENGINE_RECALL = 0.90              # share of full-engine cheaters we must catch
MAX_FALSE_ACCUSATION_RATE = 0.10      # share of fair players we may wrongly flag
TARGET_FALSE_ACCUSATION_RATE = 0.08   # stricter target used to pick the threshold (safety margin)
# Recall on partial ("smart") cheaters is measured and reported, not gated: see test_adversarial.py


def load_dataset(path=DATA_PATH):
    if not Path(path).exists():
        sys.exit(f"{path} not found - run scripts/generate_dataset.py first")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    X = np.array([[float(r[name]) for name in FEATURE_NAMES] for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    cheat_ratio = np.array([float(r["cheat_ratio"]) for r in rows])
    return X, y, cheat_ratio


def load_split():
    """Stratified 80/20 split: X_tr, X_te, y_tr, y_te, ratio_tr, ratio_te."""
    X, y, cheat_ratio = load_dataset()
    return train_test_split(X, y, cheat_ratio, test_size=0.2, stratify=y,
                            random_state=RANDOM_STATE)


def build_model():
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                       class_weight="balanced", random_state=RANDOM_STATE,
                                       n_jobs=1)),
    ])


def predict_labels(model, X, threshold):
    """1 = assisted when P(assisted) >= threshold."""
    return (model.predict_proba(X)[:, 1] >= threshold).astype(int)


def false_accusation_rate(y_true, y_pred):
    fair = y_true == 0
    return float(np.mean(y_pred[fair] == 1)) if fair.any() else 0.0


def choose_threshold(X_tr, y_tr):
    """Lowest threshold whose out-of-fold false-accusation rate stays under the target.

    A false accusation harms an innocent player, so the threshold is driven by that rate
    first, and recall is maximised within it.
    """
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
    oof = cross_val_predict(build_model(), X_tr, y_tr, cv=cv, method="predict_proba")[:, 1]
    fair_scores = oof[y_tr == 0]
    for t in np.arange(0.20, 0.96, 0.01):
        t = round(float(t), 2)
        if np.mean(fair_scores >= t) <= TARGET_FALSE_ACCUSATION_RATE:
            return t
    return 0.95


def main():
    X_tr, X_te, y_tr, y_te, _, ratio_te = load_split()
    threshold = choose_threshold(X_tr, y_tr)
    model = build_model().fit(X_tr, y_tr)
    pred = predict_labels(model, X_te, threshold)
    recall = recall_score(y_te, pred, pos_label=1)
    far = false_accusation_rate(y_te, pred)
    engine = (y_te == 1) & (ratio_te >= 0.999)
    engine_recall = float(pred[engine].mean())

    ARTIFACT_DIR.mkdir(exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()

    meta = {
        "version": digest[:12],
        "sha256": digest,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "recall_engine": round(engine_recall, 4),
            "recall_assisted": round(float(recall), 4),
            "false_accusation_rate": round(far, 4),
            "accuracy": round(float(accuracy_score(y_te, pred)), 4),
            "f1": round(float(f1_score(y_te, pred)), 4),
            "n_train": int(len(y_tr)),
            "n_test": int(len(y_te)),
        },
        "threshold": threshold,
        "n_features": len(FEATURE_NAMES),
        "feature_names": list(FEATURE_NAMES),
        "feature_min": X_tr.min(axis=0).tolist(),
        "feature_max": X_tr.max(axis=0).tolist(),
        "class_names": CLASS_NAMES,
    }
    META_PATH.write_text(json.dumps(meta, indent=2))

    print(f"Model {meta['version']}  threshold={threshold:.2f}  "
          f"engine recall={engine_recall:.4f}  all-assisted recall={recall:.4f} (reported)  "
          f"false-accusation rate={far:.4f}")
    failed = []
    if engine_recall < MIN_ENGINE_RECALL:
        failed.append(f"engine recall {engine_recall:.4f} < {MIN_ENGINE_RECALL}")
    if far > MAX_FALSE_ACCUSATION_RATE:
        failed.append(f"false-accusation rate {far:.4f} > {MAX_FALSE_ACCUSATION_RATE}")
    if failed:
        print("QUALITY GATE FAILED: " + "; ".join(failed))
        sys.exit(1)
    print("Quality gates passed")


if __name__ == "__main__":
    main()
