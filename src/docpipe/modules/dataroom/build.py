"""Materialise the dataroom and its reports from the mapping in the database.

`build` is fully regenerable and cheap to re-run: it (re)creates the
dataroom/ folder of category-organised symlinks back into the source tree
(the source is never moved or modified), INDEX.md, and the gap report. A
coverage snapshot is stored in the database so the next build — the
follow-up pass after missing documents have been added — reports which gaps
were closed since last time.

The investor report is a separate one-call LLM synthesis (`investor_report`)
so the deterministic outputs still build without an API key.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from ... import gemini
from ...config import Config, Project
from . import checklist as checklist_mod
from . import db, prompts

SNAPSHOT_META_KEY = "dataroom_coverage_snapshot"

STATUS_LABEL = {"covered": "covered", "missing": "MISSING"}


def dataroom_dir(project: Project):
    return project.root / "dataroom"


def coverage(conn, checklist: dict) -> dict:
    """Coverage of the checklist by mapped documents.

    Returns {
      "items":      {code: [doc-match rows]},
      "unassigned": [docs mapped to no item],
      "unmapped":   number of relevant docs not yet mapped,
    }"""
    db.ensure_schema(conn)
    items: dict[str, list] = {item["code"]: [] for item in checklist["items"]}
    rows = conn.execute(
        "SELECT m.item_code, m.confidence, m.period, m.note,"
        "       d.id, d.display_name, d.doc_date"
        " FROM dataroom_map m JOIN documents d ON d.id = m.document_id"
        " WHERE d.relevant = 1"
        " ORDER BY m.item_code, (d.doc_date IS NULL), d.doc_date, d.id"
    ).fetchall()
    for r in rows:
        if r["item_code"] in items:
            items[r["item_code"]].append(r)
    unassigned = conn.execute(
        "SELECT d.* FROM documents d"
        " JOIN dataroom_docs dd ON dd.document_id = d.id AND dd.mapped_at IS NOT NULL"
        " WHERE d.relevant = 1"
        "   AND d.id NOT IN (SELECT document_id FROM dataroom_map)"
        " ORDER BY d.id"
    ).fetchall()
    unmapped = conn.execute(
        "SELECT count(*) AS n FROM documents WHERE relevant = 1 AND reviewed = 1"
        " AND id NOT IN (SELECT document_id FROM dataroom_docs WHERE mapped_at IS NOT NULL)"
    ).fetchone()["n"]
    return {"items": items, "unassigned": unassigned, "unmapped": unmapped}


def _counts(checklist: dict, cov: dict) -> dict:
    """Covered/total per importance level."""
    out = {level: [0, 0] for level in checklist_mod.IMPORTANCE_LEVELS}
    for item in checklist["items"]:
        level = item.get("importance") or "required"
        if level not in out:
            level = "required"
        out[level][1] += 1
        if cov["items"].get(item["code"]):
            out[level][0] += 1
    return out


def _link(target_path: str, link):
    try:
        os.symlink(target_path, link)
    except OSError:
        try:
            shutil.copy2(target_path, link)
        except OSError:
            pass


def _materialise(project: Project, conn, checklist: dict, cov: dict) -> None:
    """(Re)build the dataroom folder tree of symlinks to the source files."""
    from ... import db as core_db

    root = dataroom_dir(project)
    managed = [c["folder"] for c in checklist["categories"]] + [checklist_mod.UNASSIGNED_FOLDER]
    for name in managed:
        folder = root / name
        if folder.exists():
            shutil.rmtree(folder)
    root.mkdir(parents=True, exist_ok=True)

    items_by_cat: dict[str, list[dict]] = {}
    for item in checklist["items"]:
        items_by_cat.setdefault(item["category"], []).append(item)

    for cat in checklist["categories"]:
        folder = root / cat["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        for item in items_by_cat.get(cat["code"], []):
            for row in cov["items"].get(item["code"], []):
                path_row = core_db.first_path(conn, row["id"])
                if not path_row:
                    continue
                link = folder / f"{item['code']}__{row['id']:05d}_{row['display_name']}"
                if not link.exists():
                    _link(path_row["path"], link)

    if cov["unassigned"]:
        folder = root / checklist_mod.UNASSIGNED_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        for doc in cov["unassigned"]:
            path_row = core_db.first_path(conn, doc["id"])
            if not path_row:
                continue
            _link(path_row["path"], folder / f"{doc['id']:05d}_{doc['display_name']}")


def _doc_cell(rows) -> str:
    parts = []
    for r in rows:
        period = f" ({r['period']})" if r["period"] else ""
        parts.append(f"doc {r['id']} `{r['display_name']}`{period}")
    return "; ".join(parts)


def _write_index(project: Project, checklist: dict, cov: dict) -> None:
    out = ["# Dataroom index", ""]
    if checklist.get("jurisdiction"):
        out.append(f"**Jurisdiction:** {checklist['jurisdiction']}")
        out.append("")
    items_by_cat: dict[str, list[dict]] = {}
    for item in checklist["items"]:
        items_by_cat.setdefault(item["category"], []).append(item)
    for cat in checklist["categories"]:
        items = items_by_cat.get(cat["code"], [])
        if not items:
            continue
        out += [f"## {cat['folder']} — {cat['title']}", ""]
        for item in items:
            rows = cov["items"].get(item["code"], [])
            status = STATUS_LABEL["covered" if rows else "missing"]
            out.append(f"- **{item['code']} {item['title']}** ({item['importance']}) — {status}")
            for r in rows:
                period = f", {r['period']}" if r["period"] else ""
                note = f" — {r['note']}" if r["note"] else ""
                out.append(f"  - doc {r['id']} `{r['display_name']}`"
                           f" (dated {r['doc_date'] or '?'}{period}){note}")
        out.append("")
    if cov["unassigned"]:
        out += [f"## {checklist_mod.UNASSIGNED_FOLDER}", "",
                "Relevant documents that matched no checklist item:", ""]
        for doc in cov["unassigned"]:
            out.append(f"- doc {doc['id']} `{doc['display_name']}`: {doc['summary'] or ''}")
        out.append("")
    (dataroom_dir(project) / "INDEX.md").write_text("\n".join(out), encoding="utf-8")


def _snapshot(conn) -> dict | None:
    from ... import db as core_db

    raw = core_db.get_meta(conn, SNAPSHOT_META_KEY)
    return json.loads(raw) if raw else None


def _save_snapshot(conn, checklist: dict, cov: dict) -> None:
    from ... import db as core_db

    covered = sorted(code for code, rows in cov["items"].items() if rows)
    core_db.set_meta(conn, SNAPSHOT_META_KEY, json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "covered": covered,
    }))


def _write_gap_report(project: Project, checklist: dict, cov: dict,
                      previous: dict | None) -> None:
    items_by_code = {item["code"]: item for item in checklist["items"]}
    counts = _counts(checklist, cov)
    now = time.strftime("%Y-%m-%d %H:%M")

    out = ["# Dataroom gap report", "", f"_Generated {now}_", ""]
    if checklist.get("jurisdiction"):
        out.append(f"**Jurisdiction:** {checklist['jurisdiction']}")
        out.append("")

    out += ["## Summary", ""]
    for level in checklist_mod.IMPORTANCE_LEVELS:
        covered, total = counts[level]
        out.append(f"- {level.capitalize()} items covered: **{covered} / {total}**")
    if cov["unmapped"]:
        out.append(f"- {cov['unmapped']} relevant documents not mapped yet — "
                   "run `docpipe dataroom map` then `docpipe status --wait`.")
    out.append("")

    if previous:
        covered_now = {code for code, rows in cov["items"].items() if rows}
        covered_before = set(previous.get("covered") or [])
        closed = sorted(covered_now - covered_before)
        regressed = sorted(covered_before - covered_now)
        out += [f"## Changes since last build ({previous.get('at', '?')})", ""]
        if closed:
            out.append("Gaps closed:")
            for code in closed:
                item = items_by_code.get(code)
                out.append(f"- {code} {item['title'] if item else ''}")
        if regressed:
            out.append("No longer covered (items or mappings removed):")
            for code in regressed:
                item = items_by_code.get(code)
                out.append(f"- {code} {item['title'] if item else ''}")
        if not closed and not regressed:
            out.append("No coverage changes.")
        out.append("")

    missing = {level: [] for level in checklist_mod.IMPORTANCE_LEVELS}
    for item in checklist["items"]:
        if not cov["items"].get(item["code"]):
            level = item.get("importance") or "required"
            missing.setdefault(level, []).append(item)

    headings = {
        "required": "Missing required documents (request these)",
        "recommended": "Missing recommended documents",
        "conditional": "Missing conditional documents (confirm whether they apply)",
    }
    for level in checklist_mod.IMPORTANCE_LEVELS:
        if not missing[level]:
            continue
        out += [f"## {headings[level]}", ""]
        for item in missing[level]:
            note = f" _{item['note']}_" if item.get("note") else ""
            out.append(f"- **{item['code']} {item['title']}** — "
                       f"{item.get('description') or ''}{note}")
        out.append("")

    out += ["## Full checklist status", ""]
    out.append("| code | item | importance | status | documents |")
    out.append("|---|---|---|---|---|")
    cat_titles = {c["code"]: c["title"] for c in checklist["categories"]}
    last_cat = None
    for item in checklist["items"]:
        if item["category"] != last_cat:
            last_cat = item["category"]
            out.append(f"| **{cat_titles.get(last_cat, last_cat)}** | | | | |")
        rows = cov["items"].get(item["code"], [])
        status = STATUS_LABEL["covered" if rows else "missing"]
        out.append(f"| {item['code']} | {item['title']} | {item['importance']} "
                   f"| {status} | {_doc_cell(rows)} |")
    out.append("")

    out += [
        "## Follow-up pass",
        "",
        "Once the missing documents have been added to the source folder:",
        "",
        "1. `docpipe run` — scans, converts, triages and extracts only the new files",
        "2. `docpipe review apply --accept-all` (or review the new verdicts first)",
        "3. `docpipe dataroom map` then `docpipe status --wait` — maps only the new documents",
        "4. `docpipe dataroom build` — regenerates the dataroom and reports; this",
        "   report will then show which gaps were closed since this build.",
        "",
    ]
    path = project.report_dir / "dataroom_gap_report.md"
    path.write_text("\n".join(out), encoding="utf-8")


def build(project: Project, cfg: Config) -> dict:
    """Build the dataroom folder, INDEX.md and the gap report. Returns paths."""
    checklist = checklist_mod.load(project)
    conn = project.connect()
    try:
        db.sync_items(conn, checklist)
        cov = coverage(conn, checklist)
        previous = _snapshot(conn)
        _materialise(project, conn, checklist, cov)
        _write_index(project, checklist, cov)
        _write_gap_report(project, checklist, cov, previous)
        _save_snapshot(conn, checklist, cov)
        conn.commit()
    finally:
        conn.close()
    return {
        "dataroom": dataroom_dir(project),
        "index": dataroom_dir(project) / "INDEX.md",
        "gap_report": project.report_dir / "dataroom_gap_report.md",
    }


def status_lines(project: Project) -> list[str]:
    """Short coverage summary for the CLI."""
    checklist = checklist_mod.load(project)
    conn = project.connect()
    try:
        cov = coverage(conn, checklist)
    finally:
        conn.close()
    counts = _counts(checklist, cov)
    lines = []
    for level in checklist_mod.IMPORTANCE_LEVELS:
        covered, total = counts[level]
        lines.append(f"{level.capitalize()} items covered: {covered}/{total}")
    lines.append(f"Documents matched to no item: {len(cov['unassigned'])}")
    lines.append(f"Relevant documents awaiting mapping: {cov['unmapped']}")
    return lines


def investor_report(project: Project, cfg: Config, max_chars: int = 600_000):
    """One non-batch LLM call producing the investor-facing report."""
    checklist = checklist_mod.load(project)
    conn = project.connect()
    try:
        db.sync_items(conn, checklist)
        cov = coverage(conn, checklist)
        counts = _counts(checklist, cov)

        blocks = ["DATAROOM COVERAGE:"]
        for level in checklist_mod.IMPORTANCE_LEVELS:
            covered, total = counts[level]
            blocks.append(f"{level} checklist items evidenced: {covered}/{total}")
        for item in checklist["items"]:
            rows = cov["items"].get(item["code"], [])
            if rows:
                blocks.append(f"{item['code']} {item['title']}: " + _doc_cell(rows))
            else:
                blocks.append(f"{item['code']} {item['title']} ({item['importance']}): MISSING")

        blocks.append("")
        blocks.append("DOCUMENTS:")
        for doc in conn.execute(
            "SELECT * FROM documents WHERE relevant = 1"
            " ORDER BY (doc_date IS NULL), doc_date, id"
        ).fetchall():
            blocks.append(
                f"[doc {doc['id']}: {doc['display_name']}] date={doc['doc_date'] or '?'} "
                f"type={doc['doc_type'] or doc['triage_category'] or '?'} :: {doc['summary'] or ''}"
            )

        blocks.append("")
        blocks.append("FACTS (chronological):")
        for f in conn.execute(
            "SELECT f.*, d.display_name FROM facts f JOIN documents d ON d.id = f.document_id"
            " ORDER BY (f.as_of_date IS NULL), f.as_of_date, f.id"
        ).fetchall():
            blocks.append(
                f"{f['as_of_date'] or '????-??-??'} | {f['entity']} | {f['attribute']} = {f['value']}"
                f" [doc {f['document_id']}: {f['display_name']}]"
            )
    finally:
        conn.close()

    context = "\n".join(blocks)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...[truncated]"

    client = gemini.get_client()
    text = gemini.generate(client, cfg.synthesis_model,
                           prompts.INVESTOR_REPORT_SYSTEM, context)
    path = dataroom_dir(project) / "INVESTOR_REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
