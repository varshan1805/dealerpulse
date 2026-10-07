"""Generate a realistic, SYNTHETIC dealership leads CSV.

No real customer data is used anywhere in this project.

    python scripts/generate_data.py --rows 800 --dirty --out leads.csv

--dirty injects the kinds of problems real CRM exports have (bad phone numbers,
impossible dates, duplicates, inconsistent labels) so the validation layer has
something real to catch.
"""
from __future__ import annotations

import argparse
import csv
import io
import random
from datetime import date, timedelta
from typing import List, Optional

COLUMNS = [
    "lead_id", "created_date", "customer_name", "phone", "source",
    "model_segment", "status", "salesperson", "city",
    "last_followup_date", "sale_amount",
]
FIRST = ["Arun", "Divya", "Karthik", "Meena", "Suresh", "Priya", "Vignesh", "Lakshmi",
         "Ravi", "Kavya", "Manoj", "Anitha", "Prakash", "Nandhini", "Sathish", "Deepa"]
LAST = ["Kumar", "Raj", "Subramanian", "Iyer", "Nair", "Pillai", "Krishnan", "Murugan",
        "Babu", "Selvam", "Ramesh", "Natarajan"]
SALESPEOPLE = ["Hari Prasad", "Sneha R", "Bala Murugan", "Farhan Ali",
               "Janani K", "Vijay Anand", "Pooja S", "Rohit M"]
CITIES = ["Chennai", "Coimbatore", "Madurai", "Trichy", "Salem"]
SOURCES = ["Walk-in", "Website", "Referral", "Phone", "Social"]
SEGMENTS = ["Hatchback", "SUV", "Sedan", "MPV", "EV"]
SEGMENT_WEIGHTS = [40, 28, 14, 10, 8]
PRICE = {  # (min, max) INR
    "Hatchback": (550_000, 900_000), "Sedan": (900_000, 1_500_000),
    "SUV": (1_100_000, 2_200_000), "MPV": (1_000_000, 1_800_000),
    "EV": (1_200_000, 2_500_000),
}
STATUSES = ["New", "Contacted", "TestDrive", "Negotiation", "Booked", "Delivered", "Lost"]
STATUS_WEIGHTS = [15, 20, 15, 12, 12, 14, 12]
SOLD = {"Booked", "Delivered"}
# Referral/walk-in leads close better than social leads: gives the dashboard a
# real pattern to show instead of uniform noise.
SOURCE_SOLD_BOOST = {"Referral": 1.8, "Walk-in": 1.4, "Website": 1.0, "Phone": 0.9, "Social": 0.5}


def generate_rows(rows: int, seed: int, today: date) -> List[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(1, rows + 1):
        source = rng.choice(SOURCES)
        segment = rng.choices(SEGMENTS, SEGMENT_WEIGHTS)[0]
        weights = [w * (SOURCE_SOLD_BOOST[source] if s in SOLD else 1.0)
                   for s, w in zip(STATUSES, STATUS_WEIGHTS)]
        status = rng.choices(STATUSES, weights)[0]
        created = today - timedelta(days=rng.randint(1, 180))
        age = (today - created).days
        if status in SOLD or status == "Lost":
            followup = created + timedelta(days=rng.randint(0, age))
        else:
            # Open leads: most were followed up recently, a minority have gone stale.
            days_ago = rng.randint(0, 7) if rng.random() < 0.72 else rng.randint(8, 45)
            followup = max(created, today - timedelta(days=days_ago))
        amount = ""
        if status in SOLD:
            lo, hi = PRICE[segment]
            amount = round(rng.randint(lo, hi), -3)
        out.append({
            "lead_id": f"L{i:05d}",
            "created_date": created.isoformat(),
            "customer_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "phone": f"{rng.randint(6, 9)}{rng.randint(0, 999_999_999):09d}",
            "source": source,
            "model_segment": segment,
            "status": status,
            "salesperson": rng.choice(SALESPEOPLE),
            "city": rng.choice(CITIES),
            "last_followup_date": followup.isoformat(),
            "sale_amount": amount,
        })
    return out


def make_dirty(rows: List[dict], seed: int, today: date) -> List[dict]:
    """Inject realistic problems. About 4% rejectable, ~5% messy-but-valid."""
    rng = random.Random(seed + 1)
    n = len(rows)

    # Messy but VALID: the cleaner should normalise these, not reject them.
    for r in rng.sample(rows, k=max(1, n // 20)):
        kind = rng.choice(["status", "source", "phone", "name"])
        if kind == "status" and r["status"] == "TestDrive":
            r["status"] = "test drive"
        elif kind == "source" and r["source"] == "Walk-in":
            r["source"] = "WALK-IN"
        elif kind == "phone":
            p = r["phone"]
            r["phone"] = f"+91 {p[:5]} {p[5:]}"
        elif kind == "name":
            r["customer_name"] = r["customer_name"].upper()

    # Rejectable problems.
    for r in rng.sample(rows, k=max(1, n // 25)):
        kind = rng.choice(["status", "phone", "id", "future", "amount_extra", "amount_missing"])
        if kind == "status":
            r["status"] = "Maybe"
        elif kind == "phone":
            r["phone"] = "12345"
        elif kind == "id":
            r["lead_id"] = ""
        elif kind == "future":
            r["created_date"] = (today + timedelta(days=30)).isoformat()
        elif kind == "amount_extra" and r["status"] not in SOLD:
            r["sale_amount"] = 750000
        elif kind == "amount_missing" and r["status"] in SOLD:
            r["sale_amount"] = ""

    # Duplicates: same lead exported twice (older copy first).
    dupes = []
    for r in rng.sample(rows, k=max(1, n // 60)):
        older = dict(r)
        older["last_followup_date"] = r["created_date"]
        dupes.append(older)
    return dupes + rows


def generate(rows: int = 500, seed: int = 42, dirty: bool = False,
             today: Optional[date] = None) -> str:
    today = today or date.today()
    data = generate_rows(rows, seed, today)
    if dirty:
        data = make_dirty(data, seed, today)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(data)
    return buf.getvalue()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dirty", action="store_true", help="inject realistic data-quality problems")
    ap.add_argument("--out", default="leads.csv")
    args = ap.parse_args()
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        fh.write(generate(args.rows, args.seed, args.dirty))
    print(f"wrote {args.out}")
