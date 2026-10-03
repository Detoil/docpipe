import json

from conftest import make_response_line

from docpipe import pipeline, triage
from docpipe.modules import dataroom
from docpipe.modules.dataroom import build as build_mod
from docpipe.modules.dataroom import checklist as cl
from docpipe.modules.dataroom import db as ddb
from docpipe.modules.dataroom import mapping


def _reviewed_project(project):
    """Triage every doc as relevant (faked responses) and mark reviewed."""
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


def _checklist(project):
    checklist = cl.base_checklist()
    cl.save(project, checklist)
    conn = project.connect()
    ddb.sync_items(conn, checklist)
    conn.commit()
    conn.close()
    return checklist


def test_base_checklist_is_consistent():
    checklist = cl.base_checklist()
    codes = [item["code"] for item in checklist["items"]]
    assert len(codes) == len(set(codes))
    categories = {c["code"] for c in checklist["categories"]}
    for item in checklist["items"]:
        assert item["category"] in categories
        assert item["importance"] in cl.IMPORTANCE_LEVELS
        prefix = item["code"].split("-")[0]
        assert prefix == item["category"]


def test_checklist_roundtrip(project):
    checklist = cl.base_checklist()
    cl.save(project, checklist)
    assert cl.load(project) == checklist


def test_merge_localization_adds_and_annotates():
    checklist = cl.base_checklist()
    n_items = len(checklist["items"])
    stats = cl.merge_localization(checklist, {
        "additions": [
            {"category": "REG", "title": "B-BBEE certificate",
             "description": "Broad-based black economic empowerment status.",
             "importance": "required", "note": "Issued by a SANAS-accredited agency."},
            {"category": "NOPE", "title": "Bad category"},
            {"category": "TAX", "title": "No importance given"},
        ],
        "local_notes": [
            {"code": "COR-01", "note": "CIPC registration certificate (COR14.3)."},
            {"code": "XXX-99", "note": "unknown item"},
        ],
    }, "South Africa")
    assert stats == {"added": 2, "notes": 1, "skipped": 2}
    assert checklist["jurisdiction"] == "South Africa"
    assert len(checklist["items"]) == n_items + 2
    added = {i["code"]: i for i in checklist["items"]}
    # Codes are assigned after the existing highest number in the category.
    assert "REG-04" in added and added["REG-04"]["title"] == "B-BBEE certificate"
    assert added["TAX-05"]["importance"] == "recommended"  # default applied
    assert added["COR-01"]["note"].startswith("CIPC")


def test_map_request_shape(converted_project):
    proj = _reviewed_project(converted_project)
    checklist = _checklist(proj)
    conn = proj.connect()
    docs = mapping.pending_docs(conn)
    assert len(docs) == 3
    system = mapping.prompts.MAP_SYSTEM.format(checklist=cl.render(checklist))
    req = mapping.build_request(conn, proj.config, docs[0], system)
    assert req["key"] == f"doc-{docs[0]['id']}"
    gen = req["request"]["generation_config"]
    assert gen["response_mime_type"] == "application/json"
    assert "EQT-01" in req["request"]["system_instruction"]["parts"][0]["text"]
    assert docs[0]["display_name"] in req["request"]["contents"][0]["parts"][0]["text"]
    json.dumps(req)  # must be JSONL-serialisable
    conn.close()


def test_map_ingest_records_matches_and_retries_failures(converted_project):
    proj = _reviewed_project(converted_project)
    _checklist(proj)
    conn = proj.connect()
    d1, d2, d3 = mapping.pending_docs(conn)
    lines = [
        make_response_line(f"doc-{d1['id']}", {"matches": [
            {"code": "EQT-01", "confidence": 0.9, "period": "FY2021", "note": "cap table"},
            {"code": "BAD-99", "confidence": 0.9},  # unknown code: dropped
        ]}),
        make_response_line(f"doc-{d2['id']}", {"matches": []}),
        {"key": f"doc-{d3['id']}", "status": {"code": 13, "message": "internal"}},
    ]
    stats = mapping.ingest_lines(conn, lines)
    assert stats == {"ok": 2, "errors": 1, "matches": 1, "unknown_codes": 1}
    # d1 and d2 are mapped; d3 stays pending for the next submit.
    assert [d["id"] for d in mapping.pending_docs(conn)] == [d3["id"]]
    row = conn.execute("SELECT * FROM dataroom_map WHERE document_id = ?",
                       (d1["id"],)).fetchone()
    assert row["item_code"] == "EQT-01" and row["period"] == "FY2021"
    conn.close()


