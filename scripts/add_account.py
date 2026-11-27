"""Add a sign-in account with a temporary password (changed at first sign-in) and write the password to a file.

Run inside the app container, e.g.:
    docker compose exec app python scripts/add_account.py kanakavalli@kestrel-labs.example Kanakavalli admin /app/var/kanakavalli.txt
The password is never printed: it is only written to the file you name. Copy that file somewhere safe and delete it.
"""
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import core  # noqa: E402


def main(email: str, name: str, role: str, out: str):
    email = email.strip().lower()
    alphabet = string.ascii_letters + string.digits
    pw = "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))
    row = core.db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        core.update_user(row["id"], role=role, disabled=False)
        uid = row["id"]
        what = "existing account: role set, new temporary password"
    else:
        uid = core.create_user(email, name, pw, role)["id"]
        what = "new account"
    core.set_password(uid, pw, must_change=True)
    Path(out).write_text(f"Context Layer sign-in for {name}\n\nEmail: {email}\nTemporary password: {pw}\nRole: {role}\n\n"
                         "The password must be changed at the first sign-in.\n", encoding="utf-8")
    print(f"{email}: {what}; password written to {out}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
