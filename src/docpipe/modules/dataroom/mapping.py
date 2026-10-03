"""Dataroom batch pass: classify each relevant document against the
checklist.

One batch request per relevant, reviewed document; the response says which
checklist items the document evidences (zero or more). Mapping is
incremental: documents already mapped are skipped, so after adding missing
documents to the source folder and re-running the core pipeline, another
`docpipe dataroom map` only processes the new ones.
"""

from __future__ import annotations

import time
from pathlib import Path

from ... import db as core_db
from ... import gemini
from ...config import Config, Project
from . import checklist as checklist_mod
from . import db, prompts

STAGE = "dataroom_map"
MAP_EXCERPT_CHARS = 12_000


def pending_docs(conn):
    """Relevant, reviewed documents that have not been mapped yet."""
    db.ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM documents WHERE relevant = 1 AND reviewed = 1"
        " AND status IN ('triaged', 'extracted')"
        " AND id NOT IN (SELECT document_id FROM dataroom_docs WHERE mapped_at IS NOT NULL)"
        " ORDER BY id"
    ).fetchall()


def _excerpt(markdown: str, limit: int = MAP_EXCERPT_CHARS) -> tuple[str, str]:
    text = (markdown or "").strip()
    if len(text) <= limit:
        return text, ""
    return text[:limit], " (truncated)"


def build_request(conn, cfg: Config, doc, system: str, client=None) -> dict:
    path_row = core_db.first_path(conn, doc["id"])
    meta = {
        "name": doc["display_name"],
        "rel_path": path_row["rel_path"] if path_row else doc["display_name"],
        "doc_type": doc["doc_type"] or doc["triage_category"] or "?",
        "doc_date": doc["doc_date"] or "?",
        "summary": doc["summary"] or "",
    }
    if doc["needs_ocr"] and client is not None and path_row:
        uri, mime = gemini.upload_for_inference(client, Path(path_row["path"]))
        parts = [
            {"text": prompts.MAP_USER_FILE.format(**meta)},
            {"file_data": {"file_uri": uri, "mime_type": mime}},
        ]
    else:
        excerpt, note = _excerpt(doc["markdown"])
        parts = [{"text": prompts.MAP_USER.format(excerpt=excerpt, truncated_note=note, **meta)}]

    return {
        "key": f"doc-{doc['id']}",
        "request": {
            "contents": [{"role": "user", "parts": parts}],
            "system_instruction": {"parts": [{"text": system}]},
            "generation_config": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": prompts.MAP_SCHEMA,
            },
        },
    }


def submit(project: Project, cfg: Config) -> list[str]:
    """Build and submit mapping batch job(s). Returns submitted job names."""
    checklist = checklist_mod.load(project)
    conn = project.connect()
    try:
        db.sync_items(conn, checklist)
        conn.commit()
        if core_db.pending_jobs(conn, STAGE):
            raise RuntimeError(
                "A dataroom mapping batch job is already pending; "
                "run `docpipe status` first."
            )
        docs = pending_docs(conn)
        if not docs:
            return []
        client = gemini.get_client()
        system = prompts.MAP_SYSTEM.format(checklist=checklist_mod.render(checklist))
        requests = [build_request(conn, cfg, doc, system, client) for doc in docs]

        job_names = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for n, start in enumerate(range(0, len(requests), cfg.max_batch_requests)):
            chunk = requests[start : start + cfg.max_batch_requests]
            jsonl_path = project.batches_dir / f"dataroom-map-{ts}-{n}.jsonl"
            gemini.write_jsonl(jsonl_path, chunk)
            name = gemini.submit_batch(client, cfg.extract_model, jsonl_path,
                                       f"docpipe-dataroom-map-{ts}-{n}")
            core_db.create_job(conn, name, STAGE, cfg.extract_model,
                               "JOB_STATE_PENDING", len(chunk), str(jsonl_path))
            job_names.append(name)
        conn.commit()
        return job_names
    finally:
        conn.close()


def ingest_lines(conn, lines: list[dict]) -> dict:
    """Apply mapping verdicts from batch result lines to the database."""
    db.ensure_schema(conn)
    valid = db.item_codes(conn)
    stats = {"ok": 0, "errors": 0, "matches": 0, "unknown_codes": 0}
    for line in lines:
        key = line.get("key") or line.get("metadata", {}).get("key", "")
        if not str(key).startswith("doc-"):
            stats["errors"] += 1
            continue
        doc_id = int(str(key).split("-", 1)[1])
        data = gemini.parse_json_response(line.get("response") or {})
        if data is None or "matches" not in data:
            # Leave the document unmapped so the next submit retries it.
            stats["errors"] += 1
            continue
        matches = []
        for m in data.get("matches") or []:
            code = str(m.get("code") or "").strip()
            if code not in valid:
                stats["unknown_codes"] += 1
                continue
            matches.append({
                "code": code,
                "confidence": m.get("confidence"),
                "period": m.get("period"),
                "note": (m.get("note") or "")[:300],
            })
        stats["matches"] += db.replace_doc_matches(conn, doc_id, matches)
        stats["ok"] += 1
    conn.commit()
    return stats
