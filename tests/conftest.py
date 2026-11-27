import os

# fast, offline password settings for tests (production: scrypt N=2^17, 15+ characters, breach check on)
os.environ.setdefault("CL_SCRYPT_N", "16384")
os.environ.setdefault("CL_MIN_PASSWORD_LENGTH", "10")
os.environ.setdefault("CL_BREACH_CHECK", "0")
os.environ.setdefault("CL_SCHEDULER", "0")          # no background scheduler thread in tests

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


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Sign-in rate limits are per process; tests sign in many times from the same test client."""
    from app import main, oauth
    main._attempts.clear()
    oauth._hits.clear()
    yield
