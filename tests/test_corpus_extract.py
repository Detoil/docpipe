import hashlib
import json

from conftest import make_response_line

from docpipe import pipeline, triage
from docpipe.modules import corpus
from docpipe.modules.corpus import db as cdb
from docpipe.modules.corpus import emit as emit_mod
from docpipe.modules.corpus import extract as extract_mod
from docpipe.modules.corpus import schema


def _reviewed_project(project):
    conn = project.connect()
    lines = [
        make_response_line(f"doc-{doc['id']}", {
            "relevant": True, "reason": "test", "category": "register",
            "doc_date": "2021-03-01", "summary": "A test document.",
        })
        for doc in triage.pending_docs(conn)
    ]
    triage.ingest_lines(conn, lines)
    conn.execute("UPDATE documents SET reviewed = 1")
    conn.commit()
    conn.close()
    return project


def _doc_ids(project):
    conn = project.connect()
    ids = [d["id"] for d in extract_mod.pending_docs(conn)]
    conn.close()
    return ids


# --- schema generation ------------------------------------------------------

def test_response_schema_covers_targets_and_bakes_enums():
    rs = extract_mod.build_response_schema()
    props = rs["properties"]
    assert set(props) == {t.register for t in extract_mod.TARGETS}
    # Singleton -> nullable object; per-record -> array of objects.
    assert props["company_identity"]["type"] == "OBJECT"
    assert props["company_identity"]["nullable"] is True
    assert props["staff_register"]["type"] == "ARRAY"
    # Select options are baked in as enums...
    status = props["staff_register"]["items"]["properties"]["status"]
    assert status["enum"] == ["active", "resigned", "terminated"]
    # ...and model-computed keys are omitted from per-record schemas.
    assert "staff_uid" not in props["staff_register"]["items"]["properties"]
    assert "supplier_uid" not in props["supplier_register"]["items"]["properties"]
    # Singleton keeps its extracted key.
    assert "registration_number" in props["company_identity"]["properties"]


def test_excluded_registers_are_not_targeted():
    targeted = {t.register for t in extract_mod.TARGETS}
    assert "financial_snapshot" not in targeted
    assert "task_queue" not in targeted
    assert "audit_log" not in targeted


def test_field_guide_lists_enums():
    guide = extract_mod.system_prompt()
    assert "in_business | deregistration_process | deregistered" in guide
    assert "alert_windows" in guide and "JSON" in guide


def test_pipeline_dispatches_corpus_extract_stage():
    ingestors = pipeline.stage_ingestors()
    assert ingestors["corpus_extract"] is corpus.INGESTORS["corpus_extract"]


# --- ingest: identity computed producer-side --------------------------------

def test_ingest_mints_supplier_uid_from_name(converted_project):
    proj = _reviewed_project(converted_project)
    doc_id = _doc_ids(proj)[0]
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [make_response_line(f"doc-{doc_id}", {
        "supplier_register": [
            {"name": "Engen Fleet Fuel", "payment_terms": "30 days",
             "supplier_uid": "MODEL-TRIED-TO-SET-THIS", "confidence": 0.9},
        ],
    })])
    rows = cdb.records_for(conn, "supplier_register")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["natural_key"] == "engen-fleet-fuel"  # slugified from name
    # A model-supplied uid is never trusted / stored.
    assert "supplier_uid" not in rows[0]["data"]
    assert rows[0]["confidence"] == 0.9


def test_ingest_derives_uid_with_documented_hash(converted_project):
    proj = _reviewed_project(converted_project)
    doc_id = _doc_ids(proj)[0]
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [make_response_line(f"doc-{doc_id}", {
        "compliance_calendar": [
            {"category": "renewal", "due_date": "2026-06-25",
             "name": "Vehicle licence", "confidence": 0.8},
        ],
    })])
    rows = cdb.records_for(conn, "compliance_calendar")
    conn.close()
    expected = hashlib.sha256("renewal|2026-06-25|Vehicle licence".encode()).hexdigest()[:16]
    assert rows[0]["natural_key"] == expected


def test_ingest_skips_record_with_no_mintable_key(converted_project):
    proj = _reviewed_project(converted_project)
    doc_id = _doc_ids(proj)[0]
    conn = proj.connect()
    stats = extract_mod.ingest_lines(conn, [make_response_line(f"doc-{doc_id}", {
        "supplier_register": [{"payment_terms": "30 days"}],  # no name -> no slug
    })])
    conn.close()
    assert stats["skipped_no_key"] == 1
    assert stats["records"] == 0


def test_ingest_is_idempotent(converted_project):
    proj = _reviewed_project(converted_project)
    doc_id = _doc_ids(proj)[0]
    line = make_response_line(f"doc-{doc_id}", {
        "supplier_register": [{"name": "Acme", "confidence": 0.5}],
    })
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [line])
    extract_mod.ingest_lines(conn, [line])
    n = conn.execute("SELECT count(*) AS n FROM corpus_records").fetchone()["n"]
    conn.close()
    assert n == 1


def test_ingest_failure_leaves_doc_pending(converted_project):
    proj = _reviewed_project(converted_project)
    doc_id = _doc_ids(proj)[0]
    conn = proj.connect()
    stats = extract_mod.ingest_lines(conn, [
        {"key": f"doc-{doc_id}", "status": {"code": 13, "message": "boom"}},
    ])
    still_pending = [d["id"] for d in extract_mod.pending_docs(conn)]
    conn.close()
    assert stats["errors"] == 1
    assert doc_id in still_pending


