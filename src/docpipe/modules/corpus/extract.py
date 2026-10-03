"""Targeted corpus-extraction batch pass (`corpus extract`).

One batch request per relevant document; the response is a register-shaped
object (schema generated from the contract specs, with select options baked in
as enums). We compute every natural key producer-side — §2b keys via the
documented hash, §2a supplier/customer/hmcp keys minted by slugifying the name
— so the model only ever supplies underlying facts, never identifiers.

Records land in ``corpus_records`` (idempotent per document); ``corpus emit``
merges and deduplicates them into the final registers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ... import db as core_db
from ... import gemini
from ...config import Config, Project
from . import db, prompts, schema

STAGE = "corpus_extract"

# rich_text fields the contract documents as carrying JSON (§3), surfaced in
# the prompt so structured values round-trip as JSON strings.
JSON_FIELDS = {
    "shareholders", "bank_details", "contact", "rate_card", "key_obligations",
    "alert_windows", "fired_windows", "outstanding_invoices", "outstanding_bills",
    "payload",
}


@dataclass(frozen=True)
class Target:
    register: str
    purpose: str
    singleton: bool = False      # merged into one company row (keyed by reg no.)
    mint_from: str | None = None  # §2a: slugify this field into the natural key

    @property
    def key_field(self) -> str:
        return schema.REGISTERS[self.register].key

    def emit_fields(self) -> list:
        """Fields the model supplies (everything except computed keys)."""
        spec = schema.REGISTERS[self.register]
        skip = set() if self.singleton else {self.key_field}
        return [f for f in spec.fields if f not in skip]


# The nine document-extractable registers. Excluded by design (not document
# targets per the contract): financial_snapshot (Xero sync), task_queue
# (internal work queue), audit_log (system-written, refused as a staging target).
TARGETS = [
    Target("company_identity",
           "The company's own legal identity — registration number, legal name, "
           "incorporation, shareholders, CIPC and B-BBEE status.", singleton=True),
    Target("financial_profile",
           "The company's tax and banking identifiers and accounting setup "
           "(income tax, VAT, PAYE, UIF numbers, bank details, financial year).",
           singleton=True),
    Target("staff_register",
           "Employees — one record per person."),
    Target("contract_register",
           "Contracts and agreements — one record per contract."),
    Target("compliance_calendar",
           "Statutory and compliance obligations with due dates."),
    Target("supplier_register",
           "Suppliers the company buys from — one record per supplier.",
           mint_from="name"),
    Target("customer_register",
           "Customers the company sells to — one record per customer.",
           mint_from="name"),
    Target("tool_access_register",
           "Software/tool access grants — one record per tool + person."),
    Target("hmcp_provider_register",
           "Outsourced service providers (payroll, accounting, legal).",
           mint_from="name"),
]

TARGETS_BY_REGISTER = {t.register: t for t in TARGETS}


def build_response_schema() -> dict:
    """The per-document response schema, generated from the register specs."""
    props: dict = {}
    for target in TARGETS:
        skip = () if target.singleton else (target.key_field,)
        obj = schema.gemini_object_schema(target.register, skip_fields=skip)
        if target.singleton:
            obj["nullable"] = True
            props[target.register] = obj
        else:
            props[target.register] = {"type": "ARRAY", "items": obj}
    return {"type": "OBJECT", "properties": props}


def system_prompt() -> str:
    guide = prompts.render_field_guide(TARGETS, JSON_FIELDS)
    return prompts.EXTRACT_SYSTEM.format(field_guide=guide)


def pending_docs(conn):
    """Relevant, reviewed documents not yet corpus-extracted."""
    db.ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM documents WHERE relevant = 1 AND reviewed = 1"
        " AND status IN ('triaged', 'extracted')"
        " AND id NOT IN (SELECT document_id FROM corpus_docs WHERE extracted_at IS NOT NULL)"
        " ORDER BY id"
    ).fetchall()


def _excerpt(markdown: str, limit: int) -> tuple[str, str]:
    text = (markdown or "").strip()
    if len(text) <= limit:
        return text, ""
    return text[:limit], " (truncated)"


def build_request(conn, cfg: Config, doc, system: str, response_schema: dict,
                  client=None) -> dict:
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
            {"text": prompts.EXTRACT_USER_FILE.format(**meta)},
            {"file_data": {"file_uri": uri, "mime_type": mime}},
        ]
    else:
        content, note = _excerpt(doc["markdown"], cfg.extract_max_chars)
        parts = [{"text": prompts.EXTRACT_USER.format(content=content, truncated_note=note, **meta)}]

    return {
        "key": f"doc-{doc['id']}",
        "request": {
            "contents": [{"role": "user", "parts": parts}],
            "system_instruction": {"parts": [{"text": system}]},
            "generation_config": {
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": response_schema,
            },
        },
    }


def submit(project: Project, cfg: Config) -> list[str]:
    """Build and submit extraction batch job(s). Returns submitted job names."""
    conn = project.connect()
    try:
        db.ensure_schema(conn)
        if core_db.pending_jobs(conn, STAGE):
            raise RuntimeError(
                "A corpus extraction batch job is already pending; "
                "run `docpipe status` first."
            )
        docs = pending_docs(conn)
        if not docs:
            return []
        client = gemini.get_client()
        system = system_prompt()
        response_schema = build_response_schema()
        requests = [build_request(conn, cfg, doc, system, response_schema, client)
                    for doc in docs]

        job_names = []
        ts = time.strftime("%Y%m%d-%H%M%S")
        for n, start in enumerate(range(0, len(requests), cfg.max_batch_requests)):
            chunk = requests[start : start + cfg.max_batch_requests]
            jsonl_path = project.batches_dir / f"corpus-extract-{ts}-{n}.jsonl"
            gemini.write_jsonl(jsonl_path, chunk)
            name = gemini.submit_batch(client, cfg.extract_model, jsonl_path,
                                       f"docpipe-corpus-extract-{ts}-{n}")
            core_db.create_job(conn, name, STAGE, cfg.extract_model,
                               "JOB_STATE_PENDING", len(chunk), str(jsonl_path))
            job_names.append(name)
        conn.commit()
        return job_names
    finally:
        conn.close()


def _natural_key(target: Target, obj: dict) -> str | None:
    """The natural key for one extracted record, computed producer-side."""
    if target.mint_from is not None:
        source = obj.get(target.mint_from)
        slug = schema.slugify(source) if source not in (None, "") else ""
        return slug or None
    # §2b: derive from the record's own source fields.
    return schema.derive_uid(target.register, obj) or None


def _record_has_content(obj: dict, key_field: str) -> bool:
    return any(v not in (None, "") for k, v in obj.items() if k != key_field)


def ingest_lines(conn, lines: list[dict]) -> dict:
    """Apply extraction results to corpus_records (idempotent per document)."""
    db.ensure_schema(conn)
    stats = {"ok": 0, "errors": 0, "records": 0, "skipped_no_key": 0}
    for line in lines:
        key = line.get("key") or line.get("metadata", {}).get("key", "")
        if not str(key).startswith("doc-"):
            stats["errors"] += 1
            continue
        doc_id = int(str(key).split("-", 1)[1])
        data = gemini.parse_json_response(line.get("response") or {})
        if data is None:
            # Leave the document unextracted so the next submit retries it.
            stats["errors"] += 1
            continue

        records = []
        for target in TARGETS:
            value = data.get(target.register)
            objs = []
            if target.singleton:
                if isinstance(value, dict):
                    objs = [value]
            elif isinstance(value, list):
                objs = [o for o in value if isinstance(o, dict)]

            for obj in objs:
                obj = dict(obj)
                confidence = obj.pop("confidence", None)
                if not _record_has_content(obj, target.key_field):
                    continue
                if target.singleton:
                    raw_key = obj.get(target.key_field)
                    natural_key = str(raw_key).strip() if raw_key not in (None, "") else None
                else:
                    natural_key = _natural_key(target, obj)
                    obj.pop(target.key_field, None)  # never trust a model-supplied key
                    if not natural_key:
                        stats["skipped_no_key"] += 1
                        continue
                records.append({
                    "register": target.register,
                    "natural_key": natural_key,
                    "data": obj,
                    "confidence": confidence,
                })

        stats["records"] += db.replace_doc_records(conn, doc_id, records)
        stats["ok"] += 1
    conn.commit()
    return stats
