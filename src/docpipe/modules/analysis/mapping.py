"""Analysis batch pass: one structured read per document.

The request carries the *profile*, not the data. A workbook with 200,000
rows costs the same handful of tokens as a two-page memo, which is what
makes running the whole corpus affordable. Only family representatives are
sent by default — the eight saved versions of one budget are read once.

Everything is incremental: documents already mapped are skipped, so a later
run after new files arrive processes only the new ones.
"""

from __future__ import annotations

import time
from pathlib import Path

from ... import db as core_db
from ... import gemini
from ...config import Config, Project
from . import db, profile as profile_mod, prompts

STAGE = "analysis_map"

# The profile carries the structure, so the excerpt only has to carry voice
# and detail. Tabular files need very little of it.
EXCERPT_CHARS = {"tabular": 2_000, "deck": 6_000, "text": 8_000}
MAX_PROFILE_CHARS = 12_000


def pending_docs(conn, include_variants: bool = False):
    """Profiled, relevant, reviewed documents that have not been mapped."""
    db.ensure_schema(conn)
    q = ("SELECT d.*, p.kind, p.profile, p.family_id, p.representative"
         " FROM documents d JOIN analysis_profiles p ON p.document_id = d.id"
         " WHERE d.relevant = 1 AND d.reviewed = 1"
         "   AND d.status IN ('triaged', 'extracted')"
         "   AND d.id NOT IN (SELECT document_id FROM analysis_signals)")
    if not include_variants:
        q += " AND p.representative = 1"
    return conn.execute(q + " ORDER BY d.id").fetchall()


def _variant_note(conn, doc) -> str:
    if not doc["family_id"]:
        return "none"
    members = [m for m in db.family_members(conn, doc["family_id"])
               if m["document_id"] != doc["id"]]
    if not members:
        return "none"
    names = "; ".join(f"{m['display_name']} ({m['mtime'] or '?'})" for m in members[:5])
    more = f" and {len(members) - 5} more" if len(members) > 5 else ""
    return f"{len(members)} — {names}{more}"


def build_request(conn, cfg: Config, doc, system: str, client=None) -> dict:
    path_row = core_db.first_path(conn, doc["id"])
    kind = doc["kind"]
    rendered = profile_mod.render_profile(kind, db.load_profile(doc))
    meta = {
        "name": doc["display_name"],
        "rel_path": path_row["rel_path"] if path_row else doc["display_name"],
        "ext": doc["ext"] or "?",
        "kind": kind,
        "mtime": doc["mtime"] or "?",
        "doc_date": doc["doc_date"] or "?",
        "summary": doc["summary"] or "",
        "variants": _variant_note(conn, doc),
        "profile": rendered[:MAX_PROFILE_CHARS],
    }

    if doc["needs_ocr"] and client is not None and path_row:
        uri, mime = gemini.upload_for_inference(client, Path(path_row["path"]))
        parts = [
            {"text": prompts.MAP_USER_FILE.format(**meta)},
            {"file_data": {"file_uri": uri, "mime_type": mime}},
        ]
    else:
        limit = EXCERPT_CHARS.get(kind, 6_000)
        content = (doc["markdown"] or "").strip()
        note = ""
        if len(content) > limit:
            content, note = content[:limit], " (truncated)"
        parts = [{"text": prompts.MAP_USER.format(excerpt=content, truncated_note=note, **meta)}]

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


def submit(project: Project, cfg: Config, include_variants: bool = False) -> list[str]:
    """Build and submit the analysis batch job(s). Returns job names."""
    conn = project.connect()
    try:
        if core_db.pending_jobs(conn, STAGE):
            raise RuntimeError(
                "An analysis batch job is already pending; run `docpipe status` first."
            )
        docs = pending_docs(conn, include_variants=include_variants)
        if not docs:
            return []
        client = gemini.get_client()
        system = prompts.MAP_SYSTEM.format(user_prompt=cfg.prompt)
        requests = [build_request(conn, cfg, doc, system, client) for doc in docs]

        job_names = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for n, start in enumerate(range(0, len(requests), cfg.max_batch_requests)):
            chunk = requests[start : start + cfg.max_batch_requests]
            jsonl_path = project.batches_dir / f"analysis-map-{ts}-{n}.jsonl"
            gemini.write_jsonl(jsonl_path, chunk)
            name = gemini.submit_batch(client, cfg.extract_model, jsonl_path,
                                       f"docpipe-analysis-map-{ts}-{n}")
            core_db.create_job(conn, name, STAGE, cfg.extract_model,
                               "JOB_STATE_PENDING", len(chunk), str(jsonl_path))
            # Commit per chunk: a submitted job is already costing money, so it
            # must survive a failure on a later chunk rather than being rolled
            # back into an untracked orphan.
            conn.commit()
            job_names.append(name)
        return job_names
    finally:
        conn.close()


