"""Generate the Lumora Energy Systems test set (fictional company; all names invented).

Tables (data):    employees.xlsx (sheets employees, departments), customers.jsonl, tickets.json (nested), invoices.csv
Documents (docs): lumora_handbook.md, lumora_products.md, lumora_incidents.json, lumora_service_catalog.csv
Gold:             lumora_retrieval_gold.json (question -> answer snippets that must be in the retrieved passages)
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

R = random.Random(42)
OUT = Path(__file__).resolve().parent / "lumora"
(OUT / "data").mkdir(parents=True, exist_ok=True)
(OUT / "docs").mkdir(parents=True, exist_ok=True)

DEPTS = [("D10", "Grid Analytics", "E1001", "CC-410"), ("D20", "Field Operations", "E1002", "CC-420"),
         ("D30", "Customer Success", "E1003", "CC-430"), ("D40", "Platform Engineering", "E1004", "CC-440"),
         ("D50", "Finance", "E1005", "CC-450")]
HEADS = {"E1001": "Meera Kulkarni", "E1002": "Tomas Rivera", "E1003": "Aisha Bello", "E1004": "Jonas Weber",
         "E1005": "Priya Nair"}
FIRST = ["Arjun", "Lena", "Kofi", "Sara", "Ravi", "Mia", "Omar", "Ines", "Yuki", "Diego", "Nadia", "Felix", "Zara",
         "Hugo", "Anika", "Leo", "Maya", "Ivan", "Chen", "Elif"]
LAST = ["Shah", "Novak", "Mensah", "Lund", "Iyer", "Costa", "Haddad", "Moreau", "Tanaka", "Silva", "Petrov", "Okafor"]
CITIES = ["Pune", "Bengaluru", "Hyderabad", "Chennai", "Mumbai"]
TITLES = {"D10": ["Data Scientist", "Analytics Engineer"], "D20": ["Field Engineer", "Site Supervisor"],
          "D30": ["Customer Success Manager", "Support Specialist"], "D40": ["Software Engineer", "SRE"],
          "D50": ["Accountant", "Financial Analyst"]}

emps = []
for eid, name in HEADS.items():
    d = next(x for x in DEPTS if x[2] == eid)
    emps.append({"Employee ID": eid, "Full Name": name, "Department ID": d[0], "Title": f"Head of {d[1]}",
                 "City": R.choice(CITIES), "Hire Date": date(2019, R.randint(1, 12), R.randint(1, 28))})
for i in range(6, 121):
    d = R.choice(DEPTS)
    emps.append({"Employee ID": f"E{1000 + i}", "Full Name": f"{R.choice(FIRST)} {R.choice(LAST)}",
                 "Department ID": d[0], "Title": R.choice(TITLES[d[0]]), "City": R.choice(CITIES),
                 "Hire Date": date(2020, 1, 1) + timedelta(days=R.randint(0, 2400))})
with pd.ExcelWriter(OUT / "data" / "employees.xlsx") as xw:
    pd.DataFrame(emps).to_excel(xw, sheet_name="Employees", index=False)
    pd.DataFrame([{"Department ID": d[0], "Department Name": d[1], "Head Employee ID": d[2], "Cost Center": d[3]}
                  for d in DEPTS]).to_excel(xw, sheet_name="Departments", index=False)

TIERS = ["Enterprise", "Growth", "Starter"]
REGIONS = ["North", "South", "East", "West"]
INDUSTRIES = ["Utility", "Manufacturing", "Real Estate", "Municipal"]
CUST_NAMES = ["Northbay Utilities", "Cedar Valley Co-op", "Harbor Steelworks", "Pinecrest Estates", "Riverbend Municipal",
              "Summit Foods", "Oakfield Textiles", "Bluewater Port Authority", "Granite Ridge Mining", "Meadowlark Homes",
              "Ironwood Plastics", "Silverlake Water Board", "Copperline Rail", "Brightwell Hospitals", "Stonegate Malls",
              "Westwind Logistics", "Lakeshore Cold Storage", "Redcliff Cement", "Elmstead Schools", "Foxglove Pharma"]
customers = []
for i, n in enumerate(CUST_NAMES, 1):
    customers.append({"customer_id": f"CU{i:03d}", "name": n, "tier": TIERS[i % 3], "region": REGIONS[i % 4],
                      "industry": INDUSTRIES[i % 4], "customer_since": str(date(2021, 1, 1) + timedelta(days=60 * i))})
customers[0].update(tier="Enterprise", region="West")
(OUT / "data" / "customers.jsonl").write_text("\n".join(json.dumps(c) for c in customers), encoding="utf-8")

PRODUCTS = ["PulseMeter", "GridSense", "FieldPro"]
tickets = []
for i in range(1, 401):
    c = R.choice(customers)
    opened = date(2025, 1, 1) + timedelta(days=R.randint(0, 690))
    hours = R.choice([2, 4, 8, 12, 24, 48, 72])
    tickets.append({"ticket_id": f"T{i:04d}", "opened_at": str(opened), "priority": R.choice(["low", "medium", "high", "urgent"]),
                    "product": R.choice(PRODUCTS), "channel": R.choice(["email", "phone", "portal"]),
                    "customer": {"customer_id": c["customer_id"], "name": c["name"], "tier": c["tier"]},
                    "assignee_id": R.choice([e["Employee ID"] for e in emps if e["Department ID"] == "D30"]),
                    "resolution_hours": hours, "csat": R.choice([2, 3, 4, 4, 5, 5])})
(OUT / "data" / "tickets.json").write_text(json.dumps({"exported": "2026-11-20", "tickets": tickets}, indent=1), encoding="utf-8")

inv = []
for i in range(1, 601):
    c = R.choice(customers)
    issued = date(2025, 1, 1) + timedelta(days=R.randint(0, 690))
    inv.append({"invoice_id": f"INV{i:05d}", "customer_id": c["customer_id"], "issued_date": str(issued),
                "due_date": str(issued + timedelta(days=30)), "amount": round(R.uniform(800, 25000), 2),
                "status": R.choices(["paid", "open", "overdue"], [0.75, 0.15, 0.10])[0],
                "product": R.choice(PRODUCTS)})
pd.DataFrame(inv).to_csv(OUT / "data" / "invoices.csv", index=False)

# ---------------------------------------------------------------- documents
handbook = """# Lumora Energy Systems: Employee Handbook

