"""Corpus tables, created idempotently inside the project's pipeline.db.

- corpus_records  one row per (document, register, extracted record); the
                  natural key is computed producer-side at ingest, the
                  register content fields are stored as JSON
- corpus_docs     per-document extraction state, so re-running `corpus
                  extract` only submits documents not yet extracted

The batch pass (`corpus extract`) writes here; `corpus emit` reads here,
merging/deduplicating records by natural key into the final registers. This
separation mirrors the dataroom module (map = batch, build = deterministic).
"""

from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_records (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    register    TEXT NOT NULL,
    natural_key TEXT,
    data        TEXT NOT NULL,
    confidence  REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS corpus_docs (
    document_id  INTEGER PRIMARY KEY REFERENCES documents(id),
    extracted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_corpus_records_reg ON corpus_records(register);
CREATE INDEX IF NOT EXISTS idx_corpus_records_doc ON corpus_records(document_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def replace_doc_records(conn: sqlite3.Connection, document_id: int,
                        records: list[dict]) -> int:
    """Idempotent ingest: replace a document's records and mark it extracted.

    Each record is {register, natural_key, data (dict), confidence}.
    """
    ensure_schema(conn)
    conn.execute("DELETE FROM corpus_records WHERE document_id = ?", (document_id,))
    n = 0
    for rec in records:
        conn.execute(
            "INSERT INTO corpus_records (document_id, register, natural_key, data, confidence)"
            " VALUES (?, ?, ?, ?, ?)",
            (document_id, rec["register"], rec.get("natural_key"),
             json.dumps(rec["data"], ensure_ascii=False), rec.get("confidence")),
        )
        n += 1
    conn.execute(
        "INSERT INTO corpus_docs (document_id, extracted_at) VALUES (?, ?)"
        " ON CONFLICT(document_id) DO UPDATE SET extracted_at = excluded.extracted_at",
        (document_id, time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    return n


def records_for(conn: sqlite3.Connection, register: str) -> list[dict]:
    """All extracted records for a register, newest document last.

    Ordered by (confidence asc, document_id asc) so a straightforward
    field-by-field overwrite during merge lets the highest-confidence,
    latest-document value win.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT document_id, natural_key, data, confidence FROM corpus_records"
        " WHERE register = ?"
        " ORDER BY (confidence IS NULL), confidence, document_id",
        (register,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "document_id": r["document_id"],
            "natural_key": r["natural_key"],
            "data": json.loads(r["data"]),
            "confidence": r["confidence"],
        })
    return out


def counts_by_register(conn: sqlite3.Connection) -> dict:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT register, count(*) AS n FROM corpus_records GROUP BY register"
    ).fetchall()
    return {r["register"]: r["n"] for r in rows}


def extracted_count(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    return conn.execute(
        "SELECT count(*) AS n FROM corpus_docs WHERE extracted_at IS NOT NULL"
    ).fetchone()["n"]
