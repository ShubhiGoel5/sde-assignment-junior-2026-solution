# Submission

## Candidate
- **Name:** TODO: your name
- **Assignment chosen:** A (Data)
- **Time spent:** TODO: your honest estimate

## How to run
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/

pytest
```

Notes:
- Python 3.11+, SQLite. The database is `ledger.sqlite` in the current folder (change it with `--db`).
- `ingest` takes several drops, or a folder of drops, so `ingest data` loads drop_1 then drop_2.
- Set `AU_PII_KEY` to any secret string. If it isn't set, a key is generated once into `ledger.sqlite.key` (git-ignored) so a fresh clone still runs.

## Design

The main idea is to store only things that never change, and work out everything else when the report runs.

Things that never change: an event arrived, a lead row was rejected, a lead's fields are what they are right now. Things that can change: whether an event is an orphan, which disposition is the effective one, which bucket a lead is in. I don't store those. `report` recomputes them from the full ledger each time. This is what makes the "event arrives before its lead" case work: once the lead shows up, the event stops being an orphan with no repair step. It also means loading drop by drop gives the same result as loading everything at once.

Tables (in `solution/pipeline/db.py`):
- `source_files`: one row per ingested file, with its name, hash and counters. This is what makes ingest idempotent.
- `leads`: one row per lead, unique on (workflow, phone fingerprint). `batch_id` is set once and never changes.
- `lead_rejections`: file, row number and reason. No cell values.
- `events`: the ledger. `event_id` is unique, which handles dedupe. Triggers block UPDATE and DELETE.
- `malformed_event_lines`: position and reason only, since the raw line might contain a phone.
- `dispositions`: a copy of `dispositions.csv` so SQL can join against it.

PII: a phone number is needed for two different jobs, so I treat it two ways. For matching, I store an HMAC-SHA256 of the canonical phone (a keyed hash, truncated to 128 bits). The same phone always gives the same value, so leads and events join on it, but you can't brute-force it without the key like you could with a plain hash. For reading the value back, the PII fields are encrypted with Fernet. `phone_last4` is stored in clear because the report prints it. Events keep only the fingerprint. The key's fingerprint is saved in the database so running with the wrong key stops with an error instead of quietly matching nothing.

Idempotency: each file is loaded in one transaction together with its `source_files` row. A second run sees the same name and hash and skips it. A crash mid-file rolls that file back.

Reports: four temp tables built in SQL, each following one rule from the spec (link events to leads, find countable calls, work out per-call facts, roll up per lead). The five files are plain SELECTs over those, read inside one transaction.

The cost of this approach is that report time grows with total history. It's 12 seconds for about 630k events, which I think is fine for now.

## Decisions and assumptions

| Decision | Why | Alternative |
|---|---|---|
| Orphan and unknown-disposition status is worked out at report time | They change when a lead arrives later | Flag at ingest and re-link later |
| Same file name with different content: refuse the whole ingest before writing anything | Re-applying a changed batch would double-count merges | Treat it as a new file |
| "Malformed" means only bad JSON or no `event_id`. An event with an id but a bad timestamp is kept with NULLs | That's the spec's definition. Such an event can't make a call countable | Count it as malformed |
| Disposition codes are matched case-sensitively | Typos like `RENEWD` are meant to be unknown | Case-insensitive |
| An override with an unknown code is ignored, so the previous override or the `set` applies | Spec says count it and ignore it | Treat the call as having no disposition |
| Ties on timestamp: higher `event_id` wins (dispositions), higher `call_id` wins (calls) | Needs to be deterministic | Arrival order |
| A call's lead comes from its `call.initiated`, else any other event of the call that has a phone. Overrides follow that | Spec: link through the call's other events | Require all events to agree |
| Connected, failed and disposition events only count if they link to the same lead as the call | Facts should come from that lead's events | Match on `call_id` alone |
| `batch_summary` lists every batch, even with 0 leads | A batch where every row was a duplicate shouldn't disappear | Only batches that own a lead |
| `rejections_by_reason` only includes reasons that occurred | There's no fixed list across workflows | Zeros for everything |
| A required non-phone field that is present but invalid gets `invalid_required:<field>` | Spec only covers phone. This can't happen with the given schemas | Reuse `missing_required` |
| If two columns match the same field, the left-most wins | Deterministic | Left-most non-empty |
| A blank row means every cell is blank, including unmapped columns | Spec says every cell | Mapped columns only |
| Amounts: commas are just removed, rounding is half-up | Indian digit grouping isn't 3-3-3 | Western grouping only |
| Defaults are applied when reading, nulls are stored | Spec says defaults apply after merging, so a later duplicate can still fill the field | Write the default at insert |
| SQLite rather than Postgres | No setup for the reviewer, one writer is enough | Postgres |

## Failure modes

Handled:
- Re-running a drop: skipped per file. Tested byte for byte on the reports.
- Crash during ingest: the file's transaction rolls back, and the next run redoes only that file.
- Events before their lead, events out of order, event files in any order.
- Duplicate events within a file and across drops. The first copy wins.
- Broken JSON lines, missing `event_id`, non-object JSON, bad or oddly formatted phones, all three timestamp forms.
- Renamed, reordered or extra columns, blank rows, a BOM, short rows.
- Wrong PII key: refused. Changed file under an old name: refused with nothing written.
- Someone running UPDATE or DELETE on the ledger: blocked by a trigger.

Not handled:
- Two `ingest` processes at the same time. SQLite serialises them, but the order of lead batches would then depend on timing.
- Key rotation. That would need a re-keying job.
- A duplicate `event_id` with different content is dropped silently. I'd want a log line or alert for that.
- Events that have an id but no `call_id` or timestamp are stored but not counted in any metric.
- `payload` is stored as received, so PII inside a payload would be in clear.
- Files are read fully into memory. Fine at 5,000 lines each.

## Testing

`pytest` runs 90 tests:
- `test_normalize.py`: phone, date, amount, enum, header and timestamp rules, valid and invalid cases.
- `test_leads.py`: rejection order, row numbering with blank rows, warnings, alias headers, merging, `batch_id` staying fixed, defaults, same phone in two workflows.
- `test_derived.py`: hand-built cases for effective disposition, the four buckets, a call with no `initiated`, the IST date boundary, orphans/malformed/duplicates, and an orphan becoming linked in a later drop.
- `test_pipeline.py`: runs on generated data for seeds 42, 7 and 2026. It checks that all five reports match a separate simple implementation (`tests/reference.py`, plain Python, no SQL), that drop-by-drop equals all-at-once, that re-ingest changes nothing, that the database file contains no names or phones, and that the ledger is append-only.

Since no expected output is provided, the reference implementation is my main correctness check. It shares my reading of the spec, though, so it would catch coding bugs but not a misread rule.

Not tested: concurrent ingest, crashes injected mid-transaction (I rely on SQLite for that), and the large dataset (timed by hand).

## Results

Seed 42, small: ingest 0.5s and 0.3s, report 0.2s.

```json
{"lead_rows_total": 2057, "lead_rows_rejected": 108,
 "rejections_by_reason": {"invalid_phone": 42, "missing_required:customer_name": 25,
   "missing_required:order_id": 13, "missing_required:phone": 14,
   "missing_required:policy_number": 14},
 "duplicate_lead_rows_merged": 57, "field_warnings": 122, "events_total": 8745,
 "malformed_events": 8, "duplicate_events_dropped": 237, "orphan_events": 462,
 "unknown_disposition_events": 18}