## Who we are
Lumora Energy Systems builds metering and grid-analytics products for utilities and large energy users. The company has five departments.

## Grid Analytics
The Grid Analytics department builds forecasting models and dashboards. It is headed by Meera Kulkarni, who joined Lumora from a national load-dispatch centre. Its cost center is CC-410.

## Field Operations
Field Operations installs and maintains meters at customer sites. The department is headed by Tomas Rivera. Field engineers work in rotating on-call shifts of seven days.

## Customer Success
Customer Success handles onboarding, renewals and support tickets. Aisha Bello heads the department. Every Enterprise customer has a named Customer Success Manager.

## Platform Engineering
Platform Engineering runs the cloud platform, device firmware and the release process. It is led by Jonas Weber. Sev1 incidents are escalated to the head of Platform Engineering within 15 minutes.

## Finance
Finance owns invoicing, collections and budgets. Priya Nair is the head of Finance. Invoices are due 30 days after issue; reminders go out at day 25 and day 35.

## Leave policy
Employees get 24 days of paid leave per year. Leave of up to 10 consecutive working days is approved by the employee's line manager. Leave longer than 10 consecutive working days must be approved by the head of the employee's department and by HR. Unused leave of up to 8 days can be carried over to the next year.

## Travel policy
Domestic travel is booked through the travel desk at least 7 days ahead. International travel needs approval from the head of Finance. Field engineers may claim a daily site allowance of INR 1,200.

## Incident severity
Sev1 means a customer-wide outage or data loss; the response target is 15 minutes, around the clock. Sev2 means a major feature is unavailable; the response target is 2 hours during business hours. Sev3 covers minor defects, answered within 2 business days.

