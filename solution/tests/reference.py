"""A second, simple version of the spec in plain Python (in memory, no SQL).

Only used by the tests to cross-check the real reports. It reuses the value parsers from
normalize.py but none of the ingest or report code.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from solution.pipeline.normalize import IST, normalize_header, parse_phone, parse_value


def run(drops: list[Path], schema_dir: Path) -> dict:
    schemas = {}
    for p in schema_dir.glob("*.json"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        schemas[doc["workflow_id"]] = doc["fields"]
    catalogue = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            catalogue[(r["workflow_id"], r["code"])] = (r["is_terminal"] == "true", r["is_success"] == "true")

    q = Counter()
    reasons = Counter()
    rejections = []
    leads = {}            # (workflow, phone) -> batch and ref
    batches = set()

    # ---------- leads ----------
    for drop in drops:
        for path in sorted((drop / "leads").glob("*/*.csv"), key=lambda p: (p.parent.name, p.name)):
            wf, batch = path.parent.name, path.stem
            batches.add((wf, batch))
            fields = schemas[wf]
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            header = [normalize_header(h) for h in rows[0]]
            n = 0
            for cells in rows[1:]:
                if not any(c.strip() for c in cells):
                    continue
                n += 1
                q["lead_rows_total"] += 1
                values, reason, warnings = {}, None, 0
                for fd in fields:
                    keys = {normalize_header(k) for k in [fd["name"], *fd.get("aliases", [])]}
                    col = next((i for i, h in enumerate(header) if h in keys), None)
                    raw = cells[col].strip() if col is not None and col < len(cells) else ""
                    val = parse_value(fd["type"], raw, fd.get("values")) if raw else None
                    values[fd["name"]] = val
                    if val is None and reason is None:
                        if fd.get("required"):
                            reason = "invalid_phone" if raw else f"missing_required:{fd['name']}"
                        elif raw:
                            warnings += 1
                if reason:
                    q["lead_rows_rejected"] += 1
                    reasons[reason] += 1
                    rejections.append((wf, batch, n, reason))
                    continue
                q["field_warnings"] += warnings
                ref_field = next(fd["name"] for fd in fields if fd.get("reference"))
                key = (wf, values["phone"])
                if key in leads:
                    q["duplicate_lead_rows_merged"] += 1
                    leads[key]["ref"] = values[ref_field]
                else:
                    leads[key] = {"batch": batch, "ref": values[ref_field]}

    # ---------- events ----------
    def when(s):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(IST)

    seen, events = set(), []
    for drop in drops:
        for path in sorted((drop / "events").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                q["events_total"] += 1
                try:
                    e = json.loads(line)
                    eid = e["event_id"]
                except (ValueError, KeyError, TypeError):
                    q["malformed_events"] += 1
                    continue
                if eid in seen:
                    q["duplicate_events_dropped"] += 1
                    continue
                seen.add(eid)
                events.append(e)

    call_lead = {}
    for e in events:
        if e["event_type"] != "disposition.override":
            key = (e["workflow_id"], parse_phone(e.get("phone")))
            if key in leads:
                call_lead[e["call_id"]] = key
    calls = defaultdict(lambda: {"types": set(), "sets": [], "overrides": []})
    for e in events:
        if e["event_type"] == "disposition.override":
            key = call_lead.get(e["call_id"])
        else:
            key = (e["workflow_id"], parse_phone(e.get("phone")))
        if key not in leads:
            q["orphan_events"] += 1
            continue
        c = calls[e["call_id"]]
        c["lead"] = key
        c["types"].add(e["event_type"])
        if e["event_type"] == "call.initiated":
            c["at"] = when(e["occurred_at"])
        if e["event_type"].startswith("disposition."):
            code = e["payload"].get("disposition_code")
            if (e["workflow_id"], code) not in catalogue:
                q["unknown_disposition_events"] += 1
                continue
            bucket = "overrides" if e["event_type"] == "disposition.override" else "sets"
            c[bucket].append((when(e["occurred_at"]), e["event_id"], code))

    by_lead = defaultdict(list)
    for call_id, c in calls.items():
        if "at" not in c:
            continue
        ranked = c["overrides"] or c["sets"]
        c["code"] = max(ranked)[2] if ranked else None
        c["flags"] = catalogue.get((c["lead"][0], c["code"]), (False, False))
        c["id"] = call_id
        by_lead[c["lead"]].append(c)

    # ---------- reports ----------
    lead_rows, summary, daily = [], defaultdict(Counter), defaultdict(lambda: [0, 0, set()])
    for (wf, phone), info in leads.items():
        cs = sorted(by_lead[(wf, phone)], key=lambda c: (c["at"], c["id"]))
        if not cs:
            bucket = "yet_to_reach_out"
        elif any(c["flags"][0] for c in cs):
            bucket = "completed"
        elif all("call.telephony_failed" in c["types"] for c in cs):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        with_code = [c["code"] for c in cs if c["code"]]
        success = any(c["flags"][1] for c in cs)
        lead_rows.append({
            "workflow_id": wf, "external_ref": info["ref"], "batch_id": info["batch"],
            "phone_masked": "+91******" + phone[-4:], "bucket": bucket,
            "total_calls": str(len(cs)),
            "connected_calls": str(sum("call.connected" in c["types"] for c in cs)),
            "last_call_at": cs[-1]["at"].isoformat() if cs else "",
            "current_disposition": with_code[-1] if with_code else "",
            "ever_success": "true" if success else "false"})
        summary[(wf, info["batch"])][bucket] += 1
        for c in cs:
            d = daily[(wf, c["at"].date().isoformat())]
            d[0] += 1
            d[1] += "call.connected" in c["types"]
            if c["flags"][1]:
                d[2].add(phone)

    buckets = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    return {
        "lead_report": sorted(lead_rows, key=lambda r: (r["workflow_id"], r["external_ref"])),
        "batch_summary": [
            {"workflow_id": wf, "batch_id": b, "total_leads": str(sum(summary[(wf, b)].values())),
             **{k: str(summary[(wf, b)][k]) for k in buckets}} for wf, b in sorted(batches)],
        "daily_overview": [
            {"workflow_id": wf, "date": day, "total_calls": str(t), "connected_calls": str(c),
             "connectivity_rate": f"{c / t:.4f}", "success_leads": str(len(s))}
            for (wf, day), (t, c, s) in sorted(daily.items())],
        "rejections": [{"workflow_id": wf, "batch_id": b, "row_number": str(n), "reason": r}
                       for wf, b, n, r in sorted(rejections)],
        "data_quality": {
            "lead_rows_total": q["lead_rows_total"], "lead_rows_rejected": q["lead_rows_rejected"],
            "rejections_by_reason": dict(sorted(reasons.items())),
            "duplicate_lead_rows_merged": q["duplicate_lead_rows_merged"],
            "field_warnings": q["field_warnings"], "events_total": q["events_total"],
            "malformed_events": q["malformed_events"],
            "duplicate_events_dropped": q["duplicate_events_dropped"],
            "orphan_events": q["orphan_events"],
            "unknown_disposition_events": q["unknown_disposition_events"]},
    }
