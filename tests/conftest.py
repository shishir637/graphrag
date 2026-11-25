import os

import pytest

# The app stores workspaces, people, keys and the semantic layer in Postgres. Tests use a throwaway database:
# CL_TEST_DATABASE_URL (default: the local test container, see GETTING-STARTED.md). Tables are emptied per test.
os.environ["CL_DATABASE_URL"] = os.environ.get("CL_TEST_DATABASE_URL",
                                               "postgresql://rag:testpw@127.0.0.1:5499/contextlayer")


def pg_available() -> bool:
    try:
        import psycopg
        psycopg.connect(os.environ["CL_DATABASE_URL"], connect_timeout=3).close()
        return True
    except Exception:                                      # noqa: BLE001
        return False


needs_pg = pytest.mark.skipif(not pg_available(), reason="no test Postgres (set CL_TEST_DATABASE_URL)")

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
