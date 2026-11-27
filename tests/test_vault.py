"""Secrets key management (0.9, last item): master keys from CL_SECRET_KEY, CL_SECRET_KEY_FILE or a provider hook,
several keys (MultiFernet) and re-encryption with the newest one (python -m app.vault rotate), refusing to start in
production without a key, and *_FILE settings. Ported from dev tests/test_vault.py. Runs on SQLite and Postgres."""
import pytest

from app import core, vault
from app.envfile import env

KEY_A, KEY_B = "ijnmXR1P-xOIeMxR0p6Ma2QL9NJfCxplANYEGyWKB2M=", "EVIME7neR_PqM9FrnfbSetbUgi0JVuPZPQvDdtm6LVk="   # test-only keys


def _provider_key():
    return f"{KEY_B}\n{KEY_A}"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    for v in ("CL_SECRET_KEY", "CL_SECRET_KEY_FILE", "CL_SECRET_KEY_PROVIDER", "CL_ENV"):
        monkeypatch.delenv(v, raising=False)
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    yield
    vault.reset_for_tests()


def test_key_sources_in_order(fresh, tmp_path, monkeypatch):
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    assert vault.configured_keys() == [KEY_A.encode()]
    monkeypatch.delenv("CL_SECRET_KEY")
    f = tmp_path / "keys"
    f.write_text(f"{KEY_B}\n{KEY_A}\n")
    monkeypatch.setenv("CL_SECRET_KEY_FILE", str(f))
    assert vault.configured_keys() == [KEY_B.encode(), KEY_A.encode()]
    monkeypatch.delenv("CL_SECRET_KEY_FILE")
    monkeypatch.setenv("CL_SECRET_KEY_PROVIDER", "module:tests.test_vault:_provider_key")
    assert vault.configured_keys() == [KEY_B.encode(), KEY_A.encode()]
    monkeypatch.setenv("CL_SECRET_KEY_PROVIDER", "nope:x")
    with pytest.raises(ValueError):
        vault.configured_keys()


def test_rotation_with_several_keys(fresh, monkeypatch):
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    vault.put("source:s1:token", "ghp_secret")
    old = vault._db().execute("SELECT ciphertext FROM secrets").fetchone()[0]
    monkeypatch.setenv("CL_SECRET_KEY", f"{KEY_B},{KEY_A}")            # new key first, old one kept
    vault.reset_for_tests()
    assert vault.get("source:s1:token") == "ghp_secret"
    assert vault.rotate() == 1 and vault.rotate() == 0
    assert vault._db().execute("SELECT ciphertext FROM secrets").fetchone()[0] != old
    monkeypatch.setenv("CL_SECRET_KEY", KEY_B)                         # the old key can now go
    vault.reset_for_tests()
    assert vault.get("source:s1:token") == "ghp_secret"
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    vault.reset_for_tests()
    with pytest.raises(Exception):
        vault.get("source:s1:token")                                   # only the new key opens it


def test_rotation_covers_every_workspace(fresh, monkeypatch):
    from app import db as D
    if not D.postgres():
        pytest.skip("one workspace on SQLite")
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    for t in ("acme-1", "globex-2"):
        with core.tenant_scope(t):
            vault.put("x", f"secret-of-{t}")
    monkeypatch.setenv("CL_SECRET_KEY", f"{KEY_B},{KEY_A}")
    vault.reset_for_tests()
    assert vault.rotate() == 2
    monkeypatch.setenv("CL_SECRET_KEY", KEY_B)
    vault.reset_for_tests()
    with core.tenant_scope("globex-2"):
        assert vault.get("x") == "secret-of-globex-2"


def test_production_needs_a_key(fresh, monkeypatch):
    monkeypatch.setenv("CL_ENV", "production")
    with pytest.raises(SystemExit):
        vault.check_startup()
    with pytest.raises(SystemExit):
        vault.put("x", "y")                                            # no generated key file in production
    assert not (core.DATA_DIR / "secret.key").exists()
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    vault.check_startup()
    monkeypatch.delenv("CL_ENV")
    monkeypatch.delenv("CL_SECRET_KEY")
    vault.reset_for_tests()
    vault.put("x", "y")                                                # development: a key file is generated
    assert (core.DATA_DIR / "secret.key").exists()


def test_the_app_refuses_to_start_in_production_without_a_key(fresh, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CL_ENV", "production")
    from app.main import asgi
    with pytest.raises(BaseException):           # the start-up raises SystemExit (the test client reports it cancelled)
        with TestClient(asgi):
            pass
    from app import vault as V
    with pytest.raises(SystemExit):
        V.check_startup()


def test_file_settings(tmp_path, monkeypatch):
    from app import db
    pw = tmp_path / "pw"
    pw.write_text("s3cr3t/with@chars\n")
    url = tmp_path / "url"
    url.write_text("postgresql://rag@dbhost:5432/contextlayer\n")
    monkeypatch.delenv("CL_DATABASE_URL", raising=False)
    monkeypatch.setenv("CL_DATABASE_URL_FILE", str(url))
    monkeypatch.setenv("CL_DB_PASSWORD_FILE", str(pw))
    monkeypatch.delenv("CL_DB_PASSWORD", raising=False)
    assert db.url() == "postgresql://rag:s3cr3t%2Fwith%40chars@dbhost:5432/contextlayer"
    q = tmp_path / "q"
    q.write_text("qdrant-key")
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    monkeypatch.setenv("QDRANT_API_KEY_FILE", str(q))
    assert env("QDRANT_API_KEY") == "qdrant-key"
    monkeypatch.setenv("QDRANT_API_KEY", "from-env")
    assert env("QDRANT_API_KEY") == "from-env"                          # the variable itself wins
    d = tmp_path / "defs"
    d.write_text("sqlite:///x.db\n")
    monkeypatch.delenv("CL_DEFS_DSN", raising=False)
    monkeypatch.setenv("CL_DEFS_DSN_FILE", str(d))
    from semantic import defstore
    assert defstore.dsn() == "sqlite:///x.db"
    monkeypatch.setenv("CL_DB_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        db.url()