def test_map_reingest_is_idempotent(converted_project):
    proj = _reviewed_project(converted_project)
    _checklist(proj)
    conn = proj.connect()
    doc = mapping.pending_docs(conn)[0]
    line = make_response_line(f"doc-{doc['id']}",
                              {"matches": [{"code": "EQT-01", "confidence": 0.9}]})
    mapping.ingest_lines(conn, [line])
    mapping.ingest_lines(conn, [line])
    n = conn.execute("SELECT count(*) AS n FROM dataroom_map WHERE document_id = ?",
                     (doc["id"],)).fetchone()["n"]
    assert n == 1
    conn.close()


def test_build_outputs(converted_project):
    proj = _reviewed_project(converted_project)
    _checklist(proj)
    conn = proj.connect()
    d1, d2, d3 = mapping.pending_docs(conn)
    mapping.ingest_lines(conn, [
        make_response_line(f"doc-{d1['id']}",
                           {"matches": [{"code": "EQT-01", "confidence": 0.9}]}),
        make_response_line(f"doc-{d2['id']}",
                           {"matches": [{"code": "EQT-01", "confidence": 0.8}]}),
        make_response_line(f"doc-{d3['id']}", {"matches": []}),
    ])
    conn.close()

    paths = build_mod.build(proj, proj.config)

    equity_dir = paths["dataroom"] / "02_Ownership_and_Equity"
    links = list(equity_dir.iterdir())
    assert len(links) == 2 and all(l.name.startswith("EQT-01__") for l in links)
    # The unmatched doc lands in the unassigned folder.
    unassigned = list((paths["dataroom"] / cl.UNASSIGNED_FOLDER).iterdir())
    assert len(unassigned) == 1

    index = paths["index"].read_text()
    assert "EQT-01" in index and "covered" in index

    gap = paths["gap_report"].read_text()
    assert "1 / " in gap or "**1 /" in gap  # one required item covered
    assert "COR-01" in gap and "MISSING" in gap
    # First build: no snapshot to diff against.
    assert "Changes since last build" not in gap

    # A rebuild is idempotent (no duplicate links).
    build_mod.build(proj, proj.config)
    assert len(list(equity_dir.iterdir())) == 2


def test_gap_report_diffs_follow_up_pass(converted_project):
    proj = _reviewed_project(converted_project)
    _checklist(proj)
    conn = proj.connect()
    d1, d2, d3 = mapping.pending_docs(conn)
    mapping.ingest_lines(conn, [
        make_response_line(f"doc-{d1['id']}",
                           {"matches": [{"code": "EQT-01", "confidence": 0.9}]}),
    ])
    conn.close()
    build_mod.build(proj, proj.config)

    # The follow-up pass: a previously missing document arrives and is mapped.
    conn = proj.connect()
    mapping.ingest_lines(conn, [
        make_response_line(f"doc-{d2['id']}",
                           {"matches": [{"code": "FIN-01", "confidence": 0.9}]}),
    ])
    conn.close()
    paths = build_mod.build(proj, proj.config)
    gap = paths["gap_report"].read_text()
    assert "Changes since last build" in gap
    assert "Gaps closed" in gap and "FIN-01" in gap


def test_pipeline_dispatches_dataroom_stage():
    ingestors = pipeline.stage_ingestors()
    assert ingestors["dataroom_map"] is dataroom.INGESTORS["dataroom_map"]
    assert set(ingestors) >= {"triage", "extract", "dataroom_map"}
