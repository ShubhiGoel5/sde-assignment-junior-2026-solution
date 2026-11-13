"""Finds the files in a drop and loads them, one transaction per file."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from .events import ingest_event_file
from .leads import ingest_lead_file
from .pii import PiiVault
from .schema import WorkflowSchema


@dataclass(frozen=True)
class InputFile:
    kind: str            # 'leads' | 'events'
    workflow_id: str     # '' for events
    path: Path
    drop_name: str


class IngestError(Exception):
    pass


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _is_drop(path: Path) -> bool:
    return (path / "leads").is_dir() or (path / "events").is_dir()


def find_drops(path: Path) -> list[Path]:
    """path is a drop, or a folder of drops (taken in natural name order)."""
    if not path.is_dir():
        raise IngestError(f"not a directory: {path}")
    if _is_drop(path):
        return [path]
    drops = sorted((p for p in path.iterdir() if p.is_dir() and _is_drop(p)),
                   key=lambda p: _natural(p.name))
    if not drops:
        raise IngestError(f"no leads/ or events/ found under {path}")
    return drops


def list_files(drop: Path) -> list[InputFile]:
    """Lead files first, then event files."""
    files = []
    leads_dir = drop / "leads"
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            for f in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                files.append(InputFile("leads", wf_dir.name, f, drop.name))
    events_dir = drop / "events"
    if events_dir.is_dir():
        for f in sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name):
            files.append(InputFile("events", "", f, drop.name))
    return files


def ingest_drop(conn: sqlite3.Connection, vault: PiiVault, schemas: dict[str, WorkflowSchema],
                drop: Path, log=lambda msg: print(msg, file=sys.stderr)) -> dict[str, int]:
    summary = {"ingested": 0, "skipped": 0}
    todo = []

    # Check every file first so a bad one can't leave the drop half loaded.
    for item in list_files(drop):
        if item.kind == "leads" and item.workflow_id not in schemas:
            raise IngestError(f"{item.path}: no schema for workflow '{item.workflow_id}'")
        data = item.path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        seen = conn.execute(
            "SELECT sha256 FROM source_files WHERE kind = ? AND workflow_id = ? AND name = ?",
            (item.kind, item.workflow_id, item.path.name)).fetchone()
        if seen is None:
            todo.append((item, data, digest))
        elif seen[0] == digest:
            summary["skipped"] += 1
        else:
            raise IngestError(
                f"{item.path}: a different file with this name was already ingested. "
                "Ingested files are immutable; deliver corrections under a new file name.")

    for item, data, digest in todo:
        text = data.decode("utf-8-sig", errors="replace")
        batch_id = item.path.stem if item.kind == "leads" else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            file_id = conn.execute(
                "INSERT INTO source_files (kind, workflow_id, name, batch_id, sha256, drop_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item.kind, item.workflow_id, item.path.name, batch_id, digest,
                 item.drop_name)).lastrowid
            if item.kind == "leads":
                s = ingest_lead_file(conn, vault, schemas[item.workflow_id], file_id, batch_id, text)
                conn.execute(
                    "UPDATE source_files SET rows_total = ?, rows_rejected = ?, "
                    "duplicates_merged = ?, field_warnings = ? WHERE file_id = ?",
                    (s.rows_total, s.rows_rejected, s.duplicates_merged, s.field_warnings, file_id))
                log(f"  {item.workflow_id}/{item.path.name}: {s.rows_total} rows, "
                    f"{s.rows_rejected} rejected, {s.duplicates_merged} merged, "
                    f"{s.field_warnings} warnings")
            else:
                s = ingest_event_file(conn, vault, file_id, text)
                conn.execute(
                    "UPDATE source_files SET lines_total = ?, lines_malformed = ?, "
                    "lines_duplicate = ? WHERE file_id = ?",
                    (s.lines_total, s.lines_malformed, s.lines_duplicate, file_id))
                log(f"  {item.path.name}: {s.lines_total} lines, {s.lines_malformed} malformed, "
                    f"{s.lines_duplicate} duplicates")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        summary["ingested"] += 1
    return summary
