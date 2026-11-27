"""Identity and secrets (0.7): master keys from CL_SECRET_KEY, CL_SECRET_KEY_FILE or a provider hook, several keys
(MultiFernet) and re-encryption with the newest one, refusing to start in production without a key, *_FILE
settings, and the optional archive of old audit events (one Parquet file per month)."""
import duckdb
import pytest
from cryptography.fernet import Fernet

from app import core, jobs, vault
from app.envfile import env
from tests.conftest import needs_pg

pytestmark = needs_pg
KEY_A, KEY_B = "ijnmXR1P-xOIeMxR0p6Ma2QL9NJfCxplANYEGyWKB2M=", "EVIME7neR_PqM9FrnfbSetbUgi0JVuPZPQvDdtm6LVk="   # test-only keys (fixed: the provider hook imports this module again)


def _provider_key():
    return f"{KEY_B}\n{KEY_A}"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    jobs.stop_workers()
    for v in ("CL_SECRET_KEY", "CL_SECRET_KEY_FILE", "CL_SECRET_KEY_PROVIDER", "CL_ENV"):
        monkeypatch.delenv(v, raising=False)
    core.reset_for_tests(tmp_path / "var")
    vault.reset_for_tests()
    yield core.create_tenant("Acme")
    vault.reset_for_tests()


def test_key_sources_in_order(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("CL_SECRET_KEY", f"{KEY_A}")
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


def test_rotation_with_several_keys(ws, monkeypatch):
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    with core.tenant_scope(ws):
        vault.put("connector:c1:token", "ghp_secret")
        old = core.db().execute("SELECT ciphertext FROM secrets").fetchone()[0]
        monkeypatch.setenv("CL_SECRET_KEY", f"{KEY_B},{KEY_A}")         # new key first, old one kept
        vault.reset_for_tests()
        assert vault.get("connector:c1:token") == "ghp_secret"
        assert vault.rotate() == 1
        assert core.db().execute("SELECT ciphertext FROM secrets").fetchone()[0] != old
        monkeypatch.setenv("CL_SECRET_KEY", KEY_B)                      # the old key can now go
        vault.reset_for_tests()
        assert vault.get("connector:c1:token") == "ghp_secret"
        monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
        vault.reset_for_tests()
        with pytest.raises(Exception):
            vault.get("connector:c1:token")                             # only the new key opens it


def test_production_needs_a_key(ws, monkeypatch):
    monkeypatch.setenv("CL_ENV", "production")
    with pytest.raises(SystemExit):
        vault.check_startup()
    with core.tenant_scope(ws), pytest.raises(SystemExit):
        vault.put("x", "y")                                             # no generated key file in production
    assert not (core.DATA_DIR / "secret.key").exists()
    monkeypatch.setenv("CL_SECRET_KEY", KEY_A)
    vault.check_startup()
    monkeypatch.delenv("CL_ENV")
    monkeypatch.delenv("CL_SECRET_KEY")
    vault.reset_for_tests()
    with core.tenant_scope(ws):
        vault.put("x", "y")                                             # development: a key file is generated
    assert (core.DATA_DIR / "secret.key").exists()


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
    assert env("QDRANT_API_KEY") == "from-env"                           # the variable itself wins
    monkeypatch.setenv("CL_DB_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        db.url()


def test_events_archive_is_off_by_default_and_idempotent(ws):
    from rag.wspath import files_root
    with core.tenant_scope(ws):
        for i, ts in enumerate(("2026-01-05T10:00:00+00:00", "2026-01-20T10:00:00+00:00", "2026-02-02T10:00:00+00:00")):
            core.db().execute("INSERT INTO events (tenant_id, time, actor_type, actor, action) VALUES (?, ?, 'user', 'ann', ?)",
                              (ws, ts, f"a{i}"))
        assert core.purge_events(90) == 3
        assert not (files_root() / ws / "archive").exists()             # off by default: deleted, not archived
        core.set_setting("events_archive", "1")
        core.db().execute("INSERT INTO events (tenant_id, time, actor_type, actor, action) VALUES (?, ?, 'user', 'ann', 'b')",
                          (ws, "2026-01-07T10:00:00+00:00"))
        assert core.purge_events(90) == 1
        from app.landing import archive_events
        rows = [{"id": 999, "tenant_id": ws, "time": "2026-01-08T00:00:00+00:00", "actor_type": "user", "actor": "x",
                 "action": "c", "target": "", "detail": "{}", "ok": 1, "seconds": None}]
        archive_events(rows)
        archive_events(rows)                                            # twice: no duplicate
        p = str(files_root() / ws / "archive" / "events-2026-01.parquet").replace("\\", "/")
        assert duckdb.connect().execute(f"SELECT count(*), count(DISTINCT id) FROM read_parquet('{p}')").fetchone() == (2, 2)
