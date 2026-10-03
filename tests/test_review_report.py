import csv

from conftest import make_response_line

from docpipe import report, review, triage


def _triage(conn, cfg):
    docs = triage.pending_docs(conn)
    lines = []
    for doc in docs:
        is_marketing = doc["display_name"] == "flyer.txt"
        lines.append(make_response_line(f"doc-{doc['id']}", {
            "relevant": not is_marketing,
            "reason": "promo" if is_marketing else "core record",
            "category": "marketing" if is_marketing else "register",
            "doc_date": None,
            "summary": "s",
        }))
    triage.ingest_lines(conn, lines)


def test_review_roundtrip(converted_project):
    proj = converted_project
    conn = proj.connect()
    _triage(conn, proj.config)
    conn.close()

    csv_path, html_path = review.export(proj, proj.config)
    assert csv_path.exists() and html_path.exists()

    # The user flips the flyer back to relevant.
    rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))
    assert len(rows) == 3
    for row in rows:
        if row["file_name"] == "flyer.txt":
            row["relevant"] = "1"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=review.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    stats = review.apply(proj, proj.config)
    assert stats["reviewed"] == 3
    assert stats["changed"] == 1
    assert stats["irrelevant"] == 0  # nothing irrelevant after the flip

    conn = proj.connect()
    flyer = conn.execute(
        "SELECT relevant, reviewed FROM documents WHERE display_name = 'flyer.txt'"
    ).fetchone()
    assert flyer["relevant"] == 1 and flyer["reviewed"] == 1
    conn.close()


def test_review_accept_all_builds_irrelevant_folder(converted_project):
    proj = converted_project
    conn = proj.connect()
    _triage(conn, proj.config)
    conn.close()

    stats = review.apply(proj, proj.config, accept_all=True)
    assert stats["reviewed"] == 3
    assert stats["irrelevant"] == 1
    links = list(proj.irrelevant_dir.iterdir())
    assert len(links) == 1
    assert "flyer.txt" in links[0].name


def test_report_builds(converted_project):
    proj = converted_project
    conn = proj.connect()
    _triage(conn, proj.config)
    conn.execute("UPDATE documents SET reviewed = 1")
    conn.commit()
    conn.execute(
        "INSERT INTO facts (document_id, entity, attribute, value, as_of_date)"
        " SELECT id, 'John Smith', 'shares held', '500', '2021-03-01'"
        " FROM documents WHERE display_name = 'shareholders_2021.txt'"
    )
    conn.commit()
    conn.close()

    path = report.build(proj, proj.config)
    text = path.read_text()
    assert "John Smith" in text
    assert "flyer.txt" in text  # listed under irrelevant documents
    assert "Current state" in text
