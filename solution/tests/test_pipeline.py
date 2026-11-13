"""End-to-end tests on generated data: match the reference, idempotency, incremental ==
all at once, append-only ledger, no plaintext PII."""
import csv
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from solution.pipeline.cli import main
from solution.pipeline.schema import DEFAULT_SCHEMA_DIR

from . import reference
from .conftest import lead, read_reports

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"
REPORT_FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
                "data_quality.json"]


@pytest.fixture(scope="module", params=[42, 7, 2026])
def data(request, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp(f"seed{request.param}")
    subprocess.run([sys.executable, str(GENERATOR), "--seed", str(request.param),
                    "--scale", "small", "--out", str(out)], check=True, capture_output=True)
    return out


def run(db: Path, *args: str) -> None:
    assert main([*args, "--db", str(db)]) == 0


def report_bytes(db: Path, out: Path) -> dict[str, bytes]:
    run(db, "report", "--out", str(out))
    return {name: (out / name).read_bytes() for name in REPORT_FILES}


@pytest.fixture(autouse=True)
def pii_key(monkeypatch):
    monkeypatch.setenv("AU_PII_KEY", "test-only-secret")


def test_reports_match_the_independent_reference_implementation(data, tmp_path):
    db = tmp_path / "a.sqlite"
    run(db, "ingest", str(data / "drop_1"))
    run(db, "ingest", str(data / "drop_2"))
    run(db, "report", "--out", str(tmp_path / "out"))
    actual = read_reports(tmp_path / "out")
    expected = reference.run([data / "drop_1", data / "drop_2"], DEFAULT_SCHEMA_DIR)
    for name in expected:
        assert actual[name] == expected[name], name


def test_incremental_equals_all_at_once_and_reingest_changes_nothing(data, tmp_path):
    step = tmp_path / "step.sqlite"
    run(step, "ingest", str(data / "drop_1"))
    after_drop_1 = report_bytes(step, tmp_path / "r1")
    run(step, "ingest", str(data / "drop_1"))                       # re-run, no change
    assert report_bytes(step, tmp_path / "r1b") == after_drop_1
    run(step, "ingest", str(data / "drop_2"))
    stepwise = report_bytes(step, tmp_path / "r2")
    assert stepwise != after_drop_1

    once = tmp_path / "once.sqlite"
    run(once, "ingest", str(data))                                  # folder with both drops
    assert report_bytes(once, tmp_path / "r3") == stepwise

    # re-ingesting everything in any order changes nothing
    conn = sqlite3.connect(step)
    counts = lambda: [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("leads", "events", "lead_rejections", "source_files")]
    before = counts()
    run(step, "ingest", str(data / "drop_2"), str(data / "drop_1"))
    assert counts() == before
    conn.close()
    assert report_bytes(step, tmp_path / "r4") == stepwise


def test_event_files_can_arrive_in_any_order(data, tmp_path):
    """Loading events before their leads gives the same reports. Only events are moved,
    since lead batch order matters by spec."""
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    run(a, "ingest", str(data))
    late = tmp_path / "events_first" / "events"
    late.mkdir(parents=True)
    for f in sorted(data.glob("drop_*/events/*.jsonl"), reverse=True):
        (late / f.name).write_bytes(f.read_bytes())
    run(b, "ingest", str(late.parent))      # every event is an orphan here
    run(b, "ingest", str(data))
    assert report_bytes(a, tmp_path / "ra") == report_bytes(b, tmp_path / "rb")


def test_no_plaintext_pii_in_the_database_file(data, tmp_path):
    db = tmp_path / "a.sqlite"
    run(db, "ingest", str(data))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    blob = db.read_bytes()

    names, phones = set(), set()
    with open(data / "drop_1" / "leads" / "motor_renewal" / "batch_001.csv", newline="",
              encoding="utf-8") as f:
        for row in csv.DictReader(f):
            names.add(row["customer_name"].strip())
            digits = re.sub(r"\D", "", row["phone"])
            if len(digits) >= 10:
                phones.add(digits[-10:])
    with open(next((data / "drop_1" / "events").glob("*.jsonl")), encoding="utf-8") as f:
        for line in list(f)[:300]:
            try:
                digits = re.sub(r"\D", "", json.loads(line).get("phone", ""))
            except ValueError:
                continue
            if len(digits) >= 10:
                phones.add(digits[-10:])
    names.discard("")
    assert len(names) > 50 and len(phones) > 300
    assert not [n for n in names if n.encode() in blob]
    assert not [p for p in phones if p.encode() in blob]


def test_ledger_is_append_only(pipe):
    pipe.leads("d1", "batch_001", [lead()])
    pipe.events("d1", "events_0001", [pipe.event("call.initiated", "c1", "2026-10-03 10:00:00")])
    pipe.ingest("d1")
    conn = sqlite3.connect(pipe.db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET disposition_code = 'renewed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()


def test_changed_file_under_an_ingested_name_is_refused_without_side_effects(pipe):
    pipe.leads("d1", "batch_001", [lead(phone="9000000001", ref="A")])
    assert pipe.ingest("d1") == 0
    before = pipe.report()
    pipe.leads("d1", "batch_001", [lead(phone="9000000002", ref="B")])
    pipe.leads("d1", "batch_000", [lead(phone="9000000003", ref="C")])   # sorts first, must not load
    assert pipe.ingest("d1") == 1
    assert pipe.report() == before


def test_wrong_pii_key_is_refused(pipe, monkeypatch):
    pipe.leads("d1", "batch_001", [lead()])
    pipe.ingest("d1")
    monkeypatch.setenv("AU_PII_KEY", "some-other-secret")
    with pytest.raises(SystemExit, match="different PII key"):
        pipe.ingest("d1")
