import json

from conftest import make_response_line

from docpipe import extract, triage


def _triage_all(conn, cfg, relevant_names=None):
    """Run triage build+ingest against faked batch responses."""
    docs = triage.pending_docs(conn)
    lines = []
    for doc in docs:
        is_relevant = relevant_names is None or doc["display_name"] in relevant_names
        lines.append(make_response_line(f"doc-{doc['id']}", {
            "relevant": is_relevant,
            "reason": "test verdict",
            "category": "shareholder register" if is_relevant else "marketing",
            "doc_date": "2021-03-01" if is_relevant else None,
            "summary": "A test document.",
        }))
    return triage.ingest_lines(conn, lines)


def test_triage_request_shape(converted_project):
    cfg = converted_project.config
    conn = converted_project.connect()
    docs = triage.pending_docs(conn)
    req = triage.build_request(conn, cfg, docs[0])
    assert req["key"] == f"doc-{docs[0]['id']}"
    gen = req["request"]["generation_config"]
    assert gen["response_mime_type"] == "application/json"
    assert gen["response_schema"]["type"] == "OBJECT"
    assert cfg.prompt in req["request"]["system_instruction"]["parts"][0]["text"]
    body = req["request"]["contents"][0]["parts"][0]["text"]
    assert docs[0]["display_name"] in body
    json.dumps(req)  # must be JSONL-serialisable
    conn.close()


def test_triage_excerpt_truncation(converted_project):
    cfg = converted_project.config
    cfg.triage_excerpt_chars = 20
    conn = converted_project.connect()
    doc = triage.pending_docs(conn)[0]
    req = triage.build_request(conn, cfg, doc)
    assert "(truncated)" in req["request"]["contents"][0]["parts"][0]["text"]
    conn.close()


def test_triage_ingest_sets_verdicts(converted_project):
    conn = converted_project.connect()
    stats = _triage_all(conn, converted_project.config,
                        relevant_names={"shareholders_2021.txt", "shareholders_2023.md"})
    assert stats == {"ok": 3, "errors": 0}
    rel = conn.execute("SELECT count(*) AS n FROM documents WHERE relevant = 1").fetchone()["n"]
    irr = conn.execute("SELECT count(*) AS n FROM documents WHERE relevant = 0").fetchone()["n"]
    assert (rel, irr) == (2, 1)
    assert triage.pending_docs(conn) == []
    conn.close()


def test_triage_ingest_bad_line_leaves_doc_retryable(converted_project):
    conn = converted_project.connect()
    doc = triage.pending_docs(conn)[0]
    bad = {"key": f"doc-{doc['id']}", "status": {"code": 13, "message": "internal"}}
    stats = triage.ingest_lines(conn, [bad])
    assert stats["errors"] == 1
    refreshed = conn.execute("SELECT status FROM documents WHERE id = ?", (doc["id"],)).fetchone()
    assert refreshed["status"] == "converted"  # will be retried on next submit
    conn.close()


def test_extract_ingest_and_current_facts(converted_project):
    conn = converted_project.connect()
    _triage_all(conn, converted_project.config)
    conn.execute("UPDATE documents SET reviewed = 1")
    conn.commit()

    docs = {d["display_name"]: d for d in extract.pending_docs(conn)}
    lines = [
        make_response_line(f"doc-{docs['shareholders_2021.txt']['id']}", {
            "doc_type": "shareholder register", "doc_date": "2021-03-01",
            "summary": "2021 register.",
            "facts": [{"entity": "John Smith", "entity_type": "shareholding",
                       "attribute": "shares held", "value": "500",
                       "as_of_date": "2021-03-01", "confidence": 0.95,
                       "quote": "John Smith holds 500 ordinary shares."}],
        }),
        make_response_line(f"doc-{docs['shareholders_2023.md']['id']}", {
            "doc_type": "shareholder register", "doc_date": "2023-06-10",
            "summary": "2023 register.",
            "facts": [{"entity": "John Smith", "entity_type": "shareholding",
                       "attribute": "shares held", "value": "800",
                       "as_of_date": "2023-06-10", "confidence": 0.95,
                       "quote": "John Smith holds 800 ordinary shares."}],
        }),
    ]
    stats = extract.ingest_lines(conn, lines)
    assert stats["ok"] == 2 and stats["facts"] == 2

    # Both assertions kept (append-only history)...
    history = conn.execute(
        "SELECT value FROM facts WHERE entity = 'John Smith' ORDER BY as_of_date"
    ).fetchall()
    assert [r["value"] for r in history] == ["500", "800"]
    # ...while current_facts resolves to the latest one.
    current = conn.execute(
        "SELECT value, as_of_date FROM current_facts WHERE entity = 'John Smith'"
    ).fetchone()
    assert current["value"] == "800" and current["as_of_date"] == "2023-06-10"
    conn.close()


def test_extract_reingest_is_idempotent(converted_project):
    conn = converted_project.connect()
    _triage_all(conn, converted_project.config)
    conn.execute("UPDATE documents SET reviewed = 1")
    conn.commit()
    doc = extract.pending_docs(conn)[0]
    line = make_response_line(f"doc-{doc['id']}", {
        "doc_type": "register", "doc_date": "2021-03-01", "summary": "s",
        "facts": [{"entity": "E", "attribute": "a", "value": "v"}],
    })
    extract.ingest_lines(conn, [line])
    extract.ingest_lines(conn, [line])
    n = conn.execute("SELECT count(*) AS n FROM facts WHERE document_id = ?",
                     (doc["id"],)).fetchone()["n"]
    assert n == 1
    conn.close()


def test_a_submitted_job_survives_a_failure_on_a_later_chunk(converted_project, monkeypatch):
    """A batch the API accepted is already costing money, so it must be recorded
    even if a later chunk is rejected (e.g. a 429 on enqueued-token quota)."""
    from docpipe import db, gemini

    cfg = converted_project.config
    cfg.max_batch_requests = 1  # one chunk per document

    calls = {"n": 0}

    def fake_submit_batch(client, model, jsonl_path, display_name):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return f"batches/fake-{calls['n']}"

    monkeypatch.setattr(gemini, "get_client", lambda: object())
    monkeypatch.setattr(gemini, "submit_batch", fake_submit_batch)

    try:
        triage.submit(converted_project, cfg)
    except RuntimeError:
        pass

    conn = converted_project.connect()
    jobs = conn.execute("SELECT job_name FROM batch_jobs").fetchall()
    conn.close()
    assert [j["job_name"] for j in jobs] == ["batches/fake-1"]
