"""Create two test sign-ins: a team admin and a regular user in one team, and write their passwords to a file.

Run inside the app container:
    docker compose exec -T app python /app/var/add_test_accounts.py "Data Engineering" /app/var/test-accounts.txt
Passwords are never printed: they are only written to the file you name. Re-running gives them new passwords.
"""
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app import core, directory, teams  # noqa: E402

ACCOUNTS = [("test.teamadmin@kestrel-labs.example", "Test Team Admin", "Team admin (manages {team})"),
            ("test.user@kestrel-labs.example", "Test User", "Regular user in {team}")]


def password() -> str:
    a = string.ascii_letters + string.digits
    return "-".join("".join(secrets.choice(a) for _ in range(5)) for _ in range(4))


def main(team: str, out: str):
    names = directory.team_names()
    if team not in names:
        team = names[0] if names else None
    if team is None:
        directory.save_team("Test Team", None, None, "Created for testing")
        team = "Test Team"
    lines = [f"Context Layer test sign-ins (team: {team})", "Open http://localhost:8080 and sign in.", ""]
    for email, name, what in ACCOUNTS:
        pw = password()
        row = core.db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            core.update_user(row["id"], role="viewer", disabled=False)
            uid = row["id"]
        else:
            uid = core.create_user(email, name, pw, "viewer")["id"]
        core.set_password(uid, pw, must_change=False)
        old = directory.db().execute("SELECT * FROM people WHERE email = ?", (email,)).fetchone()
        directory.save_person({**(dict(old) if old else {}), "email": email, "name": name, "title": "Test account",
                               "team": team, "status": "active"}, "test script")
        lines += [what.format(team=team), f"  Email:    {email}", f"  Password: {pw}", ""]
    teams.db().execute("INSERT INTO team_admins (team, email, added_by, added) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT DO NOTHING",
                       (team, ACCOUNTS[0][0], "test script", core.now()))
    lines.append("Delete these accounts on the Users tab when you are done testing.")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("created", len(ACCOUNTS), "test accounts in", team)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Data Engineering", sys.argv[2] if len(sys.argv) > 2 else "/app/var/test-accounts.txt")
