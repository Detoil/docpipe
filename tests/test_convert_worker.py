import threading

from docpipe import convert, scan
from docpipe.config import Config, Project


def test_worker_converts_and_pdf_falls_back_to_gemini(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "page.html").write_text(
        "<html><body><h1>Board minutes</h1><p>Jane Doe appointed.</p></body></html>"
    )
    # A corrupt PDF must not fail: Gemini reads PDFs natively, so it gets
    # flagged needs_ocr and the original file is uploaded instead.
    (src / "broken.pdf").write_bytes(b"\x00\x01 definitely not a pdf \x02" * 20)

    proj = Project(tmp_path / "proj")
    proj.create(Config(source_dir=str(src), prompt="test"))
    scan.run(proj, proj.config)

    messages = []
    stats = convert.run(proj, proj.config, progress=messages.append)

    assert stats["failed"] == 0
    assert stats["converted"] == 2
    assert any(m.startswith("[1/2]") for m in messages)  # progress is visible

    conn = proj.connect()
    html_doc = conn.execute(
        "SELECT * FROM documents WHERE display_name = 'page.html'"
    ).fetchone()
    assert html_doc["status"] == "converted"
    assert "Board minutes" in html_doc["markdown"]

    pdf_doc = conn.execute(
        "SELECT * FROM documents WHERE display_name = 'broken.pdf'"
    ).fetchone()
    assert pdf_doc["status"] == "converted"
    assert pdf_doc["needs_ocr"] == 1
    conn.close()


def test_worker_survives_crash_and_continues(tmp_path, monkeypatch):
    """If the worker dies mid-file, the file is marked failed and the next
    file still converts (a fresh worker is started)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.html").write_text("<html><body><p>one</p></body></html>")
    (src / "b.html").write_text("<html><body><p>two</p></body></html>")

    proj = Project(tmp_path / "proj")
    proj.create(Config(source_dir=str(src), prompt="test"))
    scan.run(proj, proj.config)

    real_convert = convert.ConversionWorker.convert
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky(self, path, timeout):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            self._kill()  # simulate the child crashing on the first file
            return ("error", "the converter crashed on this file")
        return real_convert(self, path, timeout)

    monkeypatch.setattr(convert.ConversionWorker, "convert", flaky)
    stats = convert.run(proj, proj.config)

    assert stats["failed"] == 1
    assert stats["converted"] == 1
    conn = proj.connect()
    ok = conn.execute(
        "SELECT count(*) AS n FROM documents WHERE status = 'converted'"
    ).fetchone()["n"]
    assert ok == 1
    conn.close()


def test_parallel_convert_keeps_each_document_with_its_own_text(tmp_path):
    """The worker pipe carries no correlation ids, so the real risk of running
    several workers is one document being saved with another's markdown."""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(24):
        (src / f"doc{i:02d}.html").write_text(
            f"<html><body><p>unique-marker-{i:02d}</p></body></html>"
        )

    proj = Project(tmp_path / "proj")
    proj.create(Config(source_dir=str(src), prompt="test"))
    scan.run(proj, proj.config)

    stats = convert.run(proj, proj.config, workers=4)
    assert stats["converted"] == 24
    assert stats["failed"] == 0

    conn = proj.connect()
    rows = conn.execute("SELECT display_name, markdown FROM documents").fetchall()
    assert len(rows) == 24
    for row in rows:
        i = row["display_name"].removeprefix("doc").removesuffix(".html")
        assert f"unique-marker-{i}" in row["markdown"], (
            f"{row['display_name']} got another document's text"
        )
    conn.close()


def test_resolve_workers_prefers_config_then_falls_back_to_cores():
    assert convert.resolve_workers(Config(convert_workers=3)) == 3
    auto = convert.resolve_workers(Config())
    assert 1 <= auto <= convert.MAX_AUTO_WORKERS
