-- Optional: ad-hoc SQL over the cleaned data with Amazon Athena.
-- Replace <BUCKET> with the BucketName stack output. Run in the Athena console
-- (set a query-results location first). Athena bills per data scanned, so at
-- demo size this costs effectively nothing.

CREATE DATABASE IF NOT EXISTS dealerpulse;

CREATE EXTERNAL TABLE IF NOT EXISTS dealerpulse.leads (
  lead_id             string,
  created_date        string,
  source              string,
  model_segment       string,
  status              string,
  salesperson         string,
  city                string,
  last_followup_date  string,
  sale_amount         string,
  phone_masked        string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"')
LOCATION 's3://<BUCKET>/processed/'
TBLPROPERTIES ('skip.header.line.count' = '1');

-- 1. Conversion and revenue by source and segment
SELECT source, model_segment,
       count(*) AS leads,
       count_if(status IN ('Booked','Delivered')) AS sold,
       round(100.0 * count_if(status IN ('Booked','Delivered')) / count(*), 1) AS conversion_pct,
       sum(try_cast(NULLIF(sale_amount,'') AS bigint)) AS revenue
FROM dealerpulse.leads
GROUP BY source, model_segment
ORDER BY revenue DESC NULLS LAST;

-- 2. Which salespeople let open leads go cold? (days since last follow-up)
SELECT salesperson,
       count(*) AS open_leads,
       count_if(date_diff('day', date(last_followup_date), current_date) > 7) AS overdue,
       round(100.0 * count_if(date_diff('day', date(last_followup_date), current_date) > 7) / count(*), 1) AS overdue_pct
FROM dealerpulse.leads
WHERE status NOT IN ('Booked','Delivered','Lost')
GROUP BY salesperson
ORDER BY overdue_pct DESC;

-- 3. Monthly revenue with month-over-month change (window function)
WITH monthly AS (
  SELECT substr(created_date, 1, 7) AS month,
         sum(try_cast(NULLIF(sale_amount,'') AS bigint)) AS revenue
  FROM dealerpulse.leads
  GROUP BY 1
)
SELECT month, revenue,
       revenue - lag(revenue) OVER (ORDER BY month) AS change_vs_prev_month
FROM monthly
ORDER BY month;
