# Evidence from the live deployment

Captured on 2026-10-06 from the deployed stack in `ap-south-1`
(account 897201144484). Nothing here is hand-written: every file was read back
out of AWS or produced by a browser rendering the dashboard.

| File | What it proves |
|---|---|
| `api_metrics_response.json` | Full payload returned by `GET /metrics` on the live HTTP API |
| `api_runs_response.json` | The ingestion audit trail from `GET /runs` |
| `quarantine_test_leads.errors.json` | Both rejected rows, each with its reason - bad data is reported, never silently dropped |
| `processed_sample.csv` | The cleaned output: no `customer_name` column, phone numbers masked |
| `cloudwatch_ingest.log` | The ingest Lambda's own log line plus the `REPORT` line with duration and memory |
| `dashboard_live.png` | The hosted demo rendering live API data: 58 leads, 22.4% conversion, all four charts drawn |
| `dashboard_offline_fallback.png` | Dashboard with the Chart.js CDN blocked - KPIs and tables still render |

## The run these files describe

Input: `raw/test_leads.csv`, 61 synthetic rows with deliberate defects.

| Measure | Value |
|---|---|
| Rows read | 61 |
| Valid after cleaning | 58 |
| Rejected (quarantined with reasons) | 2 |
| Duplicates removed | 1 |
| Ingest duration | 356.98 ms (642 ms billed) |
| Cold start | 284.22 ms |
| Peak memory | 103 MB of 256 MB allocated |

These are single-run figures from a 61-row file. They are not a load test and
should not be quoted as throughput.
