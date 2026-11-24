"""Deterministic date resolution for business questions (no model, no extra library).

Finds period phrases in a question and turns them into inclusive (start, end) dates using the
business rules in semantic_layer/instructions.md:
  - fiscal year April-March ("FY2025-26", "this fiscal year", "last FY", "fiscal Q1 of FY2026-27")
  - "last quarter" = last completed CALENDAR quarter; "Q2 2026" = calendar quarter
  - "last month", "this month", "this year", "last year", "in 2025", "first half of 2025", "H2 2025",
    "October 2026", "since 1 September 2026", "last 90 days"

The planner gets these as hints, and a single unambiguous hint overrides the planner's dates.
"""
import re
from calendar import monthrange
from datetime import date, timedelta

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS |= {m[:3]: i for m, i in list(MONTHS.items())}


def _month_end(y, m):
    return date(y, m, monthrange(y, m)[1])


def _quarter(y, q):
    return date(y, 3 * q - 2, 1), _month_end(y, 3 * q)


def _fy(start_year):
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def _fy_start_year(token: str) -> int:
    """'2025-26', '25-26', '2026' (FY ending), '26' -> start year of the fiscal year."""
    a = re.match(r"(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?$", token)
    first, second = a.group(1), a.group(2)
    y = int(first) + (2000 if len(first) == 2 else 0)
    return y if second else y - 1                                        # "FY2026" = year ending March 2026


def resolve(question: str, today: date) -> list[dict]:
    q = question.lower()
    hits: list[dict] = []

    def add(phrase, start, end):
        hits.append({"phrase": phrase, "start_date": start, "end_date": end})

    cur_fy = today.year if today.month >= 4 else today.year - 1
    # fiscal quarter of an explicit FY: "fiscal q1 of fy2026-27", "q2 fy25-26"
    for m in re.finditer(r"(?:fiscal\s+)?q([1-4])\s+(?:of\s+)?fy\s*(\d{2,4}(?:\s*[-/]\s*\d{2,4})?)", q):
        fy = _fy_start_year(m.group(2))
        qn = int(m.group(1))
        start = date(fy, 4, 1) if qn == 1 else date(fy, 7, 1) if qn == 2 else date(fy, 10, 1) if qn == 3 else date(fy + 1, 1, 1)
        add(m.group(0), start, _month_end(start.year, start.month + 2))
    if not hits:
        for m in re.finditer(r"\bfy\s*(\d{2,4}(?:\s*[-/]\s*\d{2,4})?)", q):
            add(m.group(0), *_fy(_fy_start_year(m.group(1))))
    if re.search(r"\b(this|current)\s+(fiscal\s+year|fy)\b", q):
        add("this fiscal year", *_fy(cur_fy))
    if re.search(r"\b(last|previous|prior)\s+(fiscal\s+year|fy)\b", q):
        add("last fiscal year", *_fy(cur_fy - 1))
    cq = (today.month - 1) // 3 + 1
    if re.search(r"\b(last|previous|prior)\s+quarter\b", q) and "fiscal quarter" not in q:
        y, qq = (today.year, cq - 1) if cq > 1 else (today.year - 1, 4)
        add("last quarter", *_quarter(y, qq))
    if re.search(r"\b(this|current)\s+quarter\b", q):
        add("this quarter", _quarter(today.year, cq)[0], today)
    for m in re.finditer(r"(?<!fiscal )\bq([1-4])\s*(?:of\s+)?(20\d{2})\b", q):
        add(m.group(0), *_quarter(int(m.group(2)), int(m.group(1))))
    if re.search(r"\b(last|previous|prior)\s+month\b", q):
        first = today.replace(day=1) - timedelta(days=1)
        add("last month", first.replace(day=1), first)
    if re.search(r"\b(this|current)\s+month\b", q):
        add("this month", today.replace(day=1), today)
    for m in re.finditer(r"\b(first|1st|second|2nd)\s+half\s+of\s+(20\d{2})\b|\bh([12])\s+(20\d{2})\b", q):
        half = 1 if (m.group(1) or "").startswith(("first", "1st")) or m.group(3) == "1" else 2
        y = int(m.group(2) or m.group(4))
        add(m.group(0), date(y, 1, 1) if half == 1 else date(y, 7, 1), date(y, 6, 30) if half == 1 else date(y, 12, 31))
    m = re.search(r"\bsince\s+(?:(\d{1,2})\s+)?([a-z]+)\s+(20\d{2})\b", q)
    if m and m.group(2)[:3] in MONTHS:
        add(m.group(0), date(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1) or 1)), today)
    m = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+days\b", q)
    if m:
        add(m.group(0), today - timedelta(days=int(m.group(1)) - 1), today)
    if not hits:                                             # month + year, e.g. "October 2026"
        for m in re.finditer(r"\b([a-z]+)\s+(20\d{2})\b", q):
            if m.group(1) in MONTHS:
                y, mo = int(m.group(2)), MONTHS[m.group(1)]
                add(m.group(0), date(y, mo, 1), _month_end(y, mo))
    if re.search(r"\b(this|current)\s+year\b", q):
        add("this year", date(today.year, 1, 1), date(today.year, 12, 31))
    if re.search(r"\b(last|previous)\s+year\b", q):
        add("last year", date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    if not hits:                                             # a bare calendar year: "in 2025", "for 2026"
        years = re.findall(r"\b(?:in|for|during|of)\s+(20\d{2})\b", q)
        if len(years) == 1:
            y = int(years[0])
            add(f"in {y}", date(y, 1, 1), date(y, 12, 31))
    return hits
