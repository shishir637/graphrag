"""Print what one person's AI apps did through Context Layer (times and tool names only, no arguments).

    docker compose exec -T app python /app/var/connector_report.py kanakavalli
"""
import sys
from collections import Counter

sys.path.insert(0, "/app")
from app import core  # noqa: E402

who = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
users = core.db().execute("SELECT id, email, name, role, last_login FROM users WHERE lower(email) LIKE ? OR lower(name) LIKE ?",
                          (f"%{who}%", f"%{who}%")).fetchall()
for u in users:
    print(f"account: {u['email']} role={u['role']} last web sign-in={u['last_login']}")
rows = core.db().execute("SELECT time, actor, action, target, ok FROM events WHERE lower(actor) LIKE ? OR lower(target) LIKE ? "
                         "ORDER BY id DESC LIMIT 400", (f"%{who}%", f"%{who}%")).fetchall()
print("\nby action:", dict(Counter(r["action"] for r in rows)))
print("\nlatest 60:")
for r in rows[:60]:
    print(r["time"], "|", r["actor"], "|", r["action"], "|", (r["target"] or "")[:60], "|", "ok" if r["ok"] else "FAILED")
try:
    from app import oauth
    print("\nconnected apps:", [(g["app"], g.get("connected") or g.get("created")) for g in oauth.grants(None) if who in g["user"].lower()])
except Exception as e:                                       # noqa: BLE001
    print("apps: ?", e)
print("\nall tool calls by anyone, last 7 days:", core.db().execute(
    "SELECT count(*) FROM events WHERE action LIKE 'tool.%' AND time > ?",
    ((__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
      - __import__("datetime").timedelta(days=7)).isoformat(timespec="seconds"),)).fetchone()[0])
