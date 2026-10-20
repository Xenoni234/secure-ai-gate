"""Train the classifier, save it with a SHA-256 fingerprint, and enforce a quality gate."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
from sklearn.datasets import load_breast_cancer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "model.joblib"
META_PATH = ARTIFACT_DIR / "model_meta.json"
RANDOM_STATE = 42
MIN_ACCURACY = 0.93


def load_split():
    data = load_breast_cancer()
    X_tr, X_te, y_tr, y_te = train_test_split(
        data.data, data.target, test_size=0.2, stratify=data.target, random_state=RANDOM_STATE
    )
    return data, X_tr, X_te, y_tr, y_te


def main():
    data, X_tr, X_te, y_tr, y_te = load_split()

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(C=0.5, max_iter=5000, random_state=RANDOM_STATE)),
    ])
    model.fit(X_tr, y_tr)
    pred = model.predict(X_te)
    acc = accuracy_score(y_te, pred)
    f1 = f1_score(y_te, pred)

    ARTIFACT_DIR.mkdir(exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()

    meta = {
        "version": digest[:12],
        "sha256": digest,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "accuracy": round(acc, 4),
        "f1": round(f1, 4),
        "n_features": int(X_tr.shape[1]),
        "feature_names": [str(n) for n in data.feature_names],
        "feature_min": X_tr.min(axis=0).tolist(),
        "feature_max": X_tr.max(axis=0).tolist(),
        "class_names": [str(n) for n in data.target_names],
        "sample": [float(v) for v in X_te[0]],
    }
    META_PATH.write_text(json.dumps(meta, indent=2))

    print(f"Model {meta['version']}  accuracy={acc:.4f}  f1={f1:.4f}")
    if acc < MIN_ACCURACY:
        print(f"QUALITY GATE FAILED: accuracy {acc:.4f} < {MIN_ACCURACY}")
        sys.exit(1)
    print("Quality gate passed")


if __name__ == "__main__":
    main()