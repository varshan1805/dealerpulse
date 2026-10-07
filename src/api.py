"""Read-only API Lambda behind an API Gateway HTTP API.

Routes:
  GET /health   liveness check
  GET /metrics  latest dashboard metrics (written by ingest.py)
  GET /runs     the 10 most recent ingestion runs (audit trail)

CORS and throttling are configured in template.yaml, not here.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

table = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "unset"))


def _json_default(value):
    # DynamoDB returns numbers as Decimal, which json can't serialise by default.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not serialisable: {type(value)}")


def _response(status: int, body: dict, cache_seconds: int = 30) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": f"max-age={cache_seconds}",
        },
        "body": json.dumps(body, default=_json_default),
    }


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    try:
        if route == "GET /health":
            return _response(200, {"status": "ok"}, cache_seconds=0)

        if route == "GET /metrics":
            item = table.get_item(Key={"pk": "METRICS", "sk": "LATEST"}).get("Item")
            if not item:
                return _response(404, {"error": "no data yet: upload a CSV to raw/"})
            body = json.loads(item["payload"])
            body["meta"] = {"source_key": item["source_key"], "updated_at": item["updated_at"]}
            return _response(200, body)

        if route == "GET /runs":
            items = table.query(
                KeyConditionExpression=Key("pk").eq("RUN"),
                ScanIndexForward=False,  # newest first
                Limit=10,
            ).get("Items", [])
            for item in items:
                item.pop("expires_at", None)
                item.pop("pk", None)
            return _response(200, {"runs": items})

        return _response(404, {"error": f"unknown route {route!r}"}, cache_seconds=0)
    except Exception:
        LOG.exception("unhandled error on %s", route)
        # Never leak internals to the caller.
        return _response(500, {"error": "internal error"}, cache_seconds=0)
