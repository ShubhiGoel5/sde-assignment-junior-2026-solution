"""Builds the reports in SQL from the ledger and the leads.

Nothing is cached, so the result only depends on what is in the database, not on the order
it was loaded. It goes through a few temp tables:

    t_link        each event with the lead it links to (NULL = orphan)
    t_calls       one row per counted call (has a linked call.initiated)
    t_call_facts  adds connected / telephony_failed / effective disposition
    t_lead_report one row per lead, with its bucket
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

DERIVE_SQL = """
DROP TABLE IF EXISTS temp.t_link;
DROP TABLE IF EXISTS temp.t_call_lead;
DROP TABLE IF EXISTS temp.t_calls;
DROP TABLE IF EXISTS temp.t_call_facts;
DROP TABLE IF EXISTS temp.t_lead_report;

-- 1. Link events to leads by (workflow_id, phone). Overrides have no phone, added below.
CREATE TEMP TABLE t_link AS
SELECT e.event_id, e.workflow_id, e.call_id, e.event_type, e.occurred_at_utc,
       e.disposition_code, l.lead_id
FROM events e
LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_token = e.phone_token
WHERE e.event_type IS NOT 'disposition.override';

-- A call's lead is the lead of its call.initiated, or else of any other event with a phone.
CREATE TEMP TABLE t_call_lead AS
SELECT workflow_id, call_id,
       COALESCE(MIN(CASE WHEN event_type = 'call.initiated' THEN lead_id END), MIN(lead_id)) AS lead_id
FROM t_link
WHERE lead_id IS NOT NULL AND call_id IS NOT NULL
GROUP BY workflow_id, call_id;
CREATE UNIQUE INDEX temp.ix_call_lead ON t_call_lead (workflow_id, call_id);

INSERT INTO t_link
SELECT o.event_id, o.workflow_id, o.call_id, o.event_type, o.occurred_at_utc,
       o.disposition_code, c.lead_id
FROM events o
LEFT JOIN t_call_lead c ON c.workflow_id = o.workflow_id AND c.call_id = o.call_id
WHERE o.event_type = 'disposition.override';

CREATE INDEX temp.ix_link_call ON t_link (workflow_id, call_id, event_type);

-- 2. A call only counts if its call.initiated is present and linked.
CREATE TEMP TABLE t_calls AS
SELECT workflow_id, call_id, lead_id, MIN(occurred_at_utc) AS initiated_at
FROM t_link
WHERE event_type = 'call.initiated' AND lead_id IS NOT NULL
  AND call_id IS NOT NULL AND occurred_at_utc IS NOT NULL
GROUP BY workflow_id, call_id;

-- 3. Per-call facts. Effective disposition is the latest override, else the latest set.
--    Codes not in the catalogue are dropped by the join.
CREATE TEMP TABLE t_call_facts AS
SELECT c.workflow_id, c.call_id, c.lead_id, c.initiated_at,
       date(c.initiated_at, '+330 minutes') AS ist_date,
       EXISTS (SELECT 1 FROM t_link k
               WHERE k.workflow_id = c.workflow_id AND k.call_id = c.call_id
                 AND k.event_type = 'call.connected' AND k.lead_id = c.lead_id) AS connected,
       EXISTS (SELECT 1 FROM t_link k
               WHERE k.workflow_id = c.workflow_id AND k.call_id = c.call_id
                 AND k.event_type = 'call.telephony_failed' AND k.lead_id = c.lead_id) AS telephony_failed,
       (SELECT k.disposition_code
        FROM t_link k
        JOIN dispositions d ON d.workflow_id = k.workflow_id AND d.code = k.disposition_code
        WHERE k.workflow_id = c.workflow_id AND k.call_id = c.call_id
          AND k.event_type IN ('disposition.set', 'disposition.override')
          AND k.lead_id = c.lead_id
        ORDER BY (k.event_type = 'disposition.override') DESC,
                 k.occurred_at_utc DESC, k.event_id DESC
        LIMIT 1) AS disposition
FROM t_calls c;
CREATE INDEX temp.ix_facts_lead ON t_call_facts (lead_id, initiated_at);

-- 4. Per-lead rollup and bucket.
CREATE TEMP TABLE t_lead_report AS
WITH agg AS (
    SELECT l.lead_id, l.workflow_id, l.external_ref, l.batch_id, l.phone_last4,
           COUNT(f.call_id)                      AS total_calls,
           COALESCE(SUM(f.connected), 0)         AS connected_calls,
           MAX(f.initiated_at)                   AS last_call_utc,
           COALESCE(MAX(d.is_terminal), 0)       AS any_terminal,
           COALESCE(MIN(f.telephony_failed), 0)  AS all_telephony_failed,
           COALESCE(MAX(d.is_success), 0)        AS ever_success
    FROM leads l
    LEFT JOIN t_call_facts f ON f.lead_id = l.lead_id
    LEFT JOIN dispositions d ON d.workflow_id = f.workflow_id AND d.code = f.disposition
    GROUP BY l.lead_id
)
SELECT a.*,
       CASE WHEN a.total_calls = 0          THEN 'yet_to_reach_out'
            WHEN a.any_terminal = 1         THEN 'completed'
            WHEN a.all_telephony_failed = 1 THEN 'in_progress_telephony_failed'
            ELSE 'in_progress_dialed' END AS bucket,
       COALESCE(strftime('%Y-%m-%dT%H:%M:%S', a.last_call_utc, '+330 minutes') || '+05:30', '')
           AS last_call_at,
       COALESCE((SELECT f.disposition FROM t_call_facts f
                 WHERE f.lead_id = a.lead_id AND f.disposition IS NOT NULL
                 ORDER BY f.initiated_at DESC, f.call_id DESC LIMIT 1), '') AS current_disposition
FROM agg a;
"""

BATCH_SUMMARY_SQL = """
SELECT b.workflow_id, b.batch_id,
       COUNT(r.lead_id),
       COALESCE(SUM(r.bucket = 'yet_to_reach_out'), 0),
       COALESCE(SUM(r.bucket = 'in_progress_telephony_failed'), 0),
       COALESCE(SUM(r.bucket = 'in_progress_dialed'), 0),
       COALESCE(SUM(r.bucket = 'completed'), 0)
FROM source_files b
LEFT JOIN t_lead_report r ON r.workflow_id = b.workflow_id AND r.batch_id = b.batch_id
WHERE b.kind = 'leads'
GROUP BY b.workflow_id, b.batch_id
ORDER BY b.workflow_id, b.batch_id
"""

DAILY_OVERVIEW_SQL = """
SELECT f.workflow_id, f.ist_date, COUNT(*), SUM(f.connected),
       COUNT(DISTINCT CASE WHEN d.is_success = 1 THEN f.lead_id END)
FROM t_call_facts f
LEFT JOIN dispositions d ON d.workflow_id = f.workflow_id AND d.code = f.disposition
GROUP BY f.workflow_id, f.ist_date
ORDER BY f.workflow_id, f.ist_date
"""

LEAD_REPORT_SQL = """
SELECT workflow_id, external_ref, batch_id, '+91******' || phone_last4, bucket, total_calls,
       connected_calls, last_call_at, current_disposition,
       CASE WHEN ever_success = 1 THEN 'true' ELSE 'false' END
FROM t_lead_report
ORDER BY workflow_id, external_ref, lead_id
"""

REJECTIONS_SQL = """
SELECT workflow_id, batch_id, row_number, reason
FROM lead_rejections
ORDER BY workflow_id, batch_id, row_number
"""

ORPHANS_SQL = "SELECT COUNT(*) FROM t_link WHERE lead_id IS NULL"

UNKNOWN_DISPOSITIONS_SQL = """
SELECT COUNT(*) FROM t_link k
WHERE k.lead_id IS NOT NULL AND k.event_type LIKE 'disposition.%'
  AND NOT EXISTS (SELECT 1 FROM dispositions d
                  WHERE d.workflow_id = k.workflow_id AND d.code = k.disposition_code)
"""


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def data_quality(conn: sqlite3.Connection) -> dict:
    """Needs the temp tables from DERIVE_SQL."""
    leads = conn.execute(
        "SELECT COALESCE(SUM(rows_total), 0), COALESCE(SUM(rows_rejected), 0), "
        "COALESCE(SUM(duplicates_merged), 0), COALESCE(SUM(field_warnings), 0) "
        "FROM source_files WHERE kind = 'leads'").fetchone()
    events = conn.execute(
        "SELECT COALESCE(SUM(lines_total), 0), COALESCE(SUM(lines_malformed), 0), "
        "COALESCE(SUM(lines_duplicate), 0) FROM source_files WHERE kind = 'events'").fetchone()
    by_reason = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM lead_rejections GROUP BY reason ORDER BY reason"))
    return {
        "lead_rows_total": leads[0],
        "lead_rows_rejected": leads[1],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": leads[2],
        "field_warnings": leads[3],
        "events_total": events[0],
        "malformed_events": events[1],
        "duplicate_events_dropped": events[2],
        "orphan_events": conn.execute(ORPHANS_SQL).fetchone()[0],
        "unknown_disposition_events": conn.execute(UNKNOWN_DISPOSITIONS_SQL).fetchone()[0],
    }


def write_reports(conn: sqlite3.Connection, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    # the transaction opened here stays open until the COMMIT below, so all five files
    # come from one snapshot
    conn.executescript("BEGIN;" + DERIVE_SQL)
    _write_csv(out_dir / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                "in_progress_telephony_failed", "in_progress_dialed", "completed"],
               conn.execute(BATCH_SUMMARY_SQL))
    _write_csv(out_dir / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                "success_leads"],
               ((wf, day, total, connected, f"{connected / total:.4f}", success)
                for wf, day, total, connected, success in conn.execute(DAILY_OVERVIEW_SQL)))
    _write_csv(out_dir / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
                "total_calls", "connected_calls", "last_call_at", "current_disposition",
                "ever_success"],
               conn.execute(LEAD_REPORT_SQL))
    _write_csv(out_dir / "rejections.csv",
               ["workflow_id", "batch_id", "row_number", "reason"],
               conn.execute(REJECTIONS_SQL))
    quality = data_quality(conn)
    (out_dir / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n",
                                               encoding="utf-8")
    return quality
