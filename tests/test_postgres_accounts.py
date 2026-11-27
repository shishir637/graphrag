"""Accounts and access in Postgres (0.9): an existing var/app.db (SQLite) is copied into Postgres once and
idempotently: people, sessions, keys, activity, settings, the access tables, teams and the directory; ids continue
after the copied ones. Needs a throwaway Postgres: CL_TEST_DATABASE_URL (see conftest.py)."""
import os

import pytest

from app import access, core, directory, teams
from app import db as D

pytestmark = pytest.mark.skipif(not os.environ.get("CL_TEST_DATABASE_URL"), reason="no test Postgres (CL_TEST_DATABASE_URL)")


def test_sqlite_install_is_copied_once(tmp_path, monkeypatch):
    var = tmp_path / "var"
    # 1. an 0.8 install on SQLite
    monkeypatch.delenv("CL_DATABASE_URL")
    core.reset_for_tests(var)
    owner = core.create_user("o@example.com", "Owner", "pass-word-123", "owner")
    _, key = core.create_agent("Reports bot", "", ["public"], ["documents"], "o@example.com")
    core.log_event("user", "o@example.com", "test.event", "x")
    core.set_setting("organisation", "Acme")
    directory.save_team("Engineering")
    directory.save_person({"email": "ann@example.com", "name": "Ann", "team": "Engineering"})
    teams.set_admin({"kind": "user", "email": "o@example.com", "role": "owner"}, "Engineering", "ann@example.com")
    access.add_dataset("finance", "Money", "o@example.com")
    access.save_role("Finance reader", "", ["finance"], ["documents"], "o@example.com")
    assert (var / "app.db").exists()
    # 2. the same install started with Postgres
    monkeypatch.setenv("CL_DATABASE_URL", os.environ["CL_TEST_DATABASE_URL"])
    core.reset_for_tests(var)                 # empties Postgres; the first use copies app.db
    assert D.postgres()
    assert [u["email"] for u in core.list_users()] == ["o@example.com"]
    assert core.list_users()[0]["id"] == owner["id"]
    assert core.agent_for_token(key)["name"] == "Reports bot"
    assert core.get_setting("organisation") == "Acme"
    assert any(e["action"] == "test.event" for e in core.list_events())
    assert "Engineering" in directory.team_names() and teams.admins_of("Engineering") == ["ann@example.com"]
    assert any(r["name"] == "Finance reader" for r in access.roles())
    assert any(d["name"] == "finance" for d in access.datasets())
    # ids continue after the copied ones
    assert core.create_user("b@example.com", "B", "pass-word-123", "viewer")["id"] > owner["id"]
    # idempotent: copying again adds nothing
    from app import migrate_local
    assert migrate_local.migrate()["skipped"] == "already copied"
    out = migrate_local.migrate(force=True)
    assert sum(out["copied"].values()) == 0
    assert len(core.list_users()) == 2


def test_everything_lives_in_postgres(tmp_path):
    core.reset_for_tests(tmp_path / "var")
    core.create_user("o@example.com", "Owner", "pass-word-123", "owner")
    access.roles()
    teams.db()
    assert not (tmp_path / "var" / "app.db").exists()
    with D.pg_tx() as c:
        names = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert {"users", "sessions", "agents", "events", "kv", "ac_roles", "ac_datasets", "ac_team", "ac_user",
            "team_admins", "teams", "people", "invites"} <= names
