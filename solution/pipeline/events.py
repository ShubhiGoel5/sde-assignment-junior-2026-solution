"""Loads event files into the ledger.

Only malformed and duplicate lines are decided here. Orphans and unknown dispositions
depend on which leads exist, so the report works those out.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .normalize import parse_phone, parse_timestamp, to_db_utc
from .pii import PiiVault


@dataclass
class EventFileStats:
    lines_total: int = 0
    lines_malformed: int = 0
    lines_duplicate: int = 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_event_line(line: str, vault: PiiVault) -> tuple | str:
    """Ledger row for a line, or a reason string if the line is malformed."""
    try:
        event = json.loads(line)
    except ValueError:
        return "invalid_json"
    if not isinstance(event, dict):
        return "not_an_object"
    event_id = _text(event.get("event_id"))
    if event_id is None:
        return "missing_event_id"

    occurred = parse_timestamp(event.get("occurred_at"))
    phone = parse_phone(event.get("phone"))
    payload = event.get("payload")
    code = _text(payload.get("disposition_code")) if isinstance(payload, dict) else None
    return (
        event_id,
        _text(event.get("event_type")),
        _text(event.get("workflow_id")),
        _text(event.get("call_id")),
        to_db_utc(occurred) if occurred else None,
        vault.phone_token(phone) if phone else None,     # raw phone is not stored
        code,
        json.dumps(payload if payload is not None else {}, ensure_ascii=False),
    )


def ingest_event_file(conn: sqlite3.Connection, vault: PiiVault, file_id: int,
                      text: str) -> EventFileStats:
    """Add one JSONL file to the ledger. The caller handles the transaction."""
    stats = EventFileStats()
    rows = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        stats.lines_total += 1
        row = parse_event_line(line, vault)
        if isinstance(row, str):
            stats.lines_malformed += 1
            conn.execute("INSERT INTO malformed_event_lines VALUES (?, ?, ?)",
                         (file_id, line_no, row))
            continue
        rows.append((*row, file_id, line_no))

    # The unique event_id drops duplicates (first copy wins). Ignored rows = duplicates.
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, "
        "occurred_at_utc, phone_token, disposition_code, payload, file_id, line_no) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    stats.lines_duplicate = len(rows) - (conn.total_changes - before)
    return stats
