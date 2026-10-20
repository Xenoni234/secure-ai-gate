"""API security tests: auth, validation, injection, DoS limits, leakage, integrity."""
import json

import pytest

from app.main import create_app
from app.train import META_PATH, MODEL_PATH
from tests.conftest import TEST_KEY

URL = "/api/v1/predict"


def body(meta):
    return {"features": list(meta["sample"])}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json["status"] == "ok"


def test_missing_api_key_rejected(client, meta):
    assert client.post(URL, json=body(meta)).status_code == 401


def test_wrong_api_key_rejected(client, meta):
    r = client.post(URL, json=body(meta), headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_valid_request(client, headers, meta):
    r = client.post(URL, json=body(meta), headers=headers)
    assert r.status_code == 200
    assert r.json["label"] in meta["class_names"]
    assert 0 <= r.json["confidence"] <= 1


def test_confidence_is_rounded(client, headers, meta):
    """Coarse confidences make model extraction / inversion harder."""
    c = client.post(URL, json=body(meta), headers=headers).json["confidence"]
    assert round(c, 2) == c


def test_wrong_feature_count(client, headers):
    r = client.post(URL, json={"features": [1.0, 2.0]}, headers=headers)
    assert r.status_code == 400


def test_boolean_rejected(client, headers, meta):
    b = body(meta); b["features"][0] = True
    assert client.post(URL, json=b, headers=headers).status_code == 400


@pytest.mark.parametrize("payload", [
    "' OR 1=1 --",
    "; rm -rf /",
    "{{7*7}}",
    "<script>alert(1)</script>",
    "__import__('os').system('id')",
])
def test_injection_strings_rejected(client, headers, meta, payload):
    b = body(meta); b["features"][0] = payload
    r = client.post(URL, json=b, headers=headers)
    assert r.status_code == 400
    assert payload not in r.get_data(as_text=True)   # input never reflected back


def test_nan_rejected(client, headers, meta):
    raw = json.dumps({"features": [float("nan")] + meta["sample"][1:]})
    r = client.post(URL, data=raw, content_type="application/json", headers=headers)
    assert r.status_code == 400


def test_infinity_rejected(client, headers, meta):
    raw = json.dumps({"features": [float("inf")] + meta["sample"][1:]})
    r = client.post(URL, data=raw, content_type="application/json", headers=headers)
    assert r.status_code == 400


def test_out_of_distribution_rejected(client, headers, meta):
    """Extreme adversarial inputs are blocked before reaching the model."""
    b = {"features": [v * 100 for v in meta["sample"]]}
    assert client.post(URL, json=b, headers=headers).status_code == 422


def test_unexpected_fields_rejected(client, headers, meta):
    b = body(meta); b["admin"] = True
    assert client.post(URL, json=b, headers=headers).status_code == 400


def test_non_json_rejected(client, headers):
    r = client.post(URL, data="features=1", content_type="text/plain", headers=headers)
    assert r.status_code == 415


def test_oversized_payload_rejected(client, headers):
    r = client.post(URL, json={"features": [1.0] * 5000}, headers=headers)
    assert r.status_code == 413


def test_security_headers(client):
    r = client.get("/")
    for h in ["X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
              "Referrer-Policy", "Cache-Control"]:
        assert h in r.headers


def test_errors_do_not_leak_internals(client):
    for r in [client.get("/does-not-exist"), client.get(URL)]:
        text = r.get_data(as_text=True)
        assert r.status_code in (404, 405)
        assert "Traceback" not in text and "File \"" not in text


def test_rate_limiting(trained_model, meta):
    app = create_app({"API_KEY": TEST_KEY, "RATE_LIMIT": 3, "TESTING": True})
    c = app.test_client()
    codes = [c.post(URL, json=body(meta), headers={"X-API-Key": TEST_KEY}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_tampered_model_is_rejected(tmp_path, trained_model):
    bad = tmp_path / "model.joblib"
    bad.write_bytes(MODEL_PATH.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="integrity"):
        create_app({"API_KEY": TEST_KEY, "MODEL_PATH": bad, "META_PATH": META_PATH})