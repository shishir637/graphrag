"""Sign-in hardening: password rules, breached-password check, rehash on login, idle sessions, sign out everywhere."""
import hashlib

import pytest

from app import core


@pytest.fixture
def db(tmp_path):
    core.reset_for_tests(tmp_path)
    yield core


def test_password_rules(monkeypatch):
    monkeypatch.setattr(core, "MIN_PASSWORD", 15)
    with pytest.raises(ValueError, match="15"):
        core.validate_password("short-one-123")
    core.validate_password("a long sentence I remember")


def test_breached_password_check(monkeypatch):
    pw = "correct horse battery staple"
    h = hashlib.sha1(pw.encode()).hexdigest().upper()

    class R:
        def __init__(self, body): self.body = body
        def read(self): return self.body.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("CL_BREACH_CHECK", "1")
    import urllib.request
    seen = {}

    def fake(req, timeout=0):
        seen["url"] = req.full_url
        return R(f"0000000000000000000000000000000000A:3\n{h[5:]}:42\n")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert core.breached(pw) and seen["url"].endswith(h[:5])         # only the first 5 hash characters leave
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert not core.breached(pw)                                      # fails open


def test_rehash_idle_and_logout_everywhere(db, monkeypatch):
    monkeypatch.setattr(core, "SCRYPT_N", 2 ** 14)
    u = core.create_user("a@example.com", "A", "pass-word-123", "viewer")
    monkeypatch.setattr(core, "SCRYPT_N", 2 ** 15)
    assert core.needs_rehash(core.db().execute("SELECT pw_hash FROM users").fetchone()[0])
    _, tok = core.login("a@example.com", "pass-word-123")
    assert core.db().execute("SELECT pw_hash FROM users").fetchone()[0].startswith("scrypt$32768$")
    assert core.session_user(tok)["email"] == "a@example.com"
    core.db().execute("UPDATE sessions SET last_seen = '2000-01-01T00:00:00+00:00'")
    assert core.session_user(tok) is None                              # idle too long
    _, t2 = core.login("a@example.com", "pass-word-123")
    _, t3 = core.login("a@example.com", "pass-word-123")
    assert core.logout_everywhere(u["id"])["sessions"] == 3
    assert core.session_user(t2) is None and core.session_user(t3) is None
