"""Loads lead files: header mapping, validation, rejection and merging."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field

from .normalize import normalize_header, parse_value
from .pii import PiiVault
from .schema import WorkflowSchema


@dataclass
class RowResult:
    values: dict[str, str | None]      # field name -> normalised value (None = missing/invalid)
    reject_reason: str | None = None
    warnings: int = 0


@dataclass
class LeadFileStats:
    rows_total: int = 0
    rows_rejected: int = 0
    duplicates_merged: int = 0
    field_warnings: int = 0
    rejections: list[tuple[int, str]] = field(default_factory=list)   # (row_number, reason)


def map_headers(header_row: list[str], schema: WorkflowSchema) -> dict[str, int]:
    """Field name -> column index. Unknown columns are ignored, left-most match wins."""
    normalised = [normalize_header(h) for h in header_row]
    mapping = {}
    for f in schema.fields:
        for idx, h in enumerate(normalised):
            if h in f.header_keys:
                mapping[f.name] = idx
                break
    return mapping


def validate_row(cells: list[str], columns: dict[str, int], schema: WorkflowSchema) -> RowResult:
    """Validate one row. The first failure in schema order is the rejection reason.
    Warnings only count if the row is kept."""
    values: dict[str, str | None] = {}
    reason = None
    warnings = 0
    for f in schema.fields:
        idx = columns.get(f.name)
        raw = cells[idx].strip() if idx is not None and idx < len(cells) else ""
        value = parse_value(f.type, raw, list(f.values)) if raw else None
        values[f.name] = value
        if value is not None or reason is not None:
            continue
        if not raw:
            if f.required:
                reason = f"missing_required:{f.name}"
        elif f.required:
            # spec only names the phone case, other fields get a similar reason
            reason = "invalid_phone" if f.type == "phone" else f"invalid_required:{f.name}"
        else:
            warnings += 1
    if reason:
        return RowResult(values, reject_reason=reason)
    return RowResult(values, warnings=warnings)


def read_rows(text: str):
    """Return (header, non-blank rows). Blank rows are dropped so they get no row number."""
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        return [], iter(())
    return header, (row for row in reader if any(c.strip() for c in row))


def ingest_lead_file(conn: sqlite3.Connection, vault: PiiVault, schema: WorkflowSchema,
                     file_id: int, batch_id: str, text: str) -> LeadFileStats:
    """Load one lead file. The caller handles the transaction."""
    stats = LeadFileStats()
    header, rows = read_rows(text)
    columns = map_headers(header, schema)
    wf = schema.workflow_id
    pii_fields = {f.name for f in schema.fields if f.pii}
    # phone token -> lead_id, kept in memory so new leads don't need a SELECT
    known: dict[bytes, int] = dict(conn.execute(
        "SELECT phone_token, lead_id FROM leads WHERE workflow_id = ?", (wf,)))

    for row_number, cells in enumerate(rows, start=1):
        stats.rows_total += 1
        result = validate_row(cells, columns, schema)
        if result.reject_reason:
            stats.rows_rejected += 1
            stats.rejections.append((row_number, result.reject_reason))
            continue
        stats.field_warnings += result.warnings

        present = {k: v for k, v in result.values.items() if v is not None}
        phone = present[schema.phone_field]
        token = vault.phone_token(phone)
        lead_id = known.get(token)

        if lead_id is None:
            plain, secret = {}, {}
        else:
            # merge: non-null new values overwrite, batch_id stays
            stats.duplicates_merged += 1
            fields_json, blob = conn.execute(
                "SELECT fields_json, pii_encrypted FROM leads WHERE lead_id = ?",
                (lead_id,)).fetchone()
            plain, secret = json.loads(fields_json), vault.decrypt(blob)
        for name, value in present.items():
            (secret if name in pii_fields else plain)[name] = value

        external_ref = {**plain, **secret}.get(schema.reference_field, "")
        params = (external_ref, json.dumps(plain, ensure_ascii=False, sort_keys=True),
                  vault.encrypt(secret))
        if lead_id is None:
            known[token] = conn.execute(
                "INSERT INTO leads (workflow_id, phone_token, phone_last4, batch_id, "
                "external_ref, fields_json, pii_encrypted) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wf, token, phone[-4:], batch_id, *params)).lastrowid
        else:
            conn.execute("UPDATE leads SET external_ref = ?, fields_json = ?, pii_encrypted = ? "
                         "WHERE lead_id = ?", (*params, lead_id))

    conn.executemany(
        "INSERT INTO lead_rejections (file_id, workflow_id, batch_id, row_number, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        [(file_id, wf, batch_id, n, reason) for n, reason in stats.rejections])
    return stats


def resolved_fields(conn: sqlite3.Connection, vault: PiiVault, schema: WorkflowSchema,
                    lead_id: int) -> dict[str, str | None]:
    """Full lead record with defaults filled in (this decrypts PII).

    Defaults are applied on read so a later duplicate can still supply a real value.
    """
    fields_json, blob = conn.execute(
        "SELECT fields_json, pii_encrypted FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    stored = {**json.loads(fields_json), **vault.decrypt(blob)}
    return {f.name: stored.get(f.name, f.default) for f in schema.fields}
