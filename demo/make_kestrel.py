"""Generate the Kestrel Labs demo company: a made-up ~130-person software company.

Everything agrees with everything else: the incidents in the incident reviews are rows of incidents.csv, their
owners are people in the directory, services belong to the teams that deploy them, team leads in the charters
are the leads in the directory. Deterministic (fixed seed), dates up to the day before today.

    python demo/make_kestrel.py            -> demo/kestrel/{data,documents,semantic_layer}/ + directory.csv
"""
import csv
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "kestrel"
R = random.Random(20261126)
TODAY = date(2026, 11, 26)
START = date(2025, 1, 1)
END = TODAY - timedelta(days=1)
DOMAIN = "kestrel-labs.example"

TEAMS = [  # id, name, department, parent, lead (name), mission
    ("T01", "Leadership", "Leadership", None, "Anika Rao", "Sets strategy, approves budgets and owns the company's goals."),
    ("T02", "Data Engineering", "Engineering", "Engineering", "Meera Krishnan", "Builds and runs the data pipelines and the warehouse that power product analytics and finance reporting."),
    ("T03", "Platform and DevOps", "Engineering", "Engineering", "Ravi Shankar", "Runs the cloud platform, CI/CD, the API gateway and authentication; leads incident response."),
    ("T04", "Backend", "Engineering", "Engineering", "Daniel Okafor", "Owns the core API, billing, notifications and search services."),
    ("T05", "Frontend", "Engineering", "Engineering", "Sara Lindqvist", "Owns the web app and the admin console."),
    ("T06", "Mobile", "Engineering", "Engineering", "Arjun Mehta", "Owns the iOS and Android apps."),
    ("T07", "QA", "Engineering", "Engineering", "Fatima Sheikh", "Owns test automation, release checks and the test environments."),
    ("T08", "Product", "Product", None, "Neha Kapoor", "Decides what to build and why; owns the roadmap and pricing proposals."),
    ("T09", "Sales", "Commercial", "Commercial", "Tom Becker", "Wins new customers and expansions."),
    ("T10", "Customer Success", "Commercial", "Commercial", "Leela Iyer", "Onboards customers, runs support and owns renewals."),
    ("T11", "Finance", "G&A", None, "Rohan Das", "Owns budgeting, billing operations, cloud cost reporting and the financial close."),
    ("T12", "People", "G&A", None, "Kavya Reddy", "Owns hiring, onboarding and people policies."),
]
SIZE = {"T01": 3, "T02": 12, "T03": 11, "T04": 18, "T05": 12, "T06": 9, "T07": 8, "T08": 8, "T09": 16, "T10": 14,
        "T11": 7, "T12": 5}
TITLES = {
    "T01": ["Chief Executive Officer", "Chief Technology Officer", "Chief Operating Officer"],
    "T02": ["Head of Data Engineering", "Senior Data Engineer", "Data Engineer", "Analytics Engineer"],
    "T03": ["Head of Platform and DevOps", "Senior Site Reliability Engineer", "Site Reliability Engineer", "DevOps Engineer"],
    "T04": ["Backend Engineering Lead", "Senior Backend Engineer", "Backend Engineer"],
    "T05": ["Frontend Engineering Lead", "Senior Frontend Engineer", "Frontend Engineer", "Product Designer"],
    "T06": ["Mobile Engineering Lead", "Senior Mobile Engineer", "Mobile Engineer"],
    "T07": ["QA Lead", "Senior QA Engineer", "QA Engineer"],
    "T08": ["Chief Product Officer", "Senior Product Manager", "Product Manager", "Product Analyst"],
    "T09": ["Head of Sales", "Account Executive", "Sales Development Representative", "Sales Engineer"],
    "T10": ["Head of Customer Success", "Customer Success Manager", "Support Engineer", "Onboarding Specialist"],
    "T11": ["Chief Financial Officer", "Financial Analyst", "Billing Specialist", "Accountant"],
    "T12": ["Head of People", "Recruiter", "People Partner"],
}
SERVICES = {  # service -> team, deploys per working day (average)
    "warehouse-sync": ("T02", 0.5), "ingest-pipeline": ("T02", 0.7),
    "api-gateway": ("T03", 0.6), "auth-service": ("T03", 0.4), "k8s-platform": ("T03", 0.5),
    "kestrel-api": ("T04", 2.2), "billing-service": ("T04", 0.8), "notification-service": ("T04", 0.6),
    "search-service": ("T04", 0.5), "web-app": ("T05", 1.8), "admin-console": ("T05", 0.6),
    "ios-app": ("T06", 0.25), "android-app": ("T06", 0.3),
}
FIRST = ["Aarav", "Aditi", "Akash", "Ananya", "Bhavna", "Chen", "Deepak", "Divya", "Elena", "Farhan", "Gauri", "Hannah",
         "Ishaan", "Jaya", "Karan", "Kiran", "Lakshmi", "Liam", "Maya", "Mohan", "Nadia", "Nikhil", "Omar", "Pooja",
         "Priya", "Rahul", "Riya", "Sameer", "Sanjana", "Shreya", "Siddharth", "Sneha", "Tanvi", "Uday", "Varun",
         "Vidya", "Yusuf", "Zoe", "Lucas", "Amara", "Kenji", "Isabel", "Noah", "Leah", "Tariq", "Mei", "Oliver", "Ines"]
LAST = ["Agarwal", "Bose", "Chatterjee", "Desai", "Fernandes", "Ghosh", "Gupta", "Hegde", "Iyer", "Jain", "Joshi",
        "Kulkarni", "Malhotra", "Nair", "Pillai", "Patel", "Raman", "Saxena", "Sen", "Thomas", "Varma", "Wong",
        "Garcia", "Muller", "Novak", "Silva", "Tanaka", "Andersen", "Haddad", "Kowalski", "Okoye", "Rossi"]
