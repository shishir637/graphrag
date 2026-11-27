import os

# fast, offline password settings for tests (production: scrypt N=2^17, 15+ characters, breach check on)
os.environ.setdefault("CL_SCRYPT_N", "16384")
os.environ.setdefault("CL_MIN_PASSWORD_LENGTH", "10")
os.environ.setdefault("CL_BREACH_CHECK", "0")
os.environ.setdefault("CL_SCHEDULER", "0")          # no background scheduler thread in tests
# The app's tables: SQLite in a temporary folder by default; the whole suite also runs on Postgres with
# CL_TEST_DATABASE_URL (a throwaway database: every test empties its tables), e.g.
#   docker run -d --name cl-addon-testdb -p 127.0.0.1:5498:5432 -e POSTGRES_HOST_AUTH_METHOD=trust #     -e POSTGRES_USER=rag -e POSTGRES_DB=contextlayer postgres:17
#   CL_TEST_DATABASE_URL=postgresql://rag@127.0.0.1:5498/contextlayer pytest
if os.environ.get("CL_TEST_DATABASE_URL"):
    os.environ["CL_DATABASE_URL"] = os.environ["CL_TEST_DATABASE_URL"]
else:
    os.environ.pop("CL_DATABASE_URL", None)

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


def stored_text() -> str:
    """Everything the app database holds, as text (to check that secrets are never stored in plain text)."""
    from app import core
    from app import db as D
    if not D.postgres():
        return (core.DATA_DIR / "app.db").read_bytes().decode("latin-1")
    out = []
    with D.pg_tx() as c:
        for (t,) in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall():
            out += [repr(tuple(r)) for r in c.execute(f'SELECT * FROM "{t}"').fetchall()]
    return "\n".join(out)
