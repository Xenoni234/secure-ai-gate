"""Model quality gates: the pipeline fails if the model catches fewer cheaters or accuses more fair players."""
import numpy as np

from app.chess_features import FEATURE_NAMES
from app.main import load_model
from app.train import (MAX_FALSE_ACCUSATION_RATE, MIN_ENGINE_RECALL, false_accusation_rate,
                       load_split, predict_labels)


def test_engine_recall_gate(trained_model):
    """Full-engine cheaters must be caught (partial cheaters are measured in test_adversarial)."""
    model, meta = load_model()
    _, X_te, _, y_te, _, ratio_te = load_split()
    pred = predict_labels(model, X_te, meta["threshold"])
    engine = (y_te == 1) & (ratio_te >= 0.999)
    assert pred[engine].mean() >= MIN_ENGINE_RECALL


def test_false_accusation_gate(trained_model):
    """Flagging an innocent player is the costly error, so it is gated separately."""
    model, meta = load_model()
    _, X_te, _, y_te, _, _ = load_split()
    pred = predict_labels(model, X_te, meta["threshold"])
    assert false_accusation_rate(y_te, pred) <= MAX_FALSE_ACCUSATION_RATE


def test_predictions_are_deterministic(trained_model):
    model, _ = load_model()
    _, X_te, _, _, _, _ = load_split()
    assert np.array_equal(model.predict_proba(X_te), model.predict_proba(X_te))


def test_metadata_complete(meta):
    for key in ["version", "sha256", "trained_at", "metrics", "threshold", "n_features",
                "feature_names", "feature_min", "feature_max", "class_names"]:
        assert key in meta
    assert meta["class_names"] == ["fair", "assisted"]
    assert meta["feature_names"] == FEATURE_NAMES
    assert 0 < meta["threshold"] < 1
    for key in ["recall_engine", "recall_assisted", "false_accusation_rate"]:
        assert key in meta["metrics"]
