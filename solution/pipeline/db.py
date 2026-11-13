"""SQLite schema and connection handling."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .pii import PiiVault

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per ingested file. Written in the same transaction as the file's rows, so a file
-- can't be processed twice. For lead files the name without extension is the batch_id.
CREATE TABLE IF NOT EXISTS source_files (
    file_id            INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id        TEXT NOT NULL DEFAULT '',      -- '' for event files
    name               TEXT NOT NULL,
    batch_id           TEXT,                          -- lead files only
    sha256             TEXT NOT NULL,
    drop_name          TEXT NOT NULL,
    ingested_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- lead-file counters
    rows_total         INTEGER NOT NULL DEFAULT 0,    -- non-blank data rows
    rows_rejected      INTEGER NOT NULL DEFAULT 0,
    duplicates_merged  INTEGER NOT NULL DEFAULT 0,
    field_warnings     INTEGER NOT NULL DEFAULT 0,
    -- event-file counters
    lines_total        INTEGER NOT NULL DEFAULT 0,    -- non-empty lines
    lines_malformed    INTEGER NOT NULL DEFAULT 0,
    lines_duplicate    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, workflow_id, name)
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code        TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
) WITHOUT ROWID;

-- Identity is (workflow_id, phone). The phone is stored only as a keyed hash, its last
-- four digits, and inside the encrypted blob.
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_token   BLOB NOT NULL,                      -- keyed hash (HMAC) of the canonical phone
    phone_last4   TEXT NOT NULL CHECK (length(phone_last4) = 4),
    external_ref  TEXT NOT NULL,
    batch_id      TEXT NOT NULL,                      -- batch of the first valid row; never changes
    fields_json   TEXT NOT NULL,                      -- normalised non-PII fields, nulls omitted
    pii_encrypted BLOB NOT NULL,                      -- Fernet(JSON of the PII fields)
    UNIQUE (workflow_id, phone_token)
);

CREATE TABLE IF NOT EXISTS lead_rejections (
    file_id     INTEGER NOT NULL REFERENCES source_files(file_id),
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,                        -- never contains cell values
    PRIMARY KEY (file_id, row_number)
) WITHOUT ROWID;

-- The ledger. Append-only (see triggers). Orphans, effective dispositions etc. are not
-- stored here, they are worked out in the report.
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY,             -- arrival order; rows are only ever appended
    event_id         TEXT NOT NULL UNIQUE,            -- dedupe key
    event_type       TEXT,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_utc  TEXT,                            -- 'YYYY-MM-DD HH:MM:SS' UTC; NULL if unparseable
    phone_token      BLOB,                            -- NULL if the phone is absent or invalid
    disposition_code TEXT,                            -- payload.disposition_code, if any
    payload          TEXT NOT NULL,                   -- payload JSON as received
    file_id          INTEGER NOT NULL REFERENCES source_files(file_id),
    line_no          INTEGER NOT NULL
);

-- No other indexes on purpose: they slowed inserts a lot, and the report builds its own.

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

-- Bad JSON or no event_id. Only the position is kept since the raw line may hold a phone.
CREATE TABLE IF NOT EXISTS malformed_event_lines (
    file_id INTEGER NOT NULL REFERENCES source_files(file_id),
    line_no INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    PRIMARY KEY (file_id, line_no)
) WITHOUT ROWID;
"""


def connect(db_path: Path, vault: PiiVault) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit mode: ingest.py starts and ends one transaction per file itself
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -262144")       # 256 MB, the default was too slow
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.executescript(DDL)

    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('pii_key_fingerprint', ?)", (vault.fingerprint,))
    elif row[0] != vault.fingerprint:
        raise SystemExit("error: this database was created with a different PII key; "
                         "phone matching would silently fail. Refusing to continue.")
    return conn


def sync_dispositions(conn: sqlite3.Connection, rows: list[tuple[str, str, int, int]]) -> None:
    """Copy dispositions.csv into the database so the report SQL can join on it."""
    conn.execute("BEGIN")
    conn.execute("DELETE FROM dispositions")
    conn.executemany("INSERT INTO dispositions VALUES (?, ?, ?, ?)", rows)
    conn.execute("COMMIT")
