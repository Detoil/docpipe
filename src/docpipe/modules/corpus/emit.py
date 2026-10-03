"""Build a Corpus Ingest Contract document from the pipeline database.

The one register docpipe can populate faithfully and completely is
``document_index`` — the contract's stated "primary Drive-extraction target".
Every other register needs structured source data (a reg number, a supplier
uid, a Xero snapshot) that the generic triage/extract pass does not produce,
and §5 of the contract is explicit: *don't invent placeholder values*. So we
emit exactly what we can derive deterministically, and omit the rest.

Identity (§2 — the single most important thing to get right): docpipe
deduplicates the source folder by SHA-256 content hash, so the stable,
run-to-run identity of a document is that hash. We mint
``file_id = "sha256:<hexdigest>"``; re-emitting the same corpus upserts in
place instead of duplicating.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from ...config import Project
from . import db, schema
from . import extract as extract_mod

FILE_ID_PREFIX = "sha256:"

DEFAULT_OUTPUT_NAME = "corpus.json"

# Ordered doc_type classification into the contract's fixed select set. First
# keyword to appear (in the document's name/category/type/summary) wins; the
# order resolves overlaps like "bank statement" vs "statement of account".
DOC_TYPE_KEYWORDS = [
    ("invoice", ("invoice", "tax invoice")),
    ("receipt", ("receipt", "proof of payment", "remittance")),
    ("statement", ("bank statement", "statement", "financial statements")),
    ("contract", ("contract", "agreement", "lease", "nda",
                  "memorandum of understanding", "engagement letter", "sla")),
]
DEFAULT_DOC_TYPE = "other"


def classify_doc_type(*signals: str) -> str:
    """Map free-text doc/triage signals onto the document_index select set."""
    haystack = " ".join(s for s in signals if s).lower()
    for doc_type, keywords in DOC_TYPE_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return doc_type
    return DEFAULT_DOC_TYPE


def folder_path(rel_path: str | None) -> str:
    """The folder a document lives in, contract-style (``/legal/2026``)."""
    if not rel_path:
        return "/"
    parent = PurePosixPath(rel_path.replace("\\", "/")).parent
    text = str(parent)
    if text in (".", "", "/"):
        return "/"
    return "/" + text.lstrip("/")


def _document_row(conn, doc) -> dict:
    from ... import db as core_db

    path_row = core_db.first_path(conn, doc["id"])
    rel_path = path_row["rel_path"] if path_row else None
    doc_type = classify_doc_type(
        doc["display_name"], doc["doc_type"], doc["triage_category"], doc["summary"]
    )
    row = {
        "file_id": FILE_ID_PREFIX + doc["sha256"],
        "filename": doc["display_name"],
        "doc_type": doc_type,
        "folder_path": folder_path(rel_path),
    }
    if doc["mtime"]:
        row["last_updated"] = doc["mtime"]
    # Omit None-valued fields rather than emitting nulls (§5: omit what you
    # don't have); the load would skip them anyway.
    return row


def _has_content(row: dict, key_field: str) -> bool:
    return any(k != key_field for k in row)


def _merge_data(records: list[dict]) -> dict:
    """Field-by-field merge; records must be ordered low->high priority so the
    highest-confidence, latest-document non-null value wins."""
    merged: dict = {}
    for rec in records:
        for field_name, value in rec["data"].items():
            if value not in (None, ""):
                merged[field_name] = value
    return merged


def _sanitized_row(register: str, key_field: str, natural_key,
                   merged: dict) -> dict | None:
    """Build a row keeping only values that will actually load (§4), keyed by
    the producer-side natural key. Returns None if the key is missing."""
    if not natural_key:
        return None
    row: dict = {key_field: natural_key}
    for field_name, value in merged.items():
        if field_name == key_field:
            continue
        clean = schema.coerce_or_none(register, field_name, value)
        if clean is not None:
            row[field_name] = clean
    return row


def _canonical_registration_number(conn) -> str | None:
    """The registration number to key the singleton company rows on: the
    highest-confidence, latest one seen across the identity/finance records."""
    candidates = []
    for register in ("company_identity", "financial_profile"):
        for rec in db.records_for(conn, register):
            if rec["natural_key"]:
                candidates.append(((rec["confidence"] or 0.0), rec["document_id"],
                                   rec["natural_key"]))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[-1][2]


def _singleton_row(conn, target, canonical_reg) -> dict | None:
    records = db.records_for(conn, target.register)
    if not records:
        return None
    row = _sanitized_row(target.register, target.key_field, canonical_reg,
                         _merge_data(records))
    if row is None:
        return None
    # financial_profile with only a reg number carries no information — skip it;
    # a bare company_identity still meaningfully identifies the company.
    if target.register == "financial_profile" and not _has_content(row, target.key_field):
        return None
    return row


def _record_rows(conn, target) -> list[dict]:
    groups: dict[str, list] = {}
    order: list[str] = []
    for rec in db.records_for(conn, target.register):
        nk = rec["natural_key"]
        if not nk:
            continue
        if nk not in groups:
            groups[nk] = []
            order.append(nk)
        groups[nk].append(rec)

    rows = []
    for nk in sorted(order):
        row = _sanitized_row(target.register, target.key_field, nk,
                             _merge_data(groups[nk]))
        if row and _has_content(row, target.key_field):
            rows.append(row)
    return rows


def build_extracted_registers(conn) -> dict:
    """Merge/deduplicate corpus_records into the contract's registers."""
    out: dict = {}
    canonical_reg = _canonical_registration_number(conn)
    for target in extract_mod.TARGETS:
        if target.singleton:
            row = _singleton_row(conn, target, canonical_reg)
            rows = [row] if row else []
        else:
            rows = _record_rows(conn, target)
        if rows:  # §1: only emit a register you have data for
            out[target.register] = rows
    return out


