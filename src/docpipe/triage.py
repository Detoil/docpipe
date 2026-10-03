"""Batch pass 1: cheap relevance triage with a flash-lite model.

Each converted document becomes one batch request containing its filename,
folder path, modification time and a truncated excerpt — enough to judge
relevance without paying for full documents. Scans/images attach the
original file instead of text.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import db, gemini, prompts
from .config import Config, Project

TRIAGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "relevant": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
        "category": {"type": "STRING"},
        "doc_date": {"type": "STRING", "nullable": True},
        "summary": {"type": "STRING"},
    },
    "required": ["relevant", "reason", "category", "summary"],
}


def pending_docs(conn):
    return conn.execute(
        "SELECT * FROM documents WHERE status = 'converted' AND relevant IS NULL ORDER BY id"
    ).fetchall()


def _excerpt(markdown: str, limit: int) -> tuple[str, str]:
    text = (markdown or "").strip()
    if len(text) <= limit:
        return text, ""
    return text[:limit], " (truncated)"


def build_request(conn, cfg: Config, doc, client=None) -> dict:
    path_row = db.first_path(conn, doc["id"])
    meta = {
        "name": doc["display_name"],
        "rel_path": path_row["rel_path"] if path_row else doc["display_name"],
        "mtime": doc["mtime"],
    }
    parts: list[dict]
    if doc["needs_ocr"] and client is not None and path_row:
        uri, mime = gemini.upload_for_inference(client, Path(path_row["path"]))
        parts = [
            {"text": prompts.TRIAGE_USER_FILE.format(**meta)},
            {"file_data": {"file_uri": uri, "mime_type": mime}},
        ]
    else:
        excerpt, note = _excerpt(doc["markdown"], cfg.triage_excerpt_chars)
        parts = [{"text": prompts.TRIAGE_USER.format(excerpt=excerpt, truncated_note=note, **meta)}]

    return {
        "key": f"doc-{doc['id']}",
        "request": {
            "contents": [{"role": "user", "parts": parts}],
            "system_instruction": {
                "parts": [{"text": prompts.TRIAGE_SYSTEM.format(user_prompt=cfg.prompt)}]
            },
            "generation_config": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": TRIAGE_SCHEMA,
            },
        },
    }


def submit(project: Project, cfg: Config) -> list[str]:
    """Build and submit triage batch job(s). Returns submitted job names."""
    conn = project.connect()
    try:
        if db.pending_jobs(conn, "triage"):
            raise RuntimeError(
                "A triage batch job is already pending; run `docpipe status` first."
            )
        docs = pending_docs(conn)
        if not docs:
            return []
        client = gemini.get_client()
        requests = [build_request(conn, cfg, doc, client) for doc in docs]

        job_names = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for n, start in enumerate(range(0, len(requests), cfg.max_batch_requests)):
            chunk = requests[start : start + cfg.max_batch_requests]
            jsonl_path = project.batches_dir / f"triage-{ts}-{n}.jsonl"
            gemini.write_jsonl(jsonl_path, chunk)
            name = gemini.submit_batch(client, cfg.triage_model, jsonl_path, f"docpipe-triage-{ts}-{n}")
            db.create_job(conn, name, "triage", cfg.triage_model, "JOB_STATE_PENDING",
                          len(chunk), str(jsonl_path))
            # See mapping.submit: commit each job as it is accepted.
            conn.commit()
            job_names.append(name)
        conn.commit()
        return job_names
    finally:
        conn.close()


def ingest_lines(conn, lines: list[dict]) -> dict:
    """Apply triage verdicts from batch result lines to the database."""
    stats = {"ok": 0, "errors": 0}
    for line in lines:
        key = line.get("key") or line.get("metadata", {}).get("key", "")
        if not str(key).startswith("doc-"):
            stats["errors"] += 1
            continue
        doc_id = int(str(key).split("-", 1)[1])
        data = gemini.parse_json_response(line.get("response") or {})
        if data is None or "relevant" not in data:
            # Leave the document in 'converted' so the next triage submit retries it.
            stats["errors"] += 1
            continue
        db.update_document(
            conn, doc_id,
            status="triaged",
            relevant=1 if data.get("relevant") else 0,
            triage_reason=(data.get("reason") or "")[:500],
            triage_category=(data.get("category") or "")[:100],
            doc_date=data.get("doc_date"),
            summary=data.get("summary"),
        )
        stats["ok"] += 1
    conn.commit()
    return stats
