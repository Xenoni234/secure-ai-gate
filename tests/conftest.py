import json

import pytest

from app.chess_features import engine_available
from app.main import create_app, load_samples
from app.train import META_PATH, MODEL_PATH
from app.train import main as train_main

TEST_KEY = "test-key-123"
TEST_CONFIG = {"API_KEY": TEST_KEY, "RATE_LIMIT": 1000, "ANALYZE_RATE_LIMIT": 1000, "TESTING": True}


def pytest_collection_modifyitems(config, items):
    """`engine` tests run wherever Stockfish exists (Mac, Jenkins); skipped only when it is missing."""
    if engine_available():
        return
    skip = pytest.mark.skip(reason="Stockfish not found: install it or set STOCKFISH_PATH")
    for item in items:
        if "engine" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def trained_model():
    if not (MODEL_PATH.exists() and META_PATH.exists()):
        train_main()


@pytest.fixture
def app(trained_model):
    return create_app(dict(TEST_CONFIG))


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def meta(trained_model):
    return json.loads(META_PATH.read_text())


@pytest.fixture
def headers():
    return {"X-API-Key": TEST_KEY}


@pytest.fixture(scope="session")
def samples():
    """{"fair": (pgn, side), "suspicious": (pgn, side)} from app/samples/."""
    found = load_samples()
    assert set(found) == {"fair", "suspicious"}, "run scripts/generate_dataset.py to create app/samples/"
    return found