# --- emit: merge / dedup / sanitize -----------------------------------------

def test_emit_merges_singletons_across_documents(converted_project):
    proj = _reviewed_project(converted_project)
    d1, d2, d3 = _doc_ids(proj)
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [
        make_response_line(f"doc-{d1}", {"company_identity": {
            "registration_number": "2019/123456/07", "legal_name": "Acme (Pty) Ltd",
            "confidence": 0.9}}),
        # A second doc adds a field but repeats no reg number.
        make_response_line(f"doc-{d2}", {"company_identity": {
            "cipc_status": "in_business", "confidence": 0.8}}),
        make_response_line(f"doc-{d3}", {"financial_profile": {
            "vat_number": "4123456789", "confidence": 0.7}}),
    ])
    conn.close()

    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    ci = doc["company_identity"]
    assert len(ci) == 1
    row = ci[0]
    assert row["registration_number"] == "2019/123456/07"  # canonical key
    assert row["legal_name"] == "Acme (Pty) Ltd"
    assert row["cipc_status"] == "in_business"  # merged from the second doc
    # financial_profile reused the same canonical registration number.
    assert doc["financial_profile"][0]["registration_number"] == "2019/123456/07"
    assert doc["financial_profile"][0]["vat_number"] == "4123456789"
    schema.validate_document(doc)  # emitted document must load


def test_emit_deduplicates_records_by_natural_key(converted_project):
    proj = _reviewed_project(converted_project)
    d1, d2, _ = _doc_ids(proj)
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [
        make_response_line(f"doc-{d1}", {"supplier_register": [
            {"name": "Engen", "payment_terms": "30 days", "confidence": 0.6}]}),
        make_response_line(f"doc-{d2}", {"supplier_register": [
            {"name": "Engen", "status": "active", "confidence": 0.9}]}),
    ])
    conn.close()

    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    suppliers = doc["supplier_register"]
    assert len(suppliers) == 1  # both docs -> one supplier
    assert suppliers[0]["supplier_uid"] == "engen"
    assert suppliers[0]["payment_terms"] == "30 days"
    assert suppliers[0]["status"] == "active"


def test_emit_drops_values_that_would_not_load(converted_project):
    proj = _reviewed_project(converted_project)
    d1 = _doc_ids(proj)[0]
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [
        make_response_line(f"doc-{d1}", {"staff_register": [{
            "name": "Jane", "role": "CFO",
            "salary": "not-a-number",       # non-numeric -> dropped
            "hire_date": "last Tuesday",    # non-ISO -> dropped
            "status": "employed",           # off-enum -> dropped
            "confidence": 0.9,
        }]}),
    ])
    conn.close()

    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    row = doc["staff_register"][0]
    assert row["name"] == "Jane" and row["role"] == "CFO"
    assert "salary" not in row and "hire_date" not in row and "status" not in row
    # The whole document still loads (fail-closed producer side).
    schema.validate_document(doc)


def test_emit_skips_company_without_registration_number(converted_project):
    proj = _reviewed_project(converted_project)
    d1 = _doc_ids(proj)[0]
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [
        make_response_line(f"doc-{d1}", {"company_identity": {
            "legal_name": "Nameless Co", "confidence": 0.9}}),  # no reg number
    ])
    conn.close()
    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    # Can't key it -> not emitted; document_index still present.
    assert "company_identity" not in doc
    assert "document_index" in doc


def test_emit_valid_multi_register_document(converted_project):
    proj = _reviewed_project(converted_project)
    d1, d2, d3 = _doc_ids(proj)
    conn = proj.connect()
    extract_mod.ingest_lines(conn, [
        make_response_line(f"doc-{d1}", {
            "company_identity": {"registration_number": "2019/1/07",
                                 "legal_name": "Acme", "confidence": 0.9},
            "contract_register": [{"contract_type": "lease", "counterparty": "Landlord Ltd",
                                   "address": "1 Main St", "monthly_amount": 15000,
                                   "confidence": 0.8}],
        }),
        make_response_line(f"doc-{d2}", {"customer_register": [
            {"name": "Big Buyer", "payment_status": "current", "confidence": 0.7}]}),
        make_response_line(f"doc-{d3}", {"tool_access_register": [
            {"tool": "Xero", "person": "Jane", "login": "jane@acme.co",
             "status": "active", "confidence": 0.6}]}),
    ])
    conn.close()

    summary = emit_mod.emit(proj)
    doc = json.loads(summary["path"].read_text())
    assert set(doc) >= {"document_index", "company_identity", "contract_register",
                        "customer_register", "tool_access_register"}
    # Derived/minted keys are present and well-formed.
    assert len(doc["contract_register"][0]["contract_uid"]) == 16
    assert doc["customer_register"][0]["customer_uid"] == "big-buyer"
    assert doc["tool_access_register"][0]["login"] == "jane@acme.co"
    schema.validate_document(doc)  # fail-closed guarantee
    # Deterministic: a second emit is byte-identical.
    assert emit_mod.build_document(proj) == emit_mod.build_document(proj)


def test_emit_without_extraction_is_still_just_document_index(converted_project):
    proj = _reviewed_project(converted_project)
    doc = json.loads(emit_mod.emit(proj)["path"].read_text())
    assert list(doc) == ["document_index"]
