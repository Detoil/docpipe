"""The human review gate between triage and extraction.

`export` writes the triage verdicts to review.csv (editable) and review.html
(readable). The user flips the `relevant` column for any wrong verdicts and
runs `apply`, which updates the database, marks documents reviewed, and
materialises the irrelevant-documents folder as symlinks back into the
source tree (the source itself is never moved or modified).
"""

from __future__ import annotations

import csv
import html
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import db
from .config import Config, Project

CSV_FIELDS = [
    "doc_id", "relevant", "file_name", "category", "doc_date",
    "reason", "summary", "needs_ocr", "paths",
]

TRUTHY = {"1", "true", "yes", "y", "keep", "relevant"}
FALSY = {"0", "false", "no", "n", "drop", "irrelevant"}


def _triaged_docs(conn):
    return conn.execute(
        "SELECT * FROM documents WHERE status IN ('triaged', 'extracted')"
        " ORDER BY relevant DESC, triage_category, display_name"
    ).fetchall()


def export(project: Project, cfg: Config) -> tuple[Path, Path]:
    conn = project.connect()
    try:
        docs = _triaged_docs(conn)
        csv_path = project.review_dir / "review.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for doc in docs:
                writer.writerow({
                    "doc_id": doc["id"],
                    "relevant": doc["relevant"],
                    "file_name": doc["display_name"],
                    "category": doc["triage_category"] or "",
                    "doc_date": doc["doc_date"] or "",
                    "reason": doc["triage_reason"] or "",
                    "summary": doc["summary"] or "",
                    "needs_ocr": doc["needs_ocr"],
                    "paths": "; ".join(db.all_paths(conn, doc["id"])),
                })

        html_path = project.review_dir / "review.html"
        html_path.write_text(_render_html(conn, docs), encoding="utf-8")
        return csv_path, html_path
    finally:
        conn.close()


def _render_html(conn, docs) -> str:
    relevant = sum(1 for d in docs if d["relevant"])
    rows = []
    for doc in docs:
        cls = "rel" if doc["relevant"] else "irr"
        rows.append(
            f"<tr class='{cls}'><td>{doc['id']}</td>"
            f"<td>{'yes' if doc['relevant'] else 'no'}</td>"
            f"<td>{html.escape(doc['display_name'])}</td>"
            f"<td>{html.escape(doc['triage_category'] or '')}</td>"
            f"<td>{html.escape(doc['doc_date'] or '')}</td>"
            f"<td>{html.escape(doc['triage_reason'] or '')}</td>"
            f"<td>{html.escape(doc['summary'] or '')}</td>"
            f"<td>{html.escape('; '.join(db.all_paths(conn, doc['id'])))}</td></tr>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>docpipe triage review</title>
<style>
body {{ font-family: sans-serif; margin: 1.5em; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; vertical-align: top; }}
tr.rel {{ background: #eaf7ea; }}
tr.irr {{ background: #fbecec; }}
</style></head><body>
<h1>Triage review</h1>
<p>{len(docs)} documents triaged: {relevant} relevant, {len(docs) - relevant} irrelevant.</p>
<p>To change verdicts, edit the <code>relevant</code> column (1/0) in
<code>review.csv</code> next to this file, then run <code>docpipe review apply</code>.</p>
<table><tr><th>id</th><th>relevant</th><th>file</th><th>category</th><th>date</th>
<th>reason</th><th>summary</th><th>paths</th></tr>
{''.join(rows)}
</table></body></html>
"""


def apply(project: Project, cfg: Config, csv_path: Path | None = None,
          accept_all: bool = False) -> dict:
    conn = project.connect()
    stats = {"reviewed": 0, "changed": 0, "irrelevant": 0}
    try:
        if accept_all:
            cur = conn.execute(
                "UPDATE documents SET reviewed = 1"
                " WHERE status = 'triaged' AND relevant IS NOT NULL"
            )
            stats["reviewed"] = cur.rowcount
        else:
            csv_path = csv_path or (project.review_dir / "review.csv")
            if not Path(csv_path).exists():
                raise FileNotFoundError(
                    f"{csv_path} not found — run `docpipe review export` first."
                )
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    doc_id = int(row["doc_id"])
                    raw = (row.get("relevant") or "").strip().lower()
                    if raw in TRUTHY:
                        new_relevant = 1
                    elif raw in FALSY:
                        new_relevant = 0
                    else:
                        continue  # blank/unparseable: keep the model's verdict
                    old = conn.execute(
                        "SELECT relevant FROM documents WHERE id = ?", (doc_id,)
                    ).fetchone()
                    if old is None:
                        continue
                    if old["relevant"] != new_relevant:
                        stats["changed"] += 1
                    db.update_document(conn, doc_id, relevant=new_relevant, reviewed=1)
                    stats["reviewed"] += 1

        stats["irrelevant"] = _materialise_irrelevant(project, conn)
        db.set_meta(conn, "review_applied_at", datetime.now().isoformat(timespec="seconds"))
        conn.commit()
        return stats
    finally:
        conn.close()


def _materialise_irrelevant(project: Project, conn) -> int:
    """(Re)build the irrelevant/ folder as symlinks to the source files."""
    folder = project.irrelevant_dir
    for entry in folder.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
    docs = conn.execute(
        "SELECT id, display_name FROM documents WHERE relevant = 0 AND reviewed = 1"
    ).fetchall()
    for doc in docs:
        path_row = db.first_path(conn, doc["id"])
        if not path_row:
            continue
        link = folder / f"{doc['id']:05d}_{doc['display_name']}"
        try:
            os.symlink(path_row["path"], link)
        except OSError:
            try:
                shutil.copy2(path_row["path"], link)
            except OSError:
                pass
    return len(docs)
