"""Pure business logic for DealerPulse.

No AWS imports on purpose: everything in this file can be unit-tested on a
laptop in milliseconds. The Lambda handlers (ingest.py, api.py) are thin
wrappers around these functions.

Pipeline:  raw CSV text -> parse_and_validate() -> compute_metrics()
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------
STAGES = ["New", "Contacted", "TestDrive", "Negotiation", "Booked", "Delivered"]
LOST = "Lost"
SOLD_STATUSES = {"Booked", "Delivered"}
SOURCES = ["Walk-in", "Website", "Referral", "Phone", "Social"]
SEGMENTS = ["Hatchback", "Sedan", "SUV", "MPV", "EV"]

REQUIRED_COLUMNS = [
    "lead_id", "created_date", "customer_name", "phone", "source",
    "model_segment", "status", "salesperson", "city",
    "last_followup_date", "sale_amount",
]
# customer_name and the raw phone number are deliberately NOT in the output:
# analytics does not need personal data, so it never leaves the raw/ prefix.
OUTPUT_COLUMNS = [
    "lead_id", "created_date", "source", "model_segment", "status",
    "salesperson", "city", "last_followup_date", "sale_amount", "phone_masked",
]

OVERDUE_AFTER_DAYS = 7
MIN_SALE, MAX_SALE = 100_000, 50_000_000  # INR, plausibility bounds
MAX_OVERDUE_LISTED = 50


def _key(value: str) -> str:
    """Normalise labels so 'Test Drive', 'test_drive' and 'TESTDRIVE' match."""
    return re.sub(r"[\s_\-]", "", value).lower()


_STATUS_LOOKUP = {_key(s): s for s in STAGES + [LOST]}
_SOURCE_LOOKUP = {_key(s): s for s in SOURCES}
_SEGMENT_LOOKUP = {_key(s): s for s in SEGMENTS}
_LEAD_ID_RE = re.compile(r"^[A-Z0-9\-]{3,20}$")


@dataclass
class ValidationResult:
    rows: List[dict]          # cleaned, de-duplicated rows
    errors: List[dict]        # one entry per rejected row
    duplicates_removed: int
    total_rows: int           # data rows seen in the file (excl. header)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Return a 10-digit Indian mobile number, or None if it is not valid."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def mask_phone(phone: str) -> str:
    return "XXXXXX" + phone[-4:]


# ---------------------------------------------------------------------------
# Validation / cleaning
# ---------------------------------------------------------------------------
def clean_row(raw: Dict[str, str], today: date) -> Tuple[Optional[dict], List[str]]:
    """Validate one CSV row. Returns (clean_row, []) or (None, [reasons])."""
    reasons: List[str] = []

    def get(col: str) -> str:
        return (raw.get(col) or "").strip()

    lead_id = get("lead_id").upper()
    if not _LEAD_ID_RE.match(lead_id):
        reasons.append("invalid or missing lead_id")

    created = followup = None
    try:
        created = parse_date(get("created_date"))
        if created > today:
            reasons.append("created_date is in the future")
    except ValueError:
        reasons.append("created_date must be YYYY-MM-DD")
    try:
        followup = parse_date(get("last_followup_date"))
        if followup > today:
            reasons.append("last_followup_date is in the future")
    except ValueError:
        reasons.append("last_followup_date must be YYYY-MM-DD")
    if created and followup and followup < created:
        reasons.append("last_followup_date is before created_date")

    status = _STATUS_LOOKUP.get(_key(get("status")))
    if status is None:
        reasons.append(f"unknown status '{get('status')}'")
    source = _SOURCE_LOOKUP.get(_key(get("source")))
    if source is None:
        reasons.append(f"unknown source '{get('source')}'")
    segment = _SEGMENT_LOOKUP.get(_key(get("model_segment")))
    if segment is None:
        reasons.append(f"unknown model_segment '{get('model_segment')}'")

    phone = normalize_phone(get("phone"))
    if phone is None:
        reasons.append("invalid phone number")

    salesperson = " ".join(get("salesperson").split()).title()
    city = " ".join(get("city").split()).title()
    if not salesperson:
        reasons.append("missing salesperson")
    if not city:
        reasons.append("missing city")

    raw_amount = get("sale_amount").replace(",", "")
    amount: Optional[int] = None
    if raw_amount:
        if not raw_amount.isdigit():
            reasons.append("sale_amount must be a whole number")
        else:
            amount = int(raw_amount)
            if not MIN_SALE <= amount <= MAX_SALE:
                reasons.append("sale_amount outside plausible range")
    if status in SOLD_STATUSES and not raw_amount:
        reasons.append("sale_amount required for Booked/Delivered")
    if status and status not in SOLD_STATUSES and raw_amount:
        reasons.append("sale_amount only allowed for Booked/Delivered")

    if reasons:
        return None, reasons
    return {
        "lead_id": lead_id,
        "created_date": created.isoformat(),
        "source": source,
        "model_segment": segment,
        "status": status,
        "salesperson": salesperson,
        "city": city,
        "last_followup_date": followup.isoformat(),
        "sale_amount": amount,
        "phone_masked": mask_phone(phone),
    }, []


def parse_and_validate(text: str, today: Optional[date] = None) -> ValidationResult:
    """Parse CSV text. Bad rows are collected, not fatal; a bad header is fatal."""
    today = today or date.today()
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValueError("missing required columns: " + ", ".join(missing))
    reader.fieldnames = header

    best: Dict[str, dict] = {}
    errors: List[dict] = []
    total = duplicates = 0
    for line_no, raw in enumerate(reader, start=2):  # line 1 is the header
        total += 1
        clean, reasons = clean_row(raw, today)
        if reasons:
            errors.append({
                "row": line_no,
                "lead_id": (raw.get("lead_id") or "").strip(),
                "reasons": reasons,
            })
            continue
        existing = best.get(clean["lead_id"])
        if existing is None:
            best[clean["lead_id"]] = clean
        else:  # same lead appears twice: keep the most recently followed-up
            duplicates += 1
            if clean["last_followup_date"] >= existing["last_followup_date"]:
                best[clean["lead_id"]] = clean
    return ValidationResult(list(best.values()), errors, duplicates, total)


def rows_to_csv(rows: List[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: ("" if r[c] is None else r[c]) for c in OUTPUT_COLUMNS})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _group(rows: List[dict], field: str) -> List[dict]:
    buckets: Dict[str, dict] = defaultdict(lambda: {"leads": 0, "sold": 0, "revenue": 0})
    for r in rows:
        b = buckets[r[field]]
        b["leads"] += 1
        if r["status"] in SOLD_STATUSES:
            b["sold"] += 1
            b["revenue"] += r["sale_amount"]
    return [
        {"name": name, **b, "conversion_rate": _rate(b["sold"], b["leads"])}
        for name, b in buckets.items()
    ]


def compute_metrics(rows: List[dict], today: Optional[date] = None) -> dict:
    today = today or date.today()
    total = len(rows)
    sold = [r for r in rows if r["status"] in SOLD_STATUSES]
    revenue = sum(r["sale_amount"] for r in sold)

    # Funnel: how many NON-lost leads are at or beyond each stage right now.
    # Limitation: we only have each lead's *current* status, not its history,
    # so Lost leads cannot be attributed to the stage where they dropped out.
    stage_index = {s: i for i, s in enumerate(STAGES)}
    active = [r for r in rows if r["status"] != LOST]
    funnel = [
        {"stage": s, "leads": sum(1 for r in active if stage_index[r["status"]] >= i)}
        for i, s in enumerate(STAGES)
    ]

    overdue = []
    for r in rows:
        if r["status"] in SOLD_STATUSES or r["status"] == LOST:
            continue
        days = (today - date.fromisoformat(r["last_followup_date"])).days
        if days > OVERDUE_AFTER_DAYS:
            overdue.append({
                "lead_id": r["lead_id"],
                "salesperson": r["salesperson"],
                "status": r["status"],
                "days_since_followup": days,
                "phone_masked": r["phone_masked"],
            })
    overdue.sort(key=lambda x: (-x["days_since_followup"], x["lead_id"]))

    return {
        "as_of": today.isoformat(),
        "totals": {
            "leads": total,
            "sold": len(sold),
            "lost": sum(1 for r in rows if r["status"] == LOST),
            "conversion_rate": _rate(len(sold), total),
            "revenue": revenue,
            "avg_deal_value": round(revenue / len(sold)) if sold else 0,
        },
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "funnel": funnel,
        "by_source": sorted(_group(rows, "source"), key=lambda x: -x["conversion_rate"]),
        "by_segment": sorted(_group(rows, "model_segment"), key=lambda x: -x["revenue"]),
        "by_month": sorted(
            _group([{**r, "month": r["created_date"][:7]} for r in rows], "month"),
            key=lambda x: x["name"],
        ),
        "salespeople": sorted(_group(rows, "salesperson"), key=lambda x: -x["revenue"])[:10],
        "overdue_followups": {
            "threshold_days": OVERDUE_AFTER_DAYS,
            "count": len(overdue),
            "top": overdue[:MAX_OVERDUE_LISTED],
        },
    }