LOCATIONS = ["Bengaluru", "Bengaluru", "Bengaluru", "Pune", "Pune", "Hyderabad", "Remote"]

# the incidents the incident reviews describe (they are also rows of incidents.csv)
REVIEWED = [
    {"incident_id": "INC-2026-031", "title": "Customers charged twice for annual renewals", "severity": "Sev1",
     "service": "billing-service", "opened_at": "2026-03-14 09:20", "hours": 5.5, "deploy_cause": True},
    {"incident_id": "INC-2026-077", "title": "API gateway outage after certificate rotation", "severity": "Sev1",
     "service": "api-gateway", "opened_at": "2026-08-02 02:45", "hours": 2.25, "deploy_cause": False},
    {"incident_id": "INC-2026-104", "title": "Dashboards stale for 14 hours after warehouse sync stalled",
     "severity": "Sev2", "service": "warehouse-sync", "opened_at": "2026-10-21 06:10", "hours": 14.0, "deploy_cause": True},
]


def days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def email_of(name: str) -> str:
    return name.lower().replace(" ", ".") + "@" + DOMAIN


# ------------------------------------------------------------------ people

def make_people():
    used, people = set(), []
    lead_of = {t[0]: t[4] for t in TEAMS}
    names = []
    for f in FIRST:
        for l in LAST:
            names.append(f"{f} {l}")
    R.shuffle(names)
    pool = iter(names)
    for tid, tname, dept, parent, lead, _ in TEAMS:
        manager = lead_of["T01"] if tid != "T01" else None
        cto = "Vikram Menon"
        lead_manager = cto if dept == "Engineering" else (lead_of["T01"] if tid != "T01" else None)
        for i in range(SIZE[tid]):
            if i == 0:
                name, title, mgr = lead, TITLES[tid][0], lead_manager
            elif tid == "T01" and i == 1:
                name, title, mgr = cto, TITLES[tid][1], lead
            elif tid == "T01":
                name, title, mgr = "Isabel Moreau", TITLES[tid][2], lead
            else:
                name = next(n for n in pool if n not in used)
                title = TITLES[tid][1 + (i % (len(TITLES[tid]) - 1))]
                mgr = lead
            used.add(name)
            hire = START - timedelta(days=R.randint(60, 1500)) if i < 3 else \
                date(2023, 1, 1) + timedelta(days=R.randint(0, (END - date(2023, 1, 1)).days - 30))
            people.append({"employee_id": f"E{len(people) + 1:04d}", "name": name, "email": email_of(name),
                           "title": title, "team_id": tid, "team": tname, "manager_email": email_of(mgr) if mgr else "",
                           "location": "Bengaluru" if i == 0 else R.choice(LOCATIONS), "hire_date": hire.isoformat(),
                           "status": "active", "left_date": ""})
    # the two Context Layer testers are part of the company too
    people.append({"employee_id": f"E{len(people) + 1:04d}", "name": "Kanakavalli", "email": "kanakavalli@" + DOMAIN,
                   "title": "Senior Data Engineer", "team_id": "T02", "team": "Data Engineering",
                   "manager_email": email_of("Meera Krishnan"), "location": "Bengaluru", "hire_date": "2024-06-03",
                   "status": "active", "left_date": ""})
    # a few people left during 2025-2026 (attrition)
    for p in R.sample([p for p in people if p["title"] not in TITLES[p["team_id"]][:1] and "kanakavalli" not in p["email"]], 7):
        p["status"] = "left"
        p["left_date"] = (date(2025, 3, 1) + timedelta(days=R.randint(0, 600))).isoformat()
    return people


# ------------------------------------------------------------------ engineering data

def engineers(people, tid):
    return [p for p in people if p["team_id"] == tid and p["status"] == "active"]


def make_deployments(people):
    rows, n = [], 0
    for d in days(START, END):
        if d.weekday() >= 5:
            continue
        for svc, (tid, rate) in SERVICES.items():
            k = sum(1 for _ in range(10) if R.random() < rate / 10)
            for _ in range(k):
                n += 1
                r = R.random()
                status = "success" if r < 0.89 else "failed" if r < 0.95 else "rolled_back"
                if svc == "billing-service" and d == date(2026, 3, 13):
                    status = "success"
                lead = round(math.exp(R.gauss(3.0 if tid in ("T04", "T05") else 3.4, 0.6)), 1)
                author = R.choice(engineers(people, tid))
                rows.append({"deploy_id": f"D{n:06d}", "service": svc, "team_id": tid,
                             "deployed_at": f"{d.isoformat()} {R.randint(9, 18):02d}:{R.randint(0, 59):02d}",
                             "environment": "production", "status": status, "lead_time_hours": lead,
                             "author_email": author["email"]})
    return rows


