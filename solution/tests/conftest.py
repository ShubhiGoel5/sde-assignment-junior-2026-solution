"""Test helpers: write small drops, run the pipeline, read the reports."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from solution.pipeline.cli import main

MOTOR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount",
                "preferred_language"]


class Pipeline:
    """Temp database plus helpers to write drops into a temp folder."""

    def __init__(self, root: Path):
        self.root = root
        self.db = root / "ledger.sqlite"
        self.out = root / "reports"
        self._eid = 0

    # ---- building input ----
    def leads(self, drop: str, batch: str, rows: list[list[str]], header=MOTOR_HEADER,
              workflow: str = "motor_renewal") -> None:
        path = self.root / drop / "leads" / workflow / f"{batch}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([header, *rows])

    def event(self, etype: str, call: str, at: str, phone: str | None = "9876543210",
              workflow: str = "motor_renewal", **payload) -> dict:
        self._eid += 1
        e = {"event_id": f"evt_{self._eid:04d}", "event_type": etype,
             "workflow_id": workflow, "call_id": call, "occurred_at": at, "payload": payload}
        if phone is not None:
            e["phone"] = phone
        return e

    def events(self, drop: str, name: str, events: list) -> None:
        path = self.root / drop / "events" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- running ----
    def ingest(self, *drops: str) -> int:
        return main(["ingest", *[str(self.root / d) for d in drops], "--db", str(self.db)])

    def report(self) -> dict:
        assert main(["report", "--db", str(self.db), "--out", str(self.out)]) == 0
        return read_reports(self.out)


def read_reports(out: Path) -> dict:
    result = {"data_quality": json.loads((out / "data_quality.json").read_text(encoding="utf-8"))}
    for name in ["batch_summary", "daily_overview", "lead_report", "rejections"]:
        with open(out / f"{name}.csv", newline="", encoding="utf-8") as f:
            result[name] = list(csv.DictReader(f))
    return result


@pytest.fixture
def pipe(tmp_path, monkeypatch) -> Pipeline:
    monkeypatch.setenv("AU_PII_KEY", "test-only-secret")
    return Pipeline(tmp_path)


def lead(name="Asha Rao", phone="9876543210", ref="MOT/1", expiry="", premium="", lang=""):
    return [name, phone, ref, expiry, premium, lang]
