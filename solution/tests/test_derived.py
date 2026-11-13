"""Small hand-made cases for linking, effective disposition and buckets."""
from .conftest import lead

P1, P2, P3, P4 = "9000000001", "9000000002", "9000000003", "9000000004"


def by_ref(report):
    return {r["external_ref"]: r for r in report["lead_report"]}


def test_effective_disposition_latest_override_beats_any_set(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A")])
    pipe.events("d1", "events_0001", [
        pipe.event("call.initiated", "c1", "2026-10-03 10:00:00", P1),
        pipe.event("call.connected", "c1", "2026-10-03 10:00:10", P1),
        pipe.event("disposition.set", "c1", "2026-10-03 10:05:00", P1, disposition_code="callback_requested"),
        # overrides arrive out of order and have no phone, the later one wins
        pipe.event("disposition.override", "c1", "2026-10-05 09:00:00", None, disposition_code="renewed"),
        pipe.event("disposition.override", "c1", "2026-10-04 09:00:00", None, disposition_code="not_interested"),
        # a later set still loses to an override
        pipe.event("disposition.set", "c1", "2026-10-06 09:00:00", P1, disposition_code="dnd"),
    ])
    pipe.ingest("d1")
    r = pipe.report()
    row = by_ref(r)["A"]
    assert row["current_disposition"] == "renewed"
    assert (row["bucket"], row["ever_success"]) == ("completed", "true")
    assert r["data_quality"]["orphan_events"] == 0


def test_latest_set_wins_without_override_and_unknown_codes_are_ignored(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A")])
    pipe.events("d1", "events_0001", [
        pipe.event("call.initiated", "c1", "2026-10-03T10:00:00+05:30", P1),
        pipe.event("disposition.set", "c1", "2026-10-03T10:05:00+05:30", P1, disposition_code="renewed"),
        pipe.event("disposition.set", "c1", "2026-10-03T10:06:00+05:30", P1, disposition_code="payment_link_sent"),
        pipe.event("disposition.set", "c1", "2026-10-03T10:07:00+05:30", P1, disposition_code="RENEWD"),
        # valid for policy_feedback, not for this workflow
        pipe.event("disposition.override", "c1", "2026-10-03T10:08:00+05:30", None, disposition_code="refused"),
    ])
    pipe.ingest("d1")
    r = pipe.report()
    row = by_ref(r)["A"]
    assert row["current_disposition"] == "payment_link_sent"
    assert (row["bucket"], row["ever_success"]) == ("in_progress_dialed", "false")
    assert r["data_quality"]["unknown_disposition_events"] == 2


def test_buckets(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A"), lead(phone=P2, ref="B"),
                                  lead(phone=P3, ref="C"), lead(phone=P4, ref="D")])
    t = "2026-10-03 10:00:00"
    pipe.events("d1", "events_0001", [
        # B: every call failed at telephony
        pipe.event("call.initiated", "b1", t, P2),
        pipe.event("call.telephony_failed", "b1", t, P2, reason="sip_503"),
        pipe.event("call.initiated", "b2", "2026-10-04 10:00:00", P2),
        pipe.event("call.telephony_failed", "b2", "2026-10-04 10:00:01", P2, reason="sip_503"),
        # C: one failed, one rang out -> dialed
        pipe.event("call.initiated", "c1", t, P3),
        pipe.event("call.telephony_failed", "c1", t, P3, reason="sip_503"),
        pipe.event("call.initiated", "c2", "2026-10-04 10:00:00", P3),
        pipe.event("call.ended", "c2", "2026-10-04 10:00:30", P3, duration_sec=0, end_reason="no_answer"),
        # D: early terminal call, later telephony failure -> still completed
        pipe.event("call.initiated", "d1", t, P4),
        pipe.event("call.connected", "d1", t, P4),
        pipe.event("disposition.set", "d1", t, P4, disposition_code="not_interested"),
        pipe.event("call.initiated", "d2", "2026-10-05 10:00:00", P4),
        pipe.event("call.telephony_failed", "d2", "2026-10-05 10:00:01", P4, reason="sip_503"),
    ])
    pipe.ingest("d1")
    r = pipe.report()
    rows = by_ref(r)
    assert {k: v["bucket"] for k, v in rows.items()} == {
        "A": "yet_to_reach_out", "B": "in_progress_telephony_failed",
        "C": "in_progress_dialed", "D": "completed"}
    assert (rows["D"]["total_calls"], rows["D"]["connected_calls"]) == ("2", "1")
    assert rows["D"]["last_call_at"] == "2026-10-05T10:00:00+05:30"
    assert rows["D"]["current_disposition"] == "not_interested"   # most recent call that has one
    assert rows["A"]["last_call_at"] == "" and rows["A"]["current_disposition"] == ""
    assert r["batch_summary"] == [{
        "workflow_id": "motor_renewal", "batch_id": "batch_001", "total_leads": "4",
        "yet_to_reach_out": "1", "in_progress_telephony_failed": "1",
        "in_progress_dialed": "1", "completed": "1"}]


def test_call_without_initiated_event_is_not_counted(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A")])
    pipe.events("d1", "events_0001", [
        pipe.event("call.connected", "c1", "2026-10-03 10:00:00", P1),
        pipe.event("disposition.set", "c1", "2026-10-03 10:05:00", P1, disposition_code="renewed"),
    ])
    pipe.ingest("d1")
    r = pipe.report()
    assert by_ref(r)["A"]["bucket"] == "yet_to_reach_out"
    assert r["daily_overview"] == []
    assert r["data_quality"]["orphan_events"] == 0     # linked, just not a countable call


def test_daily_overview_uses_ist_dates_and_counts_success_leads_per_day(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A"), lead(phone=P2, ref="B")])
    pipe.events("d1", "events_0001", [
        # 19:00 UTC on the 3rd is 00:30 IST on the 4th
        pipe.event("call.initiated", "a1", "2026-10-03T19:00:00Z", P1),
        pipe.event("call.connected", "a1", "2026-10-03T19:00:05Z", P1),
        pipe.event("disposition.set", "a1", "2026-10-03T19:05:00Z", P1, disposition_code="renewed"),
        pipe.event("call.initiated", "b1", "2026-10-04 09:00:00", P2),
        pipe.event("call.initiated", "b2", "2026-10-04 15:00:00", P2),
        pipe.event("call.initiated", "b3", "2026-10-03 23:59:59", P2),
    ])
    pipe.ingest("d1")
    assert pipe.report()["daily_overview"] == [
        {"workflow_id": "motor_renewal", "date": "2026-10-03", "total_calls": "1",
         "connected_calls": "0", "connectivity_rate": "0.0000", "success_leads": "0"},
        {"workflow_id": "motor_renewal", "date": "2026-10-04", "total_calls": "3",
         "connected_calls": "1", "connectivity_rate": "0.3333", "success_leads": "1"}]


def test_orphans_malformed_and_duplicates(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A")])
    first = pipe.event("call.initiated", "c1", "2026-10-03 10:00:00", P1)
    pipe.events("d1", "events_0001", [
        first,
        first,                                                             # duplicate
        '{"event_id": "evt_trunc", "event_ty',                             # malformed
        '{"event_type": "call.initiated", "call_id": "x"}',                # no event_id
        "",                                                                # empty: not counted
        pipe.event("call.initiated", "c9", "2026-10-03 10:00:00", P2),     # never uploaded
        pipe.event("call.initiated", "c8", "2026-10-03 10:00:00", "12345"),  # invalid phone
        pipe.event("disposition.override", "c9", "2026-10-03 11:00:00", None,
                   disposition_code="unknown"),                            # orphan, not "unknown"
        pipe.event("call.initiated", "c7", "2026-10-03 10:00:00", P1, workflow="policy_feedback"),
    ])
    pipe.ingest("d1")
    q = pipe.report()["data_quality"]
    assert (q["events_total"], q["malformed_events"], q["duplicate_events_dropped"]) == (8, 2, 1)
    assert (q["orphan_events"], q["unknown_disposition_events"]) == (4, 0)


def test_events_that_arrive_before_their_lead_count_once_the_lead_arrives(pipe):
    pipe.leads("d1", "batch_001", [lead(phone=P1, ref="A")])
    pipe.events("d1", "events_0001", [
        pipe.event("call.initiated", "c1", "2026-10-03 10:00:00", P2),
        pipe.event("call.connected", "c1", "2026-10-03 10:00:05", P2),
        pipe.event("disposition.set", "c1", "2026-10-03 10:05:00", P2, disposition_code="renewed"),
        pipe.event("disposition.override", "c1", "2026-10-03 12:00:00", None, disposition_code="dnd"),
    ])
    pipe.ingest("d1")
    before = pipe.report()
    assert before["data_quality"]["orphan_events"] == 4
    assert before["daily_overview"] == []

    pipe.leads("d2", "batch_002", [lead(phone=P2, ref="B")])
    pipe.ingest("d2")
    after = pipe.report()
    assert after["data_quality"]["orphan_events"] == 0
    row = by_ref(after)["B"]
    assert (row["bucket"], row["total_calls"], row["current_disposition"]) == ("completed", "1", "dnd")
