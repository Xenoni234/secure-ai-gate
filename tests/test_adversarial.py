"""Adversarial robustness tests (OWASP ML01: Input Manipulation)."""
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import accuracy_score

from app.main import load_model
from app.train import load_split

RNG = np.random.default_rng(0)


def fgsm_predict(model, X, y, eps):
    """FGSM for logistic regression: push each sample against its true class
    by eps (in standardized units) along sign(weights)."""
    scaler, clf = model.named_steps["scaler"], model.named_steps["clf"]
    Z = scaler.transform(X)
    direction = np.sign(clf.coef_[0])              # raises score for class 1
    push = np.where(y == 1, -1.0, 1.0)[:, None]    # move away from the true class
    return clf.predict(Z + eps * push * direction)


def test_random_noise_robustness(trained_model):
    model, _ = load_model()
    _, X_tr, X_te, _, _ = load_split()
    std = X_tr.std(axis=0)
    base = model.predict(X_te)
    flip_rates = [
        (model.predict(X_te + RNG.normal(0, 0.02 * std, X_te.shape)) != base).mean()
        for _ in range(20)
    ]
    assert np.mean(flip_rates) <= 0.05


def test_fgsm_attack(trained_model):
    model, _ = load_model()
    _, _, X_te, _, y_te = load_split()
    clean = accuracy_score(y_te, model.predict(X_te))
    results = {"clean": round(clean, 4)}
    for eps in [0.01, 0.05, 0.1, 0.2, 0.5]:
        results[f"eps_{eps}"] = round(accuracy_score(y_te, fgsm_predict(model, X_te, y_te, eps)), 4)

    Path("reports").mkdir(exist_ok=True)
    Path("reports/adversarial.json").write_text(json.dumps(results, indent=2))
    print("Adversarial robustness:", results)

    assert results["eps_0.01"] >= clean - 0.05          # small attacks barely hurt
    assert results["eps_0.5"] <= results["eps_0.01"]    # attack strength behaves as expected