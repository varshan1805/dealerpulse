"""S3-triggered ingestion Lambda.

Flow for every CSV uploaded to  s3://<bucket>/raw/<name>.csv :

  1. read + validate + de-duplicate        (core.parse_and_validate)
  2. write PII-free cleaned CSV            -> processed/<name>.csv
  3. write rejected rows with reasons      -> quarantine/<name>.errors.json
  4. compute dashboard metrics             -> DynamoDB  METRICS/LATEST
  5. record an audit item for the run      -> DynamoDB  RUN/<timestamp>#<name>
  6. alert via SNS if anything was rejected

Error-handling policy (this is the interesting design decision):
  * BAD DATA (missing columns, bad encoding, oversized file) is a permanent
    failure. Retrying cannot fix it, so we quarantine + alert and RETURN.
  * INFRASTRUCTURE errors (throttling, network) are transient. We let the
    exception propagate so Lambda retries, then the SQS dead-letter queue
    catches anything that still fails.
Writes are keyed by file name, so a duplicate S3 event simply overwrites the
same outputs: the handler is idempotent.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

import boto3

import core

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("TABLE_NAME", "unset")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
MAX_BYTES = 10 * 1024 * 1024
RUN_TTL_DAYS = 90
MAX_ERRORS_STORED = 1000

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME)
sns = boto3.client("sns")


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        size = record["s3"]["object"].get("size", 0)
        results.append(process_object(bucket, key, size))
    return {"processed": results}


def process_object(bucket: str, key: str, size: int) -> dict:
    # Defence in depth: the S3 notification already filters on raw/*.csv, and
    # outputs go to other prefixes so we can never trigger ourselves in a loop.
    if not key.startswith("raw/") or not key.lower().endswith(".csv"):
        LOG.info("skipping %s", key)
        return {"key": key, "status": "skipped"}
    if size > MAX_BYTES:
        return _reject(bucket, key, f"file too large ({size} bytes, limit {MAX_BYTES})")

    now = datetime.now(timezone.utc)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        text = body.decode("utf-8-sig")  # tolerate the BOM Excel adds
        result = core.parse_and_validate(text, today=now.date())
    except ValueError as exc:  # also covers UnicodeDecodeError
        return _reject(bucket, key, str(exc))

    name = key[len("raw/"):].rsplit(".", 1)[0]
    rejected = len(result.errors)

    if result.rows:
        s3.put_object(
            Bucket=bucket, Key=f"processed/{name}.csv",
            Body=core.rows_to_csv(result.rows).encode("utf-8"), ContentType="text/csv",
        )
        metrics = core.compute_metrics(result.rows, today=now.date())
        table.put_item(Item={
            "pk": "METRICS", "sk": "LATEST",
            # Stored as one JSON string: the dashboard always reads the whole
            # thing, and it sidesteps DynamoDB's float/Decimal restrictions.
            "payload": json.dumps(metrics),
            "source_key": key, "updated_at": now.isoformat(),
        })

    if result.errors:
        s3.put_object(
            Bucket=bucket, Key=f"quarantine/{name}.errors.json",
            Body=json.dumps({
                "file": key, "total_rejected": rejected,
                "errors": result.errors[:MAX_ERRORS_STORED],
            }, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

    table.put_item(Item={
        "pk": "RUN", "sk": f"{now.isoformat()}#{name}",
        "source_key": key, "rows_total": result.total_rows,
        "rows_valid": len(result.rows), "rows_rejected": rejected,
        "duplicates_removed": result.duplicates_removed,
        "expires_at": int((now + timedelta(days=RUN_TTL_DAYS)).timestamp()),
    })

    LOG.info("%s: %d rows, %d valid, %d rejected, %d duplicates",
             key, result.total_rows, len(result.rows), rejected, result.duplicates_removed)
    if result.errors or not result.rows:
        _alert(f"DealerPulse: {rejected} rows rejected in {key}",
               f"{key}: {result.total_rows} rows, {len(result.rows)} valid, {rejected} rejected. "
               f"Details: quarantine/{name}.errors.json")
    # Note: if NO rows were valid we deliberately keep the previous METRICS/LATEST
    # rather than overwriting a good dashboard with an empty one.
    return {"key": key, "status": "ok", "rows_valid": len(result.rows), "rows_rejected": rejected}


def _reject(bucket: str, key: str, reason: str) -> dict:
    LOG.error("rejecting %s: %s", key, reason)
    name = key[len("raw/"):].rsplit(".", 1)[0] if key.startswith("raw/") else key
    s3.put_object(
        Bucket=bucket, Key=f"quarantine/{name}.errors.json",
        Body=json.dumps({"file": key, "fatal_error": reason}, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    _alert(f"DealerPulse: file rejected: {key}", reason)
    return {"key": key, "status": "rejected", "reason": reason}


def _alert(subject: str, message: str) -> None:
    if not ALERT_TOPIC_ARN:
        return
    try:
        sns.publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:100], Message=message)
    except Exception:  # an alerting failure must never fail the ingest itself
        LOG.exception("could not publish alert")
