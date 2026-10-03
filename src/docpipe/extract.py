"""Batch pass 2: extract structured facts from reviewed, relevant documents.

Facts are stored append-only: every document contributes the assertions it
makes, dated; the current_facts view resolves "latest value" per
(entity, attribute) deterministically in SQL instead of letting the model
update rows.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import db, gemini, prompts
from .config import Config, Project

FACT_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "entity": {"type": "STRING"},
        "entity_type": {"type": "STRING"},
        "attribute": {"type": "STRING"},
        "value": {"type": "STRING"},
        "as_of_date": {"type": "STRING", "nullable": True},
        "confidence": {"type": "NUMBER"},
        "quote": {"type": "STRING"},
    },
    "required": ["entity", "attribute", "value"],
}

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "doc_type": {"type": "STRING"},
        "doc_date": {"type": "STRING", "nullable": True},
        "summary": {"type": "STRING"},
        "facts": {"type": "ARRAY", "items": FACT_ITEM_SCHEMA},
    },
    "required": ["summary", "facts"],
}


def pending_docs(conn):
    return conn.execute(
        "SELECT * FROM documents WHERE status = 'triaged' AND relevant = 1 AND reviewed = 1"
        " ORDER BY id"
    ).fetchall()


def _system_prompt(project: Project, cfg: Config) -> str:
    guidance = ""
    if project.guidance_path.exists():
        text = project.guidance_path.read_text().strip()
        if text:
            guidance = f"\nExtraction guidance for this corpus:\n{text}\n"
    return prompts.EXTRACT_SYSTEM.format(user_prompt=cfg.prompt, guidance_section=guidance)


def build_request(conn, cfg: Config, doc, system: str, client=None) -> dict:
    path_row = db.first_path(conn, doc["id"])
    meta = {
        "name": doc["display_name"],
        "rel_path": path_row["rel_path"] if path_row else doc["display_name"],
    }
    if doc["needs_ocr"] and client is not None and path_row:
        uri, mime = gemini.upload_for_inference(client, Path(path_row["path"]))
        parts = [
            {"text": prompts.EXTRACT_USER_FILE.format(**meta)},
            {"file_data": {"file_uri": uri, "mime_type": mime}},
        ]
    else:
        content = (doc["markdown"] or "").strip()
        note = ""
        if len(content) > cfg.extract_max_chars:
            content = content[: cfg.extract_max_chars]
            note = " (truncated)"
        parts = [{"text": prompts.EXTRACT_USER.format(content=content, truncated_note=note, **meta)}]

    return {
        "key": f"doc-{doc['id']}",
        "request": {
            "contents": [{"role": "user", "parts": parts}],
            "system_instruction": {"parts": [{"text": system}]},
            "generation_config": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": EXTRACT_SCHEMA,
            },
        },
    }


def submit(project: Project, cfg: Config) -> list[str]:
    conn = project.connect()
    try:
        if db.pending_jobs(conn, "extract"):
            raise RuntimeError(
                "An extract batch job is already pending; run `docpipe status` first."
            )
        docs = pending_docs(conn)
        if not docs:
            return []
        client = gemini.get_client()
        system = _system_prompt(project, cfg)
        requests = [build_request(conn, cfg, doc, system, client) for doc in docs]

        job_names = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for n, start in enumerate(range(0, len(requests), cfg.max_batch_requests)):
            chunk = requests[start : start + cfg.max_batch_requests]
            jsonl_path = project.batches_dir / f"extract-{ts}-{n}.jsonl"
            gemini.write_jsonl(jsonl_path, chunk)
            name = gemini.submit_batch(client, cfg.extract_model, jsonl_path, f"docpipe-extract-{ts}-{n}")
            db.create_job(conn, name, "extract", cfg.extract_model, "JOB_STATE_PENDING",
                          len(chunk), str(jsonl_path))
            # See mapping.submit: commit each job as it is accepted.
            conn.commit()
            job_names.append(name)
        conn.commit()
        return job_names
    finally:
        conn.close()


def ingest_lines(conn, lines: list[dict]) -> dict:
    stats = {"ok": 0, "errors": 0, "facts": 0}
    for line in lines:
        key = line.get("key") or line.get("metadata", {}).get("key", "")
        if not str(key).startswith("doc-"):
            stats["errors"] += 1
            continue
        doc_id = int(str(key).split("-", 1)[1])
        data = gemini.parse_json_response(line.get("response") or {})
        if data is None or "facts" not in data:
            db.update_document(conn, doc_id, status="extract_failed")
            stats["errors"] += 1
            continue

        # Re-extraction replaces this document's assertions (idempotent ingest).
        conn.execute("DELETE FROM facts WHERE document_id = ?", (doc_id,))
        for fact in data.get("facts") or []:
            if not (fact.get("entity") and fact.get("attribute") and fact.get("value") is not None):
                continue
            db.insert_fact(
                conn, doc_id,
                entity=str(fact["entity"]).strip(),
                attribute=str(fact["attribute"]).strip(),
                value=str(fact["value"]).strip(),
                entity_type=fact.get("entity_type"),
                as_of_date=fact.get("as_of_date"),
                confidence=fact.get("confidence"),
                quote=fact.get("quote"),
            )
            stats["facts"] += 1

        fields = {"status": "extracted", "doc_type": data.get("doc_type"),
                  "summary": data.get("summary")}
        if data.get("doc_date"):
            fields["doc_date"] = data["doc_date"]
        db.update_document(conn, doc_id, **fields)
        stats["ok"] += 1
    conn.commit()
    return stats
