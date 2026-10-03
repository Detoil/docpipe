from docpipe import convert, scan


def test_scan_dedupes_and_filters(project):
    stats = scan.run(project, project.config)
    assert stats["new"] == 3  # two registers + flyer; the copy deduped away
    assert stats["duplicates"] == 1

    conn = project.connect()
    docs = conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
    assert len(docs) == 3

    # The duplicated register keeps both paths.
    dup = conn.execute(
        "SELECT document_id, count(*) AS n FROM document_paths GROUP BY document_id"
        " ORDER BY n DESC LIMIT 1"
    ).fetchone()
    assert dup["n"] == 2

    skipped = {r["reason"] for r in conn.execute("SELECT reason FROM skipped_files")}
    assert any("extension" in r for r in skipped)  # promo.mp4
    assert any("empty" in r for r in skipped)  # empty.txt
    names = [r["path"] for r in conn.execute("SELECT path FROM skipped_files")]
    assert not any(".DS_Store" in n or "~$" in n for n in names)  # junk never registered
    conn.close()


def test_scan_is_idempotent(project):
    scan.run(project, project.config)
    stats = scan.run(project, project.config)
    assert stats["new"] == 0
    conn = project.connect()
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 3
    conn.close()


def test_convert_text_fast_path(converted_project):
    conn = converted_project.connect()
    docs = conn.execute("SELECT * FROM documents").fetchall()
    assert all(d["status"] == "converted" for d in docs)
    register = conn.execute(
        "SELECT * FROM documents WHERE display_name = 'shareholders_2021.txt'"
    ).fetchone()
    assert "500 ordinary shares" in register["markdown"]
    conn.close()


def test_fts_search(converted_project):
    conn = converted_project.connect()
    rows = conn.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'ordinary'"
    ).fetchall()
    assert len(rows) == 2
    conn.close()
