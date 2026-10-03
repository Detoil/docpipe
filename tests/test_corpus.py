import json

import pytest
from conftest import make_response_line

from docpipe import triage
from docpipe.modules import corpus
from docpipe.modules.corpus import emit as emit_mod
from docpipe.modules.corpus import schema


def _triaged_project(project, verdicts=None):
    """Triage the docs with faked responses. `verdicts` maps display_name to
    (relevant, category, doc_date, summary); defaults to relevant/register."""
    verdicts = verdicts or {}
    conn = project.connect()
    lines = []
    for doc in triage.pending_docs(conn):
        rel, cat, date, summary = verdicts.get(
            doc["display_name"], (True, "register", "2021-03-01", "A test document.")
        )
        lines.append(make_response_line(f"doc-{doc['id']}", {
            "relevant": rel, "reason": "test", "category": cat,
            "doc_date": date, "summary": summary,
        }))
    triage.ingest_lines(conn, lines)
    conn.commit()
    conn.close()
    return project


# --- schema / validator -----------------------------------------------------

def test_all_thirteen_registers_encoded():
    expected = {
        "company_identity", "financial_profile", "financial_snapshot",
        "staff_register", "contract_register", "compliance_calendar",
        "task_queue", "document_index", "supplier_register",
        "customer_register", "tool_access_register", "hmcp_provider_register",
        "audit_log",
    }
    assert set(schema.REGISTERS) == expected


def test_unknown_register_aborts():
    with pytest.raises(schema.CorpusError):
        schema.validate_document({"not_a_register": [{"x": 1}]})


def test_required_natural_key_rejects_row():
    with pytest.raises(schema.SchemaError):
        schema.validate_document({"document_index": [{"filename": "x.pdf"}]})


def test_unknown_field_is_skipped_not_fatal():
    result = schema.validate_document({"document_index": [
        {"file_id": "sha256:abc", "made_up_field": "ignored"}
    ]})
    assert result.rows == 1
    assert ("document_index", "made_up_field") in result.unknown_fields


def test_null_field_is_allowed():
    result = schema.validate_document({"document_index": [
        {"file_id": "sha256:abc", "last_updated": None}
    ]})
    assert result.registers["document_index"] == 1


def test_bad_select_option_aborts():
    with pytest.raises(schema.CorpusError):
        schema.validate_document({"document_index": [
            {"file_id": "sha256:a", "doc_type": "spreadsheet"}
        ]})


def test_bad_date_aborts():
    with pytest.raises(schema.CorpusError):
        schema.validate_document({"document_index": [
            {"file_id": "sha256:a", "last_updated": "not-a-date"}
        ]})


def test_non_numeric_number_aborts():
    with pytest.raises(schema.CorpusError):
        schema.validate_document({"financial_snapshot": [
            {"snapshot_date": "2026-01-01", "bank_balance": "lots"}
        ]})


def test_numeric_string_number_is_ok():
    result = schema.validate_document({"financial_snapshot": [
        {"snapshot_date": "2026-01-01", "bank_balance": "412000.55"}
    ]})
    assert result.rows == 1


def test_nested_value_only_in_text_fields():
    # dict is fine in rich_text (JSON-stringified downstream)...
    schema.validate_document({"supplier_register": [
        {"supplier_uid": "engen", "contact": {"email": "a@b.com"}}
    ]})
    # ...but not in a number field.
    with pytest.raises(schema.CorpusError):
        schema.validate_document({"financial_snapshot": [
            {"snapshot_date": "2026-01-01", "bank_balance": [1, 2]}
        ]})


def test_iso_datetime_with_z_accepted():
    assert schema._is_iso8601("2026-06-25T07:30:00Z")
    assert schema._is_iso8601("2026-06-25")
    assert not schema._is_iso8601("25/06/2026")