```

Seed 42, large (143,990 lead rows, 632,602 event lines), on a Windows laptop with Python 3.14:

| Step | Time |
|---|---|
| ingest drop_1 | 38s |
| ingest drop_2 | 40s |
| report | 12s |
| re-ingest of both (all skipped) | 0.6s |

About 90 seconds in total. My first version took around 7 minutes. Profiling showed the problem was how the ledger was stored: it was ordered by `event_id` with two extra indexes, so every file rewrote pages all over a growing table. Storing rows in arrival order, keeping one unique index, using a bigger page cache and inserting in batches fixed it. Journal mode and disk location made no difference.

Things I noticed in the data: about 5% of events are orphans permanently (calls to rejected or never-uploaded numbers), overrides can land in a later drop than their call, and the same duplicate event can show up in both drops.

## If I had another day
1. Add metrics for events that are stored but unusable, and alert on duplicate ids with different content.
2. Make reports incremental by keeping the per-call table and only recomputing what new files touched.
3. Move to Postgres for concurrent writers, with the ledger partitioned by month.
4. Key rotation, and keep the key in a secrets manager.
5. For 100k leads a day: land drops in object storage, have a scheduler run `ingest` per drop and then `report` (safe to retry because of the per-file idempotency), do backfills by ingesting older drops in order into a fresh database, and alert on rejection, orphan and malformed rates per drop.
