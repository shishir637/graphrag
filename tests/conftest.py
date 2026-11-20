import pytest

from semantic.db import connect
from semantic.yamlio import load_metrics, load_model


@pytest.fixture(scope="session")
def con():
    return connect()


@pytest.fixture(scope="session")
def model():
    return load_model()


@pytest.fixture(scope="session")
def metrics():
    return load_metrics()


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", help="rewrite tests/golden/*.sql")