def test_derive_uid_matches_documented_hash():
    # SHA-256 of parts joined by "|", first 16 hex.
    import hashlib
    parts = "renewal|2026-06-25|Vehicle licence"
    expected = hashlib.sha256(parts.encode()).hexdigest()[:16]
    got = schema.derive_uid("compliance_calendar", {
        "category": "renewal", "due_date": "2026-06-25", "name": "Vehicle licence",
    })
    assert got == expected


def test_derive_uid_staff_prefers_id_number():
    assert schema.derive_uid("staff_register", {"id_number": " 8801015 ", "name": "x"}) == "8801015"
    # Falls back to hash(name, role) when no id_number.
    hashed = schema.derive_uid("staff_register", {"name": "Jane", "role": "CFO"})
    assert len(hashed) == 16


def test_derive_uid_rejects_explicit_key_registers():
    with pytest.raises(schema.SchemaError):
        schema.derive_uid("document_index", {})


def test_derivable_register_missing_sources_still_validates_via_hash():
    # staff_register key is derivable; even an all-null row derives a key.
    result = schema.validate_document({"staff_register": [{"name": None, "role": None}]})
    assert result.rows == 1


# --- emit -------------------------------------------------------------------

def test_classify_doc_type():
    assert emit_mod.classify_doc_type("March invoice.pdf") == "invoice"
    assert emit_mod.classify_doc_type("", "bank statement", "", "") == "statement"
    assert emit_mod.classify_doc_type("Lease agreement", "contract") == "contract"
    assert emit_mod.classify_doc_type("payment receipt") == "receipt"
    assert emit_mod.classify_doc_type("random.txt") == "other"


def test_folder_path():
    assert emit_mod.folder_path("legal/shareholders_2021.txt") == "/legal"
    assert emit_mod.folder_path("a/b/c.pdf") == "/a/b"
    assert emit_mod.folder_path("root.txt") == "/"
    assert emit_mod.folder_path(None) == "/"


def test_emit_builds_valid_document_index(converted_project):
    proj = _triaged_project(converted_project)
    summary = emit_mod.emit(proj)

    assert summary["path"].exists()
    doc = json.loads(summary["path"].read_text())
    assert list(doc) == ["document_index"]
    rows = doc["document_index"]
    # Three unique documents (the duplicate shareholders_2021 is deduped).
    assert len(rows) == 3
    for row in rows:
        assert row["file_id"].startswith("sha256:")
        assert row["doc_type"] in ("contract", "invoice", "statement", "receipt", "other")
    # The emitted file re-validates cleanly (fail-closed guarantee).
    schema.validate_document(doc)


def test_emit_natural_key_is_stable_across_runs(converted_project):
    proj = _triaged_project(converted_project)
    first = json.loads(emit_mod.emit(proj)["path"].read_text())
    second = json.loads(emit_mod.emit(proj)["path"].read_text())
    assert first == second  # deterministic → idempotent upsert downstream


def test_emit_uses_content_hash_so_duplicates_collapse(converted_project):
    proj = _triaged_project(converted_project)
    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    file_ids = [r["file_id"] for r in doc["document_index"]]
    assert len(file_ids) == len(set(file_ids))  # no duplicate keys


def test_emit_relevant_only_filters(converted_project):
    proj = _triaged_project(converted_project, verdicts={
        "flyer.txt": (False, "marketing", None, "A promo flyer."),
    })
    all_rows = json.loads(emit_mod.emit(proj)["path"].read_text())["document_index"]
    rel_rows = json.loads(
        emit_mod.emit(proj, relevant_only=True)["path"].read_text()
    )["document_index"]
    assert len(all_rows) == 3
    assert len(rel_rows) == 2  # the flyer is excluded


def test_emit_before_scan_writes_nothing(project):
    summary = emit_mod.emit(project)
    assert summary["registers"] == {}
    assert not summary["path"].exists()


def test_status_lines(converted_project):
    proj = _triaged_project(converted_project)
    lines = emit_mod.status_lines(proj)
    assert any("document_index rows" in line for line in lines)
