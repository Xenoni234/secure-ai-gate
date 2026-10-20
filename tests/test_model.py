"""Model quality gates: the pipeline fails if the model gets worse."""
import numpy as np
from sklearn.metrics import accuracy_score, recall_score

from app.main import load_model
from app.train import MIN_ACCURACY, load_split


def test_accuracy_gate(trained_model):
    model, _ = load_model()
    _, _, X_te, _, y_te = load_split()
    assert accuracy_score(y_te, model.predict(X_te)) >= MIN_ACCURACY


def test_malignant_recall_gate(trained_model):
    """Missing a malignant case is the costly error, so recall on class 0 is gated."""
    model, _ = load_model()
    _, _, X_te, _, y_te = load_split()
    assert recall_score(y_te, model.predict(X_te), pos_label=0) >= 0.88


def test_predictions_are_deterministic(trained_model):
    model, _ = load_model()
    _, _, X_te, _, _ = load_split()
    assert np.array_equal(model.predict(X_te), model.predict(X_te))


def test_metadata_complete(meta):
    for key in ["version", "sha256", "accuracy", "n_features", "feature_min", "feature_max", "class_names"]:
        assert key in meta