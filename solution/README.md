# Solution — Assignment A (Interaction Ledger & Reporting Pipeline)

Run from the repository root:

```bash
pip install -r requirements.txt
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/
pytest
```

Design, decisions and results are in [`../SUBMISSION.md`](../SUBMISSION.md).

## Code map (suggested reading order)

| File | Responsibility |
|---|---|
| `pipeline/normalize.py` | Pure value rules: header, phone, date, decimal, enum, timestamp. No I/O. |
| `pipeline/schema.py` | Loads workflow schemas and the disposition catalogue. |
| `pipeline/pii.py` | Phone blind index (HMAC) and field encryption (Fernet); key loading. |
| `pipeline/db.py` | SQLite DDL (tables, append-only triggers) and connection setup. |
| `pipeline/leads.py` | One lead file → validate rows, reject, insert/merge leads. |
| `pipeline/events.py` | One event file → parse lines, append to the ledger, dedupe. |
| `pipeline/ingest.py` | Finds drops and files; one transaction per file; skip files already ingested. |
| `pipeline/report.py` | Derives everything in SQL and writes the five report files. |
| `pipeline/cli.py` | `ingest` / `report` commands. |
| `tests/reference.py` | Naive second implementation used only to cross-check the reports. |