def build_document(project: Project, relevant_only: bool = False,
                   include_extracted: bool = True) -> dict:
    """Assemble the corpus document: document_index plus any extracted registers."""
    conn = project.connect()
    try:
        where = ""
        if relevant_only:
            # Only judged-relevant documents; an unjudged doc has relevant=NULL.
            where = " WHERE relevant = 1"
        docs = conn.execute(
            f"SELECT * FROM documents{where} ORDER BY id"
        ).fetchall()
        index = [_document_row(conn, doc) for doc in docs]
        extracted = build_extracted_registers(conn) if include_extracted else {}
    finally:
        conn.close()

    document: dict = {}
    if index:  # §1: only emit a register you have data for
        document["document_index"] = index
    for target in extract_mod.TARGETS:  # stable, contract-ordered output
        if target.register in extracted:
            document[target.register] = extracted[target.register]
    return document


def emit(project: Project, output: Path | None = None,
         relevant_only: bool = False) -> dict:
    """Build, validate and write the corpus file. Returns a small summary.

    Validation is fail-closed: if the document would not load, nothing is
    written (mirroring the contract's whole-file abort behaviour).
    """
    document = build_document(project, relevant_only=relevant_only)
    result = schema.validate_document(document)  # raises CorpusError on violation

    out_path = Path(output) if output else project.report_dir / DEFAULT_OUTPUT_NAME
    if not document:
        # Nothing to emit — don't leave a stale/empty file behind.
        return {"path": out_path, "registers": {}, "rows": 0, "unknown_fields": []}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)
    out_path.write_text(text + "\n", encoding="utf-8")

    return {
        "path": out_path,
        "registers": dict(result.registers),
        "rows": result.rows,
        "unknown_fields": result.unknown_fields,
    }


def status_lines(project: Project) -> list[str]:
    """Short summary of what an emit would produce."""
    conn = project.connect()
    try:
        total = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        relevant = conn.execute(
            "SELECT count(*) AS n FROM documents WHERE relevant = 1"
        ).fetchone()["n"]
        extracted = db.extracted_count(conn)
    finally:
        conn.close()

    all_doc = build_document(project, relevant_only=False)
    breakdown: dict[str, int] = {}
    for row in all_doc.get("document_index", []):
        breakdown[row["doc_type"]] = breakdown.get(row["doc_type"], 0) + 1

    lines = [
        f"Documents registered: {total} ({relevant} judged relevant)",
        f"Documents corpus-extracted: {extracted}",
        f"document_index rows (all documents): {len(all_doc.get('document_index', []))}",
    ]
    for doc_type in ("contract", "invoice", "statement", "receipt", "other"):
        if breakdown.get(doc_type):
            lines.append(f"  {doc_type}: {breakdown[doc_type]}")
    extracted_registers = {r: len(v) for r, v in all_doc.items() if r != "document_index"}
    if extracted_registers:
        lines.append("Extracted registers:")
        for register, n in extracted_registers.items():
            lines.append(f"  {register}: {n} rows")
    elif extracted:
        lines.append("Extracted registers: none emittable yet "
                     "(e.g. no registration number found to key the company rows).")
    return lines
