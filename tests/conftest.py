import json

import pytest

from app.main import create_app
from app.train import META_PATH, MODEL_PATH
from app.train import main as train_main

TEST_KEY = "test-key-123"


@pytest.fixture(scope="session")
def trained_model():
    if not (MODEL_PATH.exists() and META_PATH.exists()):
        train_main()


@pytest.fixture
def app(trained_model):
    return create_app({"API_KEY": TEST_KEY, "RATE_LIMIT": 1000, "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def meta(trained_model):
    return json.loads(META_PATH.read_text())


@pytest.fixture
def headers():
    return {"X-API-Key": TEST_KEY}