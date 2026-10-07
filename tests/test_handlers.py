"""Handler tests. AWS is fully stubbed, so these run offline.

If boto3 isn't installed (e.g. a bare laptop), a stub module is injected so the
handlers can still be imported. In CI boto3 is installed and the real module is
used; only the clients are replaced with mocks.
"""
import io
import json
import os
import sys
import types
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ["TABLE_NAME"] = "test-table"
os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:ap-south-1:000000000000:test"

try:
    import boto3  # noqa: F401
except ImportError:  # pragma: no cover
    fake = types.ModuleType("boto3")
    fake.client = mock.MagicMock()
    fake.resource = mock.MagicMock()
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Key = mock.MagicMock()
    dynamodb = types.ModuleType("boto3.dynamodb")
    dynamodb.conditions = conditions
    fake.dynamodb = dynamodb
    sys.modules.update({"boto3": fake, "boto3.dynamodb": dynamodb,
                        "boto3.dynamodb.conditions": conditions})

import api  # noqa: E402
import generate_data  # noqa: E402
import ingest  # noqa: E402


def s3_event(key, size=1000, bucket="b"):
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}}}]}


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.s3 = mock.MagicMock()
        self.table = mock.MagicMock()
        self.sns = mock.MagicMock()
        patches = [mock.patch.object(ingest, "s3", self.s3),
                   mock.patch.object(ingest, "table", self.table),
                   mock.patch.object(ingest, "sns", self.sns)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def serve(self, text):
        self.s3.get_object.return_value = {"Body": io.BytesIO(text.encode("utf-8"))}

    def put_keys(self):
        return [c.kwargs["Key"] for c in self.s3.put_object.call_args_list]

    def items(self):
        return [c.kwargs["Item"] for c in self.table.put_item.call_args_list]

    def test_clean_file_writes_processed_metrics_and_run(self):
        self.serve(generate_data.generate(rows=100, seed=3, today=date.today()))
        out = ingest.lambda_handler(s3_event("raw/sales.csv"), None)
        self.assertEqual(out["processed"][0]["status"], "ok")
        self.assertEqual(self.put_keys(), ["processed/sales.csv"])  # no quarantine file
        pks = [i["pk"] for i in self.items()]
        self.assertEqual(pks, ["METRICS", "RUN"])
        payload = json.loads(self.items()[0]["payload"])
        self.assertEqual(payload["totals"]["leads"], 100)
        self.sns.publish.assert_not_called()

    def test_dirty_file_quarantines_and_alerts(self):
        self.serve(generate_data.generate(rows=300, seed=4, dirty=True, today=date.today()))
        out = ingest.lambda_handler(s3_event("raw/dirty.csv"), None)
        self.assertEqual(out["processed"][0]["status"], "ok")
        self.assertIn("quarantine/dirty.errors.json", self.put_keys())
        self.assertGreater(out["processed"][0]["rows_rejected"], 0)
        self.sns.publish.assert_called_once()

    def test_processed_output_contains_no_pii(self):
        self.serve(generate_data.generate(rows=50, seed=5, today=date.today()))
        ingest.lambda_handler(s3_event("raw/x.csv"), None)
        body = next(c.kwargs["Body"] for c in self.s3.put_object.call_args_list
                    if c.kwargs["Key"].startswith("processed/")).decode()
        self.assertNotIn("customer_name", body)
        self.assertNotIn("phone,", body)

    def test_missing_columns_is_rejected_not_retried(self):
        self.serve("lead_id,status\nL1,New\n")
        out = ingest.lambda_handler(s3_event("raw/bad.csv"), None)  # must NOT raise
        self.assertEqual(out["processed"][0]["status"], "rejected")
        self.table.put_item.assert_not_called()
        self.assertEqual(self.put_keys(), ["quarantine/bad.errors.json"])
        self.sns.publish.assert_called_once()

    def test_all_rows_invalid_keeps_previous_metrics(self):
        header = "lead_id,created_date,customer_name,phone,source,model_segment,status,salesperson,city,last_followup_date,sale_amount\n"
        self.serve(header + ",,,,,,,,,,\n")
        ingest.lambda_handler(s3_event("raw/empty.csv"), None)
        self.assertNotIn("METRICS", [i["pk"] for i in self.items()])

    def test_oversized_file_rejected_without_reading(self):
        out = ingest.lambda_handler(s3_event("raw/huge.csv", size=ingest.MAX_BYTES + 1), None)
        self.assertEqual(out["processed"][0]["status"], "rejected")
        self.s3.get_object.assert_not_called()

    def test_non_raw_or_non_csv_keys_are_skipped(self):
        for key in ["processed/a.csv", "raw/a.txt"]:
            out = ingest.lambda_handler(s3_event(key), None)
            self.assertEqual(out["processed"][0]["status"], "skipped")
        self.s3.get_object.assert_not_called()

    def test_url_encoded_keys_are_decoded(self):
        self.serve(generate_data.generate(rows=10, seed=6, today=date.today()))
        ingest.lambda_handler(s3_event("raw/my+file%281%29.csv"), None)
        self.assertEqual(self.s3.get_object.call_args.kwargs["Key"], "raw/my file(1).csv")

    def test_transient_aws_error_propagates_so_lambda_retries(self):
        self.s3.get_object.side_effect = RuntimeError("throttled")
        with self.assertRaises(RuntimeError):
            ingest.lambda_handler(s3_event("raw/a.csv"), None)

    def test_alert_failure_does_not_fail_ingest(self):
        self.sns.publish.side_effect = RuntimeError("sns down")
        self.serve("lead_id,status\nL1,New\n")
        out = ingest.lambda_handler(s3_event("raw/bad.csv"), None)
        self.assertEqual(out["processed"][0]["status"], "rejected")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.table = mock.MagicMock()
        p = mock.patch.object(api, "table", self.table)
        p.start()
        self.addCleanup(p.stop)

    def call(self, route):
        resp = api.lambda_handler({"routeKey": route}, None)
        return resp["statusCode"], json.loads(resp["body"]), resp["headers"]

    def test_health(self):
        status, body, _ = self.call("GET /health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_metrics_returns_payload_with_meta(self):
        self.table.get_item.return_value = {"Item": {
            "payload": json.dumps({"totals": {"leads": 5}}),
            "source_key": "raw/a.csv", "updated_at": "2026-10-07T00:00:00+00:00"}}
        status, body, headers = self.call("GET /metrics")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["leads"], 5)
        self.assertEqual(body["meta"]["source_key"], "raw/a.csv")
        self.assertIn("max-age", headers["Cache-Control"])

    def test_metrics_404_before_first_upload(self):
        self.table.get_item.return_value = {}
        status, body, _ = self.call("GET /metrics")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_runs_serialises_decimals_and_strips_internal_fields(self):
        from decimal import Decimal
        self.table.query.return_value = {"Items": [{
            "pk": "RUN", "sk": "t#a", "rows_total": Decimal("10"),
            "rows_rejected": Decimal("1"), "expires_at": Decimal("123")}]}
        status, body, _ = self.call("GET /runs")
        self.assertEqual(status, 200)
        run = body["runs"][0]
        self.assertEqual(run["rows_total"], 10)
        self.assertNotIn("expires_at", run)
        self.assertNotIn("pk", run)

    def test_unknown_route_404_and_errors_do_not_leak_internals(self):
        self.assertEqual(self.call("GET /nope")[0], 404)
        self.table.get_item.side_effect = RuntimeError("secret table name")
        status, body, _ = self.call("GET /metrics")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
