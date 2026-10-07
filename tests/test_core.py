import os
import sys
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import core  # noqa: E402
import generate_data  # noqa: E402

TODAY = date(2026, 10, 7)
HEADER = ",".join(core.REQUIRED_COLUMNS)


def row(**overrides):
    base = {
        "lead_id": "L00001", "created_date": "2026-08-01", "customer_name": "Test User",
        "phone": "9876543210", "source": "Referral", "model_segment": "SUV",
        "status": "Contacted", "salesperson": "hari prasad", "city": "chennai",
        "last_followup_date": "2026-08-05", "sale_amount": "",
    }
    base.update(overrides)
    return base


def csv_of(*rows):
    lines = [HEADER]
    for r in rows:
        lines.append(",".join(str(r[c]) for c in core.REQUIRED_COLUMNS))
    return "\n".join(lines) + "\n"


class CleanRowTests(unittest.TestCase):
    def test_valid_row_is_normalised(self):
        clean, reasons = core.clean_row(row(), TODAY)
        self.assertEqual(reasons, [])
        self.assertEqual(clean["salesperson"], "Hari Prasad")
        self.assertEqual(clean["city"], "Chennai")
        self.assertEqual(clean["phone_masked"], "XXXXXX3210")

    def test_pii_is_not_in_output(self):
        clean, _ = core.clean_row(row(), TODAY)
        self.assertNotIn("customer_name", clean)
        self.assertNotIn("phone", clean)
        self.assertNotIn("9876543210", str(clean))

    def test_label_variants_are_accepted(self):
        clean, reasons = core.clean_row(row(status="test drive", source="WALK-IN"), TODAY)
        self.assertEqual(reasons, [])
        self.assertEqual(clean["status"], "TestDrive")
        self.assertEqual(clean["source"], "Walk-in")

    def test_phone_formats(self):
        for raw in ["+91 98765 43210", "09876543210", "98765-43210", "919876543210"]:
            self.assertEqual(core.normalize_phone(raw), "9876543210", raw)
        for bad in ["12345", "5876543210", "", None, "98765432100000"]:
            self.assertIsNone(core.normalize_phone(bad), bad)

    def test_rejections(self):
        cases = {
            "bad status": row(status="Maybe"),
            "bad phone": row(phone="12345"),
            "missing id": row(lead_id=""),
            "future date": row(created_date="2026-11-01"),
            "bad date format": row(created_date="01/08/2026"),
            "followup before created": row(last_followup_date="2026-07-01"),
            "amount on open lead": row(sale_amount="750000"),
            "sold without amount": row(status="Booked", sale_amount=""),
            "absurd amount": row(status="Booked", sale_amount="5"),
            "non-numeric amount": row(status="Booked", sale_amount="lots"),
        }
        for label, r in cases.items():
            clean, reasons = core.clean_row(r, TODAY)
            self.assertIsNone(clean, label)
            self.assertTrue(reasons, label)

    def test_amount_with_commas_is_accepted(self):
        clean, reasons = core.clean_row(row(status="Booked", sale_amount="7,50,000"), TODAY)
        self.assertEqual(reasons, [])
        self.assertEqual(clean["sale_amount"], 750000)

    def test_multiple_reasons_reported_together(self):
        _, reasons = core.clean_row(row(status="Maybe", phone="1"), TODAY)
        self.assertGreaterEqual(len(reasons), 2)


class ParseTests(unittest.TestCase):
    def test_missing_columns_is_fatal(self):
        with self.assertRaises(ValueError) as ctx:
            core.parse_and_validate("lead_id,status\nL1,New\n", TODAY)
        self.assertIn("missing required columns", str(ctx.exception))

    def test_header_case_and_whitespace_tolerated(self):
        text = csv_of(row()).replace("lead_id", " Lead_ID ", 1)
        result = core.parse_and_validate(text, TODAY)
        self.assertEqual(len(result.rows), 1)

    def test_bad_rows_do_not_stop_good_rows(self):
        text = csv_of(row(), row(lead_id="L00002", status="Maybe"), row(lead_id="L00003"))
        result = core.parse_and_validate(text, TODAY)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["row"], 3)  # header is line 1
        self.assertEqual(result.total_rows, 3)

    def test_duplicates_keep_latest_followup_in_either_order(self):
        old = row(last_followup_date="2026-08-02", status="New")
        new = row(last_followup_date="2026-09-01", status="Negotiation")
        for ordering in ([old, new], [new, old]):
            result = core.parse_and_validate(csv_of(*ordering), TODAY)
            self.assertEqual(len(result.rows), 1)
            self.assertEqual(result.rows[0]["status"], "Negotiation")
            self.assertEqual(result.duplicates_removed, 1)

    def test_output_csv_has_no_pii_columns(self):
        result = core.parse_and_validate(csv_of(row()), TODAY)
        out = core.rows_to_csv(result.rows)
        self.assertNotIn("customer_name", out)
        self.assertNotIn("9876543210", out)
        self.assertEqual(out.splitlines()[0], ",".join(core.OUTPUT_COLUMNS))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        rows = [
            row(lead_id="LA01", status="New", last_followup_date="2026-10-06"),
            row(lead_id="LA02", status="Contacted", last_followup_date="2026-09-01"),   # overdue 36d
            row(lead_id="LA03", status="TestDrive", last_followup_date="2026-09-25"),   # overdue 12d
            row(lead_id="LA04", status="Booked", sale_amount="1000000", source="Website"),
            row(lead_id="LA05", status="Delivered", sale_amount="500000", source="Website"),
            row(lead_id="LA06", status="Lost", last_followup_date="2026-08-02"),        # never overdue
        ]
        result = core.parse_and_validate(csv_of(*rows), TODAY)
        self.assertEqual(result.errors, [])
        self.m = core.compute_metrics(result.rows, TODAY)

    def test_totals(self):
        t = self.m["totals"]
        self.assertEqual(t["leads"], 6)
        self.assertEqual(t["sold"], 2)
        self.assertEqual(t["lost"], 1)
        self.assertEqual(t["revenue"], 1_500_000)
        self.assertEqual(t["avg_deal_value"], 750_000)
        self.assertAlmostEqual(t["conversion_rate"], 2 / 6, places=3)

    def test_funnel_is_monotonically_non_increasing(self):
        counts = [s["leads"] for s in self.m["funnel"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(counts[0], 5)   # all non-lost leads
        self.assertEqual(counts[-1], 1)  # only Delivered

    def test_overdue_excludes_sold_lost_and_recent(self):
        ids = [o["lead_id"] for o in self.m["overdue_followups"]["top"]]
        self.assertEqual(ids, ["LA02", "LA03"])  # sorted worst-first
        self.assertEqual(self.m["overdue_followups"]["count"], 2)

    def test_source_conversion(self):
        web = next(s for s in self.m["by_source"] if s["name"] == "Website")
        self.assertEqual((web["leads"], web["sold"], web["conversion_rate"]), (2, 2, 1.0))

    def test_empty_input_does_not_crash_or_divide_by_zero(self):
        m = core.compute_metrics([], TODAY)
        self.assertEqual(m["totals"]["conversion_rate"], 0.0)
        self.assertEqual(m["totals"]["avg_deal_value"], 0)


class GeneratorIntegrationTests(unittest.TestCase):
    def test_clean_data_has_no_errors(self):
        text = generate_data.generate(rows=400, seed=1, dirty=False, today=TODAY)
        result = core.parse_and_validate(text, TODAY)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.rows), 400)

    def test_dirty_data_is_caught_and_cleaned(self):
        text = generate_data.generate(rows=600, seed=2, dirty=True, today=TODAY)
        result = core.parse_and_validate(text, TODAY)
        self.assertGreater(len(result.errors), 0)
        self.assertGreater(result.duplicates_removed, 0)
        self.assertGreater(len(result.rows), 500)
        ids = [r["lead_id"] for r in result.rows]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates survive
        m = core.compute_metrics(result.rows, TODAY)
        self.assertGreater(m["totals"]["revenue"], 0)

    def test_generator_is_deterministic(self):
        a = generate_data.generate(rows=50, seed=9, today=TODAY)
        b = generate_data.generate(rows=50, seed=9, today=TODAY)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