## Security
All laptops use full-disk encryption. Customer meter data is stored in the Mumbai cloud region and is never copied to personal devices. Access reviews happen every quarter.
"""
(OUT / "docs" / "lumora_handbook.md").write_text(handbook, encoding="utf-8")

products = """# Lumora product sheets

## PulseMeter
PulseMeter is a smart three-phase electricity meter with an LTE-M modem. It is designed and maintained by the Platform Engineering department. PulseMeter reports readings every 15 minutes and stores 90 days of data locally. The hardware warranty is 5 years. Current firmware is version 4.2, released in August 2026.

## GridSense
GridSense is a cloud analytics service that forecasts load and detects anomalies across a utility's network. It is owned by the Grid Analytics department. GridSense is sold as an annual subscription priced per connected meter. Forecasts are refreshed every hour. GridSense has no hardware warranty; the subscription includes a 99.9% availability commitment.

## FieldPro
FieldPro is the mobile app field engineers use to install, test and replace meters. It is owned by the Field Operations department. FieldPro works offline and syncs when a network is available. FieldPro devices (rugged tablets) carry a 2-year warranty.
"""
(OUT / "docs" / "lumora_products.md").write_text(products, encoding="utf-8")

incidents = [
    {"incident_id": "INC-2041", "date": "2026-09-14", "severity": "Sev1", "product": "PulseMeter",
     "reported_by": "Northbay Utilities", "summary": "Firmware 4.1 caused PulseMeter units to stop reporting after a daylight-saving clock change.",
     "root_cause": "a time-zone conversion bug in the firmware scheduler", "resolution": "Platform Engineering shipped firmware 4.2 within 36 hours.",
     "owner": "Jonas Weber"},
    {"incident_id": "INC-2057", "date": "2026-10-02", "severity": "Sev2", "product": "GridSense",
     "reported_by": "Cedar Valley Co-op", "summary": "Hourly forecasts were delayed by up to three hours.",
     "root_cause": "a stuck message queue after a cloud maintenance window", "resolution": "Queue workers were restarted and an alert was added.",
     "owner": "Meera Kulkarni"},
    {"incident_id": "INC-2063", "date": "2026-10-19", "severity": "Sev3", "product": "FieldPro",
     "reported_by": "Harbor Steelworks", "summary": "Photos attached in FieldPro were missing after offline sync.",
     "root_cause": "an image-compression timeout on older tablets", "resolution": "Fixed in FieldPro 3.8.",
     "owner": "Tomas Rivera"},
]
for i in range(1, 41):        # distractors: routine incidents
    p = R.choice(PRODUCTS)
    incidents.append({"incident_id": f"INC-{1900 + i}", "date": str(date(2025, 3, 1) + timedelta(days=9 * i)),
                      "severity": R.choice(["Sev2", "Sev3", "Sev3"]), "product": p, "reported_by": R.choice(CUST_NAMES[3:]),
                      "summary": R.choice(["Readings arrived late for some meters.", "A dashboard widget showed stale data.",
                                           "Sync failed for a few devices.", "Users saw a login error.", "An export file was incomplete."]),
                      "root_cause": R.choice(["network congestion", "an expired certificate", "a configuration error", "a disk filling up"]),
                      "resolution": "Resolved by the on-call team.", "owner": R.choice(list(HEADS.values()))})
(OUT / "docs" / "lumora_incidents.json").write_text(json.dumps({"incidents": incidents}, indent=1), encoding="utf-8")

catalog = pd.DataFrame([
    {"service": "Meter installation", "product": "PulseMeter", "team": "Field Operations", "lead_time_days": 10, "price_inr": 4500},
    {"service": "Firmware upgrade", "product": "PulseMeter", "team": "Platform Engineering", "lead_time_days": 2, "price_inr": 0},
    {"service": "Forecast onboarding", "product": "GridSense", "team": "Grid Analytics", "lead_time_days": 21, "price_inr": 150000},
    {"service": "Custom dashboard", "product": "GridSense", "team": "Grid Analytics", "lead_time_days": 14, "price_inr": 60000},
    {"service": "Tablet replacement", "product": "FieldPro", "team": "Field Operations", "lead_time_days": 5, "price_inr": 38000},
])
catalog.to_csv(OUT / "docs" / "lumora_service_catalog.csv", index=False)

gold = [
    # single facts
    {"question": "Who is the head of Customer Success at Lumora?", "type": "single", "snippets": ["Aisha Bello heads the department"]},
    {"question": "How often does PulseMeter report readings?", "type": "single", "snippets": ["PulseMeter reports readings every 15 minutes"]},
    {"question": "What is the response target for a Sev2 incident?", "type": "single", "snippets": ["the response target is 2 hours during business hours"]},
    {"question": "How many days of paid leave do Lumora employees get?", "type": "single", "snippets": ["24 days of paid leave per year"]},
    {"question": "What caused incident INC-2041?", "type": "single", "snippets": ["time-zone conversion bug in the firmware scheduler"]},
    {"question": "What is the lead time for forecast onboarding?", "type": "single", "snippets": ["Forecast onboarding"]},
    {"question": "Where is customer meter data stored?", "type": "single", "snippets": ["stored in the Mumbai cloud region"]},
    {"question": "What daily site allowance can field engineers claim?", "type": "single", "snippets": ["daily site allowance of INR 1,200"]},
    {"question": "Which incident did Harbor Steelworks report about missing photos?", "type": "single", "snippets": ["Photos attached in FieldPro were missing"]},
    {"question": "How long is the PulseMeter hardware warranty?", "type": "single", "snippets": ["The hardware warranty is 5 years"]},
    # two hops
    {"question": "Who heads the department that owns GridSense?", "type": "multi_hop",
     "snippets": ["It is owned by the Grid Analytics department", "It is headed by Meera Kulkarni"]},
    {"question": "Which department maintains the product affected by incident INC-2041, and who leads it?", "type": "multi_hop",
     "snippets": ["Firmware 4.1 caused PulseMeter units", "designed and maintained by the Platform Engineering department", "It is led by Jonas Weber"]},
    {"question": "Who must approve a 12-day leave for a field engineer, and who is that person in Field Operations?", "type": "multi_hop",
     "snippets": ["must be approved by the head of the employee's department and by HR", "The department is headed by Tomas Rivera"]},
    {"question": "Which team owns the app whose photos went missing after offline sync?", "type": "multi_hop",
     "snippets": ["Photos attached in FieldPro were missing", "It is owned by the Field Operations department"]},
    {"question": "Who approves international travel, and what is that person's name?", "type": "multi_hop",
     "snippets": ["International travel needs approval from the head of Finance", "Priya Nair is the head of Finance"]},
    {"question": "Within how many minutes must a Sev1 incident with PulseMeter reach the head of the team that maintains it?", "type": "multi_hop",
     "snippets": ["Sev1 incidents are escalated to the head of Platform Engineering within 15 minutes", "designed and maintained by the Platform Engineering department"]},
    # comparison
    {"question": "How do the warranties of PulseMeter and FieldPro devices differ?", "type": "comparison",
     "snippets": ["The hardware warranty is 5 years", "carry a 2-year warranty"]},
    {"question": "Compare how often PulseMeter reports readings with how often GridSense refreshes forecasts.", "type": "comparison",
     "snippets": ["PulseMeter reports readings every 15 minutes", "Forecasts are refreshed every hour"]},
    {"question": "What did incidents INC-2057 and INC-2063 have as root causes?", "type": "comparison",
     "snippets": ["a stuck message queue", "an image-compression timeout"]},
    {"question": "Which departments are led by Meera Kulkarni and Jonas Weber?", "type": "comparison",
     "snippets": ["It is headed by Meera Kulkarni", "It is led by Jonas Weber"]},
]
(OUT / "lumora_retrieval_gold.json").write_text(json.dumps(gold, indent=1), encoding="utf-8")
print("written", OUT)