def make_incidents(people, deployments):
    rows = []
    by_svc = {}
    for dep in deployments:
        by_svc.setdefault(dep["service"], []).append(dep)
    n = 0
    month = START.replace(day=1)
    while month <= END:
        for _ in range(R.randint(4, 8)):
            day = month + timedelta(days=R.randint(0, 27))
            if day > END:
                continue
            svc = R.choice(list(SERVICES))
            tid = SERVICES[svc][0]
            sev = R.choices(["Sev1", "Sev2", "Sev3", "Sev4"], [3, 15, 45, 37])[0]
            hours = round({"Sev1": R.uniform(1.5, 6), "Sev2": R.uniform(2, 12), "Sev3": R.uniform(4, 36),
                           "Sev4": R.uniform(12, 96)}[sev], 2)
            n += 1
            opened = datetime(day.year, day.month, day.day, R.randint(0, 23), R.randint(0, 59))
            cause = ""
            recent = [d for d in by_svc.get(svc, []) if d["status"] != "success" and d["deployed_at"][:10] <= day.isoformat()]
            if recent and R.random() < 0.45:
                cause = recent[-1]["deploy_id"]
            rows.append({"incident_id": f"INC-{day.year}-{n:03d}", "title": R.choice([
                "Elevated error rate", "Slow responses", "Background jobs delayed", "Login failures for some users",
                "Webhook deliveries failing", "Search results missing", "Push notifications delayed"]),
                "severity": sev, "service": svc, "team_id": tid, "opened_at": opened.strftime("%Y-%m-%d %H:%M"),
                "resolved_at": (opened + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M"), "time_to_restore_hours": hours,
                "owner_email": R.choice(engineers(people, tid) or engineers(people, "T03"))["email"],
                "caused_by_deploy": cause})
        month = (month + timedelta(days=32)).replace(day=1)
    # renumber per year and put the reviewed incidents in their place
    rows.sort(key=lambda r: r["opened_at"])
    counters = {}
    for r in rows:
        y = r["opened_at"][:4]
        counters[y] = counters.get(y, 0) + 1
        r["incident_id"] = f"INC-{y}-{counters[y]:03d}"
    owners = {"billing-service": "Daniel Okafor", "api-gateway": "Ravi Shankar", "warehouse-sync": "Meera Krishnan"}
    for rv in REVIEWED:
        tid = SERVICES[rv["service"]][0]
        opened = datetime.strptime(rv["opened_at"], "%Y-%m-%d %H:%M")
        cause = ""
        if rv["deploy_cause"]:
            cands = [d for d in deployments if d["service"] == rv["service"] and d["deployed_at"] < rv["opened_at"]]
            cause = cands[-1]["deploy_id"]
        row = {"incident_id": rv["incident_id"], "title": rv["title"], "severity": rv["severity"], "service": rv["service"],
               "team_id": tid, "opened_at": rv["opened_at"],
               "resolved_at": (opened + timedelta(hours=rv["hours"])).strftime("%Y-%m-%d %H:%M"),
               "time_to_restore_hours": rv["hours"], "owner_email": email_of(owners[rv["service"]]), "caused_by_deploy": cause}
        i = next((k for k, r in enumerate(rows) if r["incident_id"] == rv["incident_id"]), None)
        if i is None:
            rows.append(row)
        else:
            rows[i] = row
    rows.sort(key=lambda r: r["opened_at"])
    return rows


def make_tickets(people):
    rows, n = [], 0
    sprint_start = date(2025, 1, 6)
    s = 0
    eng = ["T02", "T03", "T04", "T05", "T06", "T07"]
    while sprint_start + timedelta(days=13) <= END:
        s += 1
        name = f"Sprint {s}"
        for tid in eng:
            for _ in range(R.randint(14, 30) if tid != "T07" else R.randint(8, 14)):
                n += 1
                typ = R.choices(["story", "bug", "task"], [55, 25, 20])[0]
                pts = R.choice([1, 2, 3, 5, 8]) if typ != "bug" else R.choice([1, 2, 3])
                created = sprint_start + timedelta(days=R.randint(-10, 3))
                done = R.random() < 0.86
                cycle = round(max(0.3, R.gauss(3.5 + pts * 0.6, 1.8)), 1)
                done_at = created + timedelta(days=cycle) if done else None
                rows.append({"ticket_id": f"KES-{n:05d}", "team_id": tid, "sprint": name, "sprint_start": sprint_start.isoformat(),
                             "type": typ, "story_points": pts, "status": "done" if done else "carried over",
                             "created_at": created.isoformat(), "done_at": done_at.isoformat() if done_at else "",
                             "cycle_time_days": cycle if done else "",
                             "assignee_email": R.choice(engineers(people, tid))["email"]})
        sprint_start += timedelta(days=14)
    return rows


def make_pipeline_runs():
    pipes = {"events_ingest": 0.97, "billing_export": 0.98, "crm_sync": 0.95, "warehouse_build": 0.96,
             "finance_daily": 0.98}
    rows, n = [], 0
    for d in days(START, END):
        for p, ok in pipes.items():
            n += 1
            success = R.random() < ok
            fresh = round(R.uniform(0.5, 2.5) if success else R.uniform(6, 20), 2)
            if p == "warehouse_build" and d == date(2026, 10, 21):
                success, fresh = False, 14.0
            rows.append({"run_id": f"R{n:06d}", "pipeline": p, "team_id": "T02", "run_date": d.isoformat(),
                         "status": "success" if success else "failed", "duration_minutes": round(R.uniform(4, 55), 1),
                         "freshness_hours": fresh, "rows_processed": R.randint(80_000, 2_400_000) if success else 0})
    return rows


def make_cloud_costs():
    base = {"T02": 9500, "T03": 21000, "T04": 14000, "T05": 3500, "T06": 2200, "T07": 2600}
    split = [("aws", "compute", 0.45), ("aws", "database", 0.22), ("aws", "storage", 0.13), ("aws", "network", 0.08),
             ("datadog", "monitoring", 0.12)]
    rows = []
    m = START
    i = 0
    while m <= END.replace(day=1):
        growth = 1 + 0.018 * i
        for tid, b in base.items():
            for prov, svc, share in split:
                cost = b * growth * share * R.uniform(0.9, 1.1)
                if tid == "T02" and svc == "database":
                    prov, svc = "snowflake", "warehouse"
                rows.append({"month": m.isoformat(), "team_id": tid, "provider": prov, "service": svc,
                             "cost_usd": round(cost, 2)})
        m = (m + timedelta(days=32)).replace(day=1)
        i += 1
    return rows


# ------------------------------------------------------------------ customers and revenue

PLANS = {"Starter": 490, "Growth": 2400, "Enterprise": 9800}


def make_customers(people):
    industries = ["Retail", "Logistics", "Fintech", "Healthcare", "Manufacturing", "Media", "Education", "SaaS"]
    regions = ["India", "North America", "Europe", "APAC"]
    pre = ["Blue", "North", "Silver", "Bright", "Clear", "Swift", "Iron", "Green", "Summit", "Harbor", "Cedar", "Nova",
           "Atlas", "Pine", "River", "Stone", "Orbit", "Echo", "Lumen", "Vertex"]
    post = ["Logistics", "Retail", "Health", "Foods", "Systems", "Analytics", "Works", "Labs", "Pay", "Media",
            "Motors", "Learning", "Energy", "Freight", "Clinics", "Studio"]
    names = sorted({f"{a} {b}" for a in pre for b in post})
    R.shuffle(names)
    csms = engineers(people, "T10")
    customers, subs, n_sub = [], [], 0
    for i in range(320):
        signup = date(2023, 1, 1) + timedelta(days=R.randint(0, (END - date(2023, 1, 1)).days - 20))
        plan = R.choices(list(PLANS), [55, 33, 12])[0]
        churned = R.random() < 0.13 and signup < date(2026, 6, 1)
        churn_date = signup + timedelta(days=R.randint(120, max(121, (END - signup).days))) if churned else None
        if churn_date and churn_date > END:
            churn_date = END
        cid = f"C{i + 1:04d}"
        customers.append({"customer_id": cid, "name": names[i], "industry": R.choice(industries),
                          "region": R.choices(regions, [40, 30, 20, 10])[0], "plan": plan, "signup_date": signup.isoformat(),
                          "status": "churned" if churned else "active", "churned_at": churn_date.isoformat() if churned else "",
                          "csm_email": R.choice(csms)["email"]})
        n_sub += 1
        start, cur = signup, plan
        if plan != "Enterprise" and R.random() < 0.22 and not churned:
            up = start + timedelta(days=R.randint(90, 400))
            if up < END:
                subs.append({"subscription_id": f"S{n_sub:05d}", "customer_id": cid, "plan": cur,
                             "mrr_usd": round(PLANS[cur] * R.uniform(0.85, 1.15), 2), "start_date": start.isoformat(),
                             "end_date": up.isoformat(), "status": "upgraded"})
                n_sub += 1
                start, cur = up, "Growth" if cur == "Starter" else "Enterprise"
        subs.append({"subscription_id": f"S{n_sub:05d}", "customer_id": cid, "plan": cur,
                     "mrr_usd": round(PLANS[cur] * R.uniform(0.85, 1.15), 2), "start_date": start.isoformat(),
                     "end_date": churn_date.isoformat() if churned else "", "status": "cancelled" if churned else "active"})
    return customers, subs


def make_support(customers, people):
    rows, n = [], 0
    agents = engineers(people, "T10")
    for c in customers:
        start = date.fromisoformat(c["signup_date"])
        end = date.fromisoformat(c["churned_at"]) if c["churned_at"] else END
        per_month = {"Starter": 0.6, "Growth": 1.2, "Enterprise": 2.5}[c["plan"]]
        months = max(1, (end - max(start, START)).days // 30)
        for _ in range(int(months * per_month * R.uniform(0.6, 1.4))):
            day = max(start, START) + timedelta(days=R.randint(0, max(1, (end - max(start, START)).days)))
            if day > END:
                continue
            n += 1
            pri = R.choices(["urgent", "high", "normal", "low"], [5, 20, 55, 20])[0]
            rows.append({"ticket_id": f"SUP-{n:06d}", "customer_id": c["customer_id"], "opened_at": day.isoformat(),
                         "priority": pri, "channel": R.choice(["email", "chat", "portal", "phone"]),
                         "first_response_minutes": round(R.uniform(3, 45) if pri in ("urgent", "high") else R.uniform(20, 360), 1),
                         "resolution_hours": round(R.uniform(0.5, 8) if pri == "urgent" else R.uniform(2, 72), 1),
                         "csat": R.choices([1, 2, 3, 4, 5], [3, 5, 12, 35, 45])[0], "agent_email": R.choice(agents)["email"]})
    return rows


# ------------------------------------------------------------------ documents

def documents(people, incidents, deployments):
    lead = {t[0]: t[4] for t in TEAMS}
    tname = {t[0]: t[1] for t in TEAMS}
    by_id = {i["incident_id"]: i for i in incidents}
    name_of = {p["email"]: p["name"] for p in people}
    svc_rows = "\n".join(f"| {s} | {tname[t]} | {lead[t]} |" for s, (t, _) in SERVICES.items())
    docs = {}
    docs["engineering-handbook.md"] = f"""# Kestrel Labs Engineering Handbook

Kestrel Labs builds Kestrel, a workflow analytics product sold as a subscription (Starter, Growth and Enterprise
plans). Engineering is led by Vikram Menon, Chief Technology Officer.

## Who owns which service

Every production service has one owning team. The owning team deploys it, carries its pager and writes the
incident review when it breaks.

| Service | Owning team | Team lead |
| --- | --- | --- |
{svc_rows}

## How we ship

All changes reach production through the CI/CD pipeline run by Platform and DevOps. A change needs one approving
code review and a green build. Deploys run on working days between 09:00 and 19:00 IST; deploys outside that
window need approval from the on-call Site Reliability Engineer.

Billing-service and auth-service changes need a second reviewer from the owning team, because mistakes there cost
customers money or lock them out.

## What we measure (DORA)

We track four delivery measures for every team, from the deployments and incidents tables:

- **Deployment frequency:** production deploys per week. Target: at least daily for kestrel-api and web-app.
- **Lead time for changes:** hours from the first commit of a change to its production deploy. Target: under 24 hours.
- **Change failure rate:** share of production deploys that fail or are rolled back. Target: under 15 percent.
- **Time to restore:** hours from an incident being opened to service being restored. Target: under 4 hours for Sev1.

Quality reviews happen monthly. The engineering leads review the numbers with the CTO.
"""
    docs["on-call-policy.md"] = f"""# On-call and incident policy

Platform and DevOps (lead: Ravi Shankar) runs incident response for Kestrel Labs. Every engineering team with a
production service has a weekly on-call rotation; Platform and DevOps is the second line for all of them.

## Severity levels

| Severity | Meaning | First response | Who is paged |
| --- | --- | --- | --- |
| Sev1 | Kestrel is down or customers lose money or data | 15 minutes | Owning team on-call, Platform on-call, the CTO |
| Sev2 | A major feature is broken for many customers | 30 minutes | Owning team on-call, Platform on-call |
| Sev3 | A feature is degraded or broken for a few customers | Next working hours | Owning team |
| Sev4 | Cosmetic or minor | Planned work | Owning team |

## After an incident

Every Sev1 and Sev2 incident gets a written incident review within five working days, written by the incident
owner and reviewed by the owning team's lead. Reviews are blameless. Each review lists follow-up actions with
owners; Platform and DevOps tracks them until they are done.

Customer Success (lead: Leela Iyer) informs affected customers for every Sev1 incident, and for Sev2 incidents
that last more than two hours.
"""
    reviews = {
        "INC-2026-031": ("billing-service", "a retry in the renewal job sent the charge request twice when the payment "
                         "provider answered slowly", "Daniel Okafor",
                         ["Make renewal charges idempotent with a charge key (Backend, Daniel Okafor)",
                          "Alert when one customer is charged twice within a day (Finance and Backend)",
                          "Refund the 64 affected customers and email them (Customer Success, Leela Iyer)"]),
        "INC-2026-077": ("api-gateway", "an expired intermediate certificate was not included when certificates were "
                         "rotated, so clients could not complete TLS handshakes", "Ravi Shankar",
                         ["Automate certificate rotation and check the full chain before switching (Platform and DevOps)",
                          "Page the on-call 14 days before any certificate expires (Platform and DevOps)"]),
        "INC-2026-104": ("warehouse-sync", "a schema change in the events table made the warehouse sync stall without "
                         "failing, so the warehouse_build pipeline used 14-hour-old data", "Meera Krishnan",
                         ["Fail warehouse sync loudly on schema mismatches (Data Engineering)",
                          "Alert when data freshness is over 3 hours (Data Engineering, Meera Krishnan)",
                          "Add the events schema to the release checks (QA, Fatima Sheikh)"]),
    }
    for iid, (svc, cause, owner, actions) in reviews.items():
        r = by_id[iid]
        team = tname[r["team_id"]]
        dep = next((d for d in deployments if d["deploy_id"] == r["caused_by_deploy"]), None)
        trigger = (f"It started after deploy {dep['deploy_id']} of {svc} at {dep['deployed_at']}."
                   if dep else "It was not caused by a deploy.")
        acts = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(actions))
        docs[f"incident-review-{iid}.md"] = f"""# Incident review {iid}: {r['title']}

| Fact | Value |
| --- | --- |
| Severity | {r['severity']} |
| Service | {svc} (owned by {team}) |
| Opened | {r['opened_at']} IST |
| Restored | {r['resolved_at']} IST |
| Time to restore | {r['time_to_restore_hours']} hours |
| Incident owner | {owner} |

## What happened

{r['title']}. {trigger}

## Why it happened

The root cause was that {cause}.

## Follow-up actions

{acts}
"""
    charters = "\n\n".join(f"## {t[1]}\n\nLead: {t[4]}. Department: {t[2]}.\n\n{t[5]}" +
                           (f" Services: {', '.join(s for s, (tid, _) in SERVICES.items() if tid == t[0])}."
                            if any(tid == t[0] for tid, _ in SERVICES.values()) else "")
                           for t in TEAMS)
    docs["team-charters.md"] = f"# Kestrel Labs team charters\n\nKestrel Labs has {len(TEAMS)} teams. Anika Rao is the Chief Executive Officer.\n\n{charters}\n"
    docs["architecture-overview.md"] = """# Kestrel architecture overview

Kestrel runs on Kubernetes in AWS (ap-south-1, with a standby region in eu-west-1), operated by Platform and DevOps.

1. Every request enters through **api-gateway**, which checks the session with **auth-service**.
2. **kestrel-api** holds the product logic and stores data in PostgreSQL. **search-service** indexes workspaces
   with OpenSearch. **notification-service** sends email and push notifications.
3. **billing-service** charges customers through the payment provider every month or year and writes invoices.
4. **ingest-pipeline** collects product events; **warehouse-sync** copies product and billing data into the
   Snowflake warehouse every hour, where the warehouse_build pipeline builds the reporting tables.
5. **web-app** and **admin-console** (Frontend) and the **ios-app** and **android-app** (Mobile) call kestrel-api
   through api-gateway.

Data Engineering owns the warehouse. Finance reporting (MRR, ARR, churn) and the product analytics dashboards read
from it, so a stale warehouse means stale numbers for Finance and for customers' dashboards.
"""
    docs["pricing-policy.md"] = """# Pricing and discount policy

Kestrel is sold in three plans, billed monthly or annually in US dollars:

| Plan | List price per month | For |
| --- | --- | --- |
| Starter | USD 490 | Teams up to 25 users |
| Growth | USD 2,400 | Up to 200 users, single sign-on, API access |
| Enterprise | USD 9,800 and up | Unlimited users, dedicated success manager, 99.9% uptime commitment |

Annual billing gets 15 percent off. Account executives may give up to 10 percent extra discount. Discounts from
10 to 25 percent need approval from Tom Becker, Head of Sales. Anything above 25 percent needs approval from
Rohan Das, Chief Financial Officer. Product (Neha Kapoor) proposes price changes; Leadership approves them.
"""
    docs["security-policy.md"] = """# Security policy (summary)

- Everyone uses single sign-on with two-factor authentication. Platform and DevOps manages it.
- Production access is granted per person for 8 hours and is logged. Only Platform and DevOps and the on-call
  engineer of the owning team can request it.
- Customer data stays in the customer's region. Personal data is deleted 30 days after a customer leaves.
- Security incidents are always Sev1 and are led by Ravi Shankar with the CTO, Vikram Menon.
- People (Kavya Reddy) removes all access on a person's last day.
"""
    return docs


# ------------------------------------------------------------------ semantic layer (definitions)

def dims(*items):
    return [{"name": n, "column": n, "type": t, "label": l, "description": d} for n, t, l, d in items]


def semantic():
    teams_dims = dims(("team_id", "key", "Team ID", "Team identifier, T01 to T12."),
                      ("team_name", "category", "Team", "Team name, e.g. Backend or Data Engineering."),
                      ("department", "category", "Department", "Engineering, Commercial, Product, G&A or Leadership."),
                      ("lead", "text", "Team lead", "Name of the team lead."))
    model = {"version": 1, "entities": [
        {"name": "teams", "table": "teams", "label": "Teams", "primary_key": "team_id",
         "description": "One row per team.", "dimensions": teams_dims, "measures": []},
        {"name": "employees", "table": "employees", "label": "Employees", "primary_key": "employee_id",
         "description": "One row per person who works or worked at Kestrel Labs.",
         "dimensions": dims(("employee_id", "key", "Employee ID", ""), ("team_id", "key", "Team ID", ""),
                            ("title", "category", "Title", "Job title."), ("location", "category", "Location", "Office or Remote."),
                            ("hire_date", "time", "Hire date", ""), ("status", "category", "Status", "active or left."),
                            ("left_date", "time", "Left date", "Last day, for people who left.")), "measures": []},
        {"name": "deployments", "table": "deployments", "label": "Deployments", "primary_key": "deploy_id",
         "description": "One row per production deploy of a service.",
         "dimensions": dims(("deploy_id", "key", "Deploy ID", ""), ("service", "category", "Service", "The deployed service."),
                            ("team_id", "key", "Team ID", ""), ("deployed_at", "time", "Deployed at", ""),
                            ("status", "category", "Deploy status", "success, failed or rolled_back.")), "measures": []},
        {"name": "incidents", "table": "incidents", "label": "Incidents", "primary_key": "incident_id",
         "description": "One row per production incident.",
         "dimensions": dims(("incident_id", "key", "Incident ID", ""), ("severity", "category", "Severity", "Sev1 (worst) to Sev4."),
                            ("service", "category", "Service", ""), ("team_id", "key", "Team ID", ""),
                            ("opened_at", "time", "Opened at", "")), "measures": []},
        {"name": "tickets", "table": "tickets", "label": "Engineering tickets", "primary_key": "ticket_id",
         "description": "One row per engineering ticket in a two-week sprint.",
         "dimensions": dims(("ticket_id", "key", "Ticket ID", ""), ("team_id", "key", "Team ID", ""),
                            ("sprint", "category", "Sprint", "Sprint 1 started on 2025-01-06."), ("type", "category", "Ticket type", "story, bug or task."),
                            ("status", "category", "Ticket status", "done or carried over."),
                            ("sprint_start", "time", "Sprint start", "")), "measures": []},
        {"name": "pipeline_runs", "table": "pipeline_runs", "label": "Data pipeline runs", "primary_key": "run_id",
         "description": "One row per daily run of a Data Engineering pipeline.",
         "dimensions": dims(("run_id", "key", "Run ID", ""), ("pipeline", "category", "Pipeline", ""),
                            ("team_id", "key", "Team ID", ""), ("run_date", "time", "Run date", ""),
                            ("status", "category", "Run status", "success or failed.")), "measures": []},
        {"name": "cloud_costs", "table": "cloud_costs", "label": "Cloud costs", "primary_key": "month",
         "description": "Monthly cloud and tooling cost per team, provider and service, in US dollars.",
         "dimensions": dims(("month", "time", "Month", ""), ("team_id", "key", "Team ID", ""),
                            ("provider", "category", "Provider", "aws, snowflake or datadog."),
                            ("service", "category", "Cloud service", "compute, database, storage, network, warehouse or monitoring.")),
         "measures": []},
        {"name": "customers", "table": "customers", "label": "Customers", "primary_key": "customer_id",
         "description": "One row per customer company.",
         "dimensions": dims(("customer_id", "key", "Customer ID", ""), ("name", "text", "Customer", ""),
                            ("industry", "category", "Industry", ""), ("region", "category", "Region", "India, North America, Europe or APAC."),
                            ("plan", "category", "Plan at signup", "Starter, Growth or Enterprise."),
                            ("signup_date", "time", "Signup date", ""), ("status", "category", "Customer status", "active or churned."),
                            ("churned_at", "time", "Churn date", "")), "measures": []},
        {"name": "subscriptions", "table": "subscriptions", "label": "Subscriptions", "primary_key": "subscription_id",
         "description": "One row per subscription period of a customer; an upgrade ends one and starts the next.",
         "dimensions": dims(("subscription_id", "key", "Subscription ID", ""), ("customer_id", "key", "Customer ID", ""),
                            ("plan", "category", "Plan", "Starter, Growth or Enterprise."),
                            ("start_date", "time", "Start date", ""), ("status", "category", "Subscription status", "active, upgraded or cancelled.")),
         "measures": []},
        {"name": "support_tickets", "table": "support_tickets", "label": "Support tickets", "primary_key": "ticket_id",
         "description": "One row per customer support ticket.",
         "dimensions": dims(("ticket_id", "key", "Ticket ID", ""), ("customer_id", "key", "Customer ID", ""),
                            ("opened_at", "time", "Opened at", ""), ("priority", "category", "Priority", "urgent, high, normal or low."),
                            ("channel", "category", "Channel", "email, chat, portal or phone.")), "measures": []},
    ], "joins": [
        {"from_entity": e, "from_column": "team_id", "to_entity": "teams", "to_column": "team_id", "relationship": "many_to_one"}
        for e in ("employees", "deployments", "incidents", "tickets", "pipeline_runs", "cloud_costs")
    ] + [
        {"from_entity": e, "from_column": "customer_id", "to_entity": "customers", "to_column": "customer_id",
         "relationship": "many_to_one"} for e in ("subscriptions", "support_tickets")
    ]}

    def m(name, label, desc, entity, expr=None, typ="simple", time=None, fmt="number", filters=None, owner="", num=None, den=None):
        out = {"name": name, "label": label, "description": desc, "type": typ, "entity": entity, "format": fmt,
               "owner": owner, "status": "approved"}
        if expr:
            out["expression"] = expr
        if time:
            out["time_column"] = time
        if filters:
            out["filters"] = filters
        if num:
            out["numerator"], out["denominator"] = num, den
        return out
    metrics = {"version": 1, "metrics": [
        m("deployment_count", "Deployments", "Number of production deploys.", "deployments", "count(*)", time="deployments.deployed_at", owner="Platform and DevOps"),
        m("failed_deployments", "Failed deployments", "Production deploys that failed or were rolled back.", "deployments", "count(*)",
          time="deployments.deployed_at", filters=["deployments.status != 'success'"], owner="Platform and DevOps"),
        m("change_failure_rate", "Change failure rate", "Share of production deploys that failed or were rolled back (DORA).",
          "deployments", typ="ratio", num="failed_deployments", den="deployment_count", fmt="percent", owner="Platform and DevOps"),
        m("lead_time_for_changes", "Lead time for changes (hours)", "Average hours from first commit to production deploy (DORA).",
          "deployments", "avg(deployments.lead_time_hours)", time="deployments.deployed_at", owner="Platform and DevOps"),
        m("incident_count", "Incidents", "Number of production incidents.", "incidents", "count(*)", time="incidents.opened_at", owner="Platform and DevOps"),
        m("sev1_incidents", "Sev1 incidents", "Number of Sev1 incidents.", "incidents", "count(*)", time="incidents.opened_at",
          filters=["incidents.severity = 'Sev1'"], owner="Platform and DevOps"),
        m("time_to_restore", "Time to restore (hours)", "Average hours from an incident being opened to service being restored (DORA).",
          "incidents", "avg(incidents.time_to_restore_hours)", time="incidents.opened_at", owner="Platform and DevOps"),
        m("story_points_done", "Story points done (velocity)", "Story points of tickets finished in the sprint.", "tickets",
          "sum(tickets.story_points)", time="tickets.sprint_start", filters=["tickets.status = 'done'"], owner="Engineering"),
        m("ticket_cycle_time", "Ticket cycle time (days)", "Average days from a ticket being created to done.", "tickets",
          "avg(tickets.cycle_time_days)", time="tickets.sprint_start", filters=["tickets.status = 'done'"], owner="Engineering"),
        m("bugs_opened", "Bugs", "Number of bug tickets.", "tickets", "count(*)", time="tickets.sprint_start",
          filters=["tickets.type = 'bug'"], owner="QA"),
        m("pipeline_runs_count", "Pipeline runs", "Number of data pipeline runs.", "pipeline_runs", "count(*)", time="pipeline_runs.run_date", owner="Data Engineering"),
        m("successful_pipeline_runs", "Successful pipeline runs", "Pipeline runs that succeeded.", "pipeline_runs", "count(*)",
          time="pipeline_runs.run_date", filters=["pipeline_runs.status = 'success'"], owner="Data Engineering"),
        m("pipeline_success_rate", "Pipeline success rate", "Share of data pipeline runs that succeeded.", "pipeline_runs",
          typ="ratio", num="successful_pipeline_runs", den="pipeline_runs_count", fmt="percent", owner="Data Engineering"),
        m("data_freshness_hours", "Data freshness (hours)", "Average age in hours of the data a pipeline run delivered.",
          "pipeline_runs", "avg(pipeline_runs.freshness_hours)", time="pipeline_runs.run_date", owner="Data Engineering"),
        m("cloud_cost", "Cloud cost (USD)", "Cloud and tooling spend in US dollars.", "cloud_costs", "sum(cloud_costs.cost_usd)",
          time="cloud_costs.month", fmt="currency", owner="Finance"),
        m("headcount", "Headcount", "People currently employed.", "employees", "count(*)", filters=["employees.status = 'active'"], owner="People"),
        m("people_left", "People who left", "People whose last day was in the period.", "employees", "count(*)",
          time="employees.left_date", filters=["employees.status = 'left'"], owner="People"),
        m("mrr", "MRR (USD)", "Monthly recurring revenue: the monthly price of all active subscriptions, in US dollars.",
          "subscriptions", "sum(subscriptions.mrr_usd)", filters=["subscriptions.status = 'active'"], fmt="currency", owner="Finance"),
        m("arr", "ARR (USD)", "Annual recurring revenue: MRR times 12, in US dollars.", "subscriptions", "{mrr} * 12",
          typ="derived", fmt="currency", owner="Finance"),
        m("active_customers", "Active customers", "Customers who have not churned.", "customers", "count(*)",
          filters=["customers.status = 'active'"], owner="Customer Success"),
        m("new_customers", "New customers", "Customers who signed up in the period.", "customers", "count(*)",
          time="customers.signup_date", owner="Sales"),
        m("churned_customers", "Churned customers", "Customers who left in the period.", "customers", "count(*)",
          time="customers.churned_at", filters=["customers.status = 'churned'"], owner="Customer Success"),
        m("support_ticket_count", "Support tickets", "Number of customer support tickets.", "support_tickets", "count(*)",
          time="support_tickets.opened_at", owner="Customer Success"),
        m("first_response_minutes", "First response time (minutes)", "Average minutes to the first reply on a support ticket.",
          "support_tickets", "avg(support_tickets.first_response_minutes)", time="support_tickets.opened_at", owner="Customer Success"),
        m("csat", "CSAT", "Average customer satisfaction score (1 to 5) on support tickets.", "support_tickets",
          "avg(support_tickets.csat)", time="support_tickets.opened_at", owner="Customer Success"),
    ]}
    syn = [("deploys", "deployment_count"), ("releases", "deployment_count"), ("CFR", "change_failure_rate"),
           ("MTTR", "time_to_restore"), ("time to recovery", "time_to_restore"), ("lead time", "lead_time_for_changes"),
           ("velocity", "story_points_done"), ("cycle time", "ticket_cycle_time"), ("outages", "incident_count"),
           ("cloud spend", "cloud_cost"), ("AWS bill", "cloud_cost"), ("revenue", "mrr"), ("recurring revenue", "mrr"),
           ("annual revenue", "arr"), ("customer satisfaction", "csat"), ("employees", "headcount"),
           ("attrition", "people_left"), ("signups", "new_customers"), ("churn", "churned_customers"),
           ("freshness", "data_freshness_hours"), ("team", "teams.team_name"), ("squad", "teams.team_name"),
           ("devops", "teams.team_name=Platform and DevOps"), ("SRE", "teams.team_name=Platform and DevOps"),
           ("data team", "teams.team_name=Data Engineering"), ("CS", "teams.team_name=Customer Success")]
    enrichment = {"version": 1, "synonyms": [{"term": t, "target": g} for t, g in syn],
                  "value_list_dimensions": ["teams.team_name", "teams.department", "deployments.service", "deployments.status",
                                            "incidents.severity", "incidents.service", "tickets.type", "pipeline_runs.pipeline",
                                            "cloud_costs.provider", "cloud_costs.service", "customers.region",
                                            "customers.industry", "customers.plan", "subscriptions.plan",
                                            "support_tickets.priority", "support_tickets.channel", "employees.location"]}
    instructions = """# Business rules for answering data questions (Kestrel Labs)

- **Currency:** all money is in US dollars (USD).
- **Fiscal year:** April to March. "FY2026-27" means 2026-04-01 to 2027-03-31.
- **Revenue:** "revenue" without qualification means MRR (monthly recurring revenue of active subscriptions).
  ARR is MRR times 12. Revenue is a current figure, not a sum over time.
- **DORA metrics:** deployment frequency = deployment_count per week; change failure rate, lead time for changes and
  time to restore are defined in the metrics. Targets are in the Engineering Handbook.
- **Teams:** split engineering numbers by teams.team_name. Platform and DevOps is also called SRE or DevOps.
- **Velocity** counts only finished tickets (status done) and is reported per sprint.
- **Headcount** is people currently employed (status active).
"""
    return model, metrics, enrichment, instructions


def main():
    people = make_people()
    deployments = make_deployments(people)
    incidents = make_incidents(people, deployments)
    customers, subs = make_customers(people)
    data = {
        "teams": [{"team_id": t[0], "team_name": t[1], "department": t[2], "lead": t[4]} for t in TEAMS],
        "employees": [{k: p[k] for k in ("employee_id", "name", "email", "title", "team_id", "location", "hire_date",
                                         "status", "left_date")} for p in people],
        "deployments": deployments, "incidents": incidents, "tickets": make_tickets(people),
        "pipeline_runs": make_pipeline_runs(), "cloud_costs": make_cloud_costs(), "customers": customers,
        "subscriptions": subs, "support_tickets": make_support(customers, people),
    }
    for name, rows in data.items():
        write_csv(OUT / "data" / f"{name}.csv", rows)
    for name, text in documents(people, incidents, deployments).items():
        (OUT / "documents").mkdir(parents=True, exist_ok=True)
        (OUT / "documents" / name).write_text(text, encoding="utf-8")
    write_csv(OUT / "directory.csv", [{"name": p["name"], "email": p["email"], "title": p["title"], "team": p["team"],
                                       "manager_email": p["manager_email"], "location": p["location"],
                                       "status": p["status"]} for p in people])
    (OUT / "teams.json").write_text(json.dumps([{"name": t[1], "lead_email": email_of(t[4]),
                                                 "parent": None, "description": t[5]} for t in TEAMS], indent=1))
    model, metrics, enrichment, instructions = semantic()
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from semantic.yamlio import raw_write
    lay = OUT / "semantic_layer"
    raw_write(lay / "model.yaml", model, "Kestrel Labs demo company: semantic model.")
    raw_write(lay / "metrics.yaml", metrics, "Kestrel Labs demo company: metric definitions.")
    raw_write(lay / "enrichment.yaml", enrichment, "Kestrel Labs demo company: everyday words.")
    raw_write(lay / "verified_queries.yaml", {"version": 1, "queries": []}, "Kestrel Labs: verified example requests.")
    (lay / "instructions.md").write_text(instructions, encoding="utf-8")
    print({k: len(v) for k, v in data.items()}, "documents:", len(list((OUT / "documents").glob("*.md"))))


if __name__ == "__main__":
    main()