def _clean(value, limit: int = 300) -> str:
    return str(value).strip()[:limit]


def _items(payload: dict) -> list[dict]:
    """Flatten a signal into the rows the corpus-wide roll-ups aggregate."""
    items: list[dict] = []
    for process in payload.get("processes") or []:
        name = _clean(process.get("name") or "", 200)
        if not name:
            continue
        detail_bits = [b for b in (process.get("cadence"), process.get("trigger"),
                                   process.get("evidence")) if b]
        items.append({
            "kind": "process",
            "name": name,
            "detail": _clean(" — ".join(str(b) for b in detail_bits), 400) or None,
            "extra": {
                "trigger": process.get("trigger"),
                "actors": [_clean(a, 80) for a in (process.get("actors") or [])][:10],
                "systems": [_clean(s, 80) for s in (process.get("systems") or [])][:10],
                "cadence": process.get("cadence"),
                "outputs": [_clean(o, 120) for o in (process.get("outputs") or [])][:10],
                "evidence": process.get("evidence"),
            },
        })
    for system in payload.get("systems") or []:
        name = _clean(system.get("name") or "", 120)
        if name:
            items.append({"kind": "system", "name": name,
                          "detail": _clean(system.get("used_for") or "", 200) or None})
    for entity in payload.get("entities") or []:
        name = _clean(entity.get("name") or "", 200)
        if name:
            items.append({"kind": "entity", "name": name,
                          "detail": _clean(entity.get("role") or "", 200) or None,
                          "extra": {"type": entity.get("type")}})
    for metric in payload.get("metrics") or []:
        name = _clean(metric.get("name") or "", 200)
        if name:
            items.append({"kind": "metric", "name": name,
                          "detail": _clean(metric.get("location") or "", 200) or None,
                          "extra": {"unit": metric.get("unit"),
                                    "period": metric.get("period")}})
    for issue in payload.get("issues") or []:
        name = _clean(issue.get("name") or "", 200)
        if name:
            items.append({"kind": "issue", "name": name,
                          "detail": _clean(issue.get("detail") or "", 400) or None})
    for question in payload.get("questions") or []:
        text = _clean(question, 400)
        if text:
            items.append({"kind": "question", "name": text})
    return items


def _coerce(payload: dict) -> dict:
    """Keep the payload to the shape the pack renders, so a stray field or a
    string where a number belongs never reaches the outputs."""
    value = payload.get("analysis_value")
    try:
        value = min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        value = None
    return {
        "purpose": _clean(payload.get("purpose") or "", 600),
        "domain": _clean(payload.get("domain") or "", 60).lower() or None,
        "record_type": _clean(payload.get("record_type") or "", 120) or None,
        "period_start": _clean(payload.get("period_start") or "", 20) or None,
        "period_end": _clean(payload.get("period_end") or "", 20) or None,
        "analysis_value": value,
        "joins": [_clean(j, 80) for j in (payload.get("joins") or [])][:20],
        "processes": payload.get("processes") or [],
        "systems": payload.get("systems") or [],
        "entities": payload.get("entities") or [],
        "metrics": payload.get("metrics") or [],
        "issues": payload.get("issues") or [],
        "questions": payload.get("questions") or [],
    }


def ingest_lines(conn, lines: list[dict]) -> dict:
    """Apply analysis signals from batch result lines to the database."""
    db.ensure_schema(conn)
    stats = {"ok": 0, "errors": 0, "items": 0}
    for line in lines:
        key = line.get("key") or line.get("metadata", {}).get("key", "")
        if not str(key).startswith("doc-"):
            stats["errors"] += 1
            continue
        doc_id = int(str(key).split("-", 1)[1])
        data = gemini.parse_json_response(line.get("response") or {})
        if data is None or "purpose" not in data:
            # Leave the document unmapped so the next submit retries it.
            stats["errors"] += 1
            continue
        payload = _coerce(data)
        stats["items"] += db.save_signal(conn, doc_id, payload, _items(payload))
        stats["ok"] += 1
    conn.commit()
    return stats
