import sqlite3

from solution.pipeline.leads import resolved_fields
from solution.pipeline.pii import PiiVault
from solution.pipeline.schema import DEFAULT_SCHEMA_DIR, load_schemas

from .conftest import lead


def stored_fields(pipe, lead_id):
    schema = load_schemas(DEFAULT_SCHEMA_DIR)["motor_renewal"]
    conn = sqlite3.connect(pipe.db)
    try:
        return resolved_fields(conn, PiiVault("test-only-secret"), schema, lead_id)
    finally:
        conn.close()


def test_rejection_reason_is_first_failure_in_schema_order(pipe):
    pipe.leads("d1", "batch_001", [
        lead(name="", phone="12345"),             # name comes first in the schema
        lead(phone="12345", ref=""),              # then phone
        lead(phone=""),                           # missing, not invalid
        lead(ref="   "),                          # whitespace = missing
        lead(),                                   # fine
    ])
    pipe.ingest("d1")
    r = pipe.report()
    assert [(x["row_number"], x["reason"]) for x in r["rejections"]] == [
        ("1", "missing_required:customer_name"), ("2", "invalid_phone"),
        ("3", "missing_required:phone"), ("4", "missing_required:policy_number")]
    assert r["data_quality"]["lead_rows_total"] == 5
    assert r["data_quality"]["lead_rows_rejected"] == 4
    assert len(r["lead_report"]) == 1


def test_blank_rows_are_skipped_and_do_not_consume_row_numbers(pipe):
    pipe.leads("d1", "batch_001", [
        lead(phone="9000000001", ref="A"),
        ["", "  ", "", "", "", ""],
        lead(phone="bad", ref="B"),
    ])
    pipe.ingest("d1")
    r = pipe.report()
    assert r["data_quality"]["lead_rows_total"] == 2
    assert r["rejections"][0]["row_number"] == "2"


def test_invalid_optional_fields_warn_but_rejected_rows_do_not_count_warnings(pipe):
    pipe.leads("d1", "batch_001", [
        lead(phone="9000000001", expiry="31/02/2026", premium="N/A", lang="Hindi"),   # 3 warnings
        lead(phone="9000000002", lang=""),                                             # empty: none
        lead(phone="bad", expiry="soon"),                                              # rejected
    ])
    pipe.ingest("d1")
    assert pipe.report()["data_quality"]["field_warnings"] == 3


def test_alias_headers_reordered_columns_and_extra_columns(pipe):
    pipe.leads("d1", "batch_001",
               [["hi", "Asha", "MOT/9", "+91 98765 43210", "VIP"]],
               header=["Language", " Insured_Name ", "POLICY  NO", "mobile_no", "agent_notes"])
    pipe.ingest("d1")
    row = pipe.report()["lead_report"][0]
    assert (row["external_ref"], row["phone_masked"]) == ("MOT/9", "+91******3210")


def test_duplicates_merge_last_non_null_wins_and_batch_is_permanent(pipe):
    pipe.leads("d1", "batch_001", [lead(ref="OLD", premium="100", lang="hi")])
    pipe.leads("d1", "batch_002", [lead(phone="+91 98765 43210", ref="NEW", premium="", lang="oops")])
    pipe.ingest("d1")
    r = pipe.report()
    assert r["data_quality"]["duplicate_lead_rows_merged"] == 1
    assert r["data_quality"]["field_warnings"] == 1
    [row] = r["lead_report"]
    assert (row["external_ref"], row["batch_id"]) == ("NEW", "batch_001")
    assert [(b["batch_id"], b["total_leads"]) for b in r["batch_summary"]] == [
        ("batch_001", "1"), ("batch_002", "0")]

    fields = stored_fields(pipe, 1)
    assert fields["premium_amount"] == "100.00"      # empty value did not overwrite
    assert fields["preferred_language"] == "hi"      # invalid value did not overwrite
    assert fields["policy_expiry"] is None


def test_default_applies_only_to_fields_still_null_after_merging(pipe):
    pipe.leads("d1", "batch_001", [lead(phone="9000000001"), lead(phone="9000000002")])
    pipe.leads("d2", "batch_002", [lead(phone="9000000002", lang="TA")])
    pipe.ingest("d1", "d2")
    langs = [stored_fields(pipe, i)["preferred_language"] for i in (1, 2)]
    assert langs == ["en", "ta"]


def test_same_phone_in_two_workflows_is_two_leads(pipe):
    pipe.leads("d1", "batch_001", [lead()])
    pipe.leads("d1", "batch_001", [["Asha", "9876543210", "ORD-1", "Phone", "", ""]],
               header=["customer_name", "phone", "order_id", "product", "order_date",
                       "preferred_language"], workflow="policy_feedback")
    pipe.ingest("d1")
    r = pipe.report()
    assert len(r["lead_report"]) == 2
    assert r["data_quality"]["duplicate_lead_rows_merged"] == 0
