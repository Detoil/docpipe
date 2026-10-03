"""SQLite schema and helpers.

One database file is both the output artifact and the pipeline's state
store, which is what makes every stage resumable:

- documents       one row per *unique* file content (deduplicated by sha256)
- document_paths  every location a given content hash was found at
- skipped_files   files excluded by the local pass, with the reason
- facts           append-only assertions extracted from documents
- current_facts   view: latest assertion per (entity, attribute)
- batch_jobs      submitted Gemini batch jobs and their lifecycle
- meta            misc pipeline state (e.g. when review was applied)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Document status lifecycle:
# discovered -> converted | convert_failed
# converted  -> triaged          (relevant column set 0/1)
# triaged    -> extracted | extract_failed   (only relevant + reviewed docs)
SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    ext           TEXT,
    size_bytes    INTEGER,
    mtime         TEXT,
    status        TEXT NOT NULL DEFAULT 'discovered',
    markdown      TEXT,
    needs_ocr     INTEGER NOT NULL DEFAULT 0,
    convert_error TEXT,
    relevant      INTEGER,
    triage_reason   TEXT,
    triage_category TEXT,
    doc_date      TEXT,
    doc_type      TEXT,
    summary       TEXT,
    reviewed      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document_paths (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    path        TEXT UNIQUE NOT NULL,
    rel_path    TEXT
);

CREATE TABLE IF NOT EXISTS skipped_files (
    id     INTEGER PRIMARY KEY,
    path   TEXT UNIQUE NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    entity      TEXT NOT NULL,
    entity_type TEXT,
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    as_of_date  TEXT,
    confidence  REAL,
    quote       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id            INTEGER PRIMARY KEY,
    job_name      TEXT UNIQUE NOT NULL,
    stage         TEXT NOT NULL,
    model         TEXT,
    state         TEXT NOT NULL,
    request_count INTEGER,
    jsonl_path    TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity, attribute);
CREATE INDEX IF NOT EXISTS idx_facts_doc ON facts(document_id);
CREATE INDEX IF NOT EXISTS idx_paths_doc ON document_paths(document_id);

CREATE VIEW IF NOT EXISTS current_facts AS
SELECT id, document_id, entity, entity_type, attribute, value, as_of_date, confidence, quote
FROM (
    SELECT f.*, ROW_NUMBER() OVER (
        PARTITION BY f.entity, f.attribute
        ORDER BY (f.as_of_date IS NULL) ASC, f.as_of_date DESC, f.id DESC
    ) AS rn
    FROM facts f
)
WHERE rn = 1;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
    USING fts5(markdown, content='documents', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, markdown) VALUES (new.id, coalesce(new.markdown, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, markdown)
        VALUES ('delete', old.id, coalesce(old.markdown, ''));
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE OF markdown ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, markdown)
        VALUES ('delete', old.id, coalesce(old.markdown, ''));
    INSERT INTO documents_fts(rowid, markdown) VALUES (new.id, coalesce(new.markdown, ''));
END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            pass  # SQLite built without FTS5; full-text search is optional
        conn.commit()
    finally:
        conn.close()


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()
    return row is not None


def register_document(conn, sha256: str, display_name: str, ext: str,
                      size_bytes: int, mtime: str, needs_ocr: int = 0):
    """Insert a document if its content hash is new. Returns (id, created)."""
    row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    if row:
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO documents (sha256, display_name, ext, size_bytes, mtime, needs_ocr)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sha256, display_name, ext, size_bytes, mtime, needs_ocr),
    )
    return cur.lastrowid, True


def add_path(conn, document_id: int, path: str, rel_path: str) -> bool:
    """Record a location for a document. Returns True if the path was new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO document_paths (document_id, path, rel_path) VALUES (?, ?, ?)",
        (document_id, path, rel_path),
    )
    return cur.rowcount > 0


def add_skipped(conn, path: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO skipped_files (path, reason) VALUES (?, ?)", (path, reason)
    )


def update_document(conn, doc_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE documents SET {cols} WHERE id = ?", [*fields.values(), doc_id])


def first_path(conn, doc_id: int):
    return conn.execute(
        "SELECT path, rel_path FROM document_paths WHERE document_id = ? ORDER BY id LIMIT 1",
        (doc_id,),
    ).fetchone()


def all_paths(conn, doc_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT rel_path FROM document_paths WHERE document_id = ? ORDER BY id", (doc_id,)
    ).fetchall()
    return [r["rel_path"] for r in rows]


def status_counts(conn) -> dict:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM documents GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def insert_fact(conn, document_id: int, entity: str, attribute: str, value: str,
                entity_type: str | None = None, as_of_date: str | None = None,
                confidence: float | None = None, quote: str | None = None) -> None:
    conn.execute(
        "INSERT INTO facts (document_id, entity, entity_type, attribute, value,"
        " as_of_date, confidence, quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, entity, entity_type, attribute, value, as_of_date, confidence, quote),
    )


def create_job(conn, job_name: str, stage: str, model: str, state: str,
               request_count: int, jsonl_path: str) -> None:
    conn.execute(
        "INSERT INTO batch_jobs (job_name, stage, model, state, request_count, jsonl_path)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_name, stage, model, state, request_count, jsonl_path),
    )


def update_job(conn, job_name: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE batch_jobs SET {cols} WHERE job_name = ?", [*fields.values(), job_name])


def pending_jobs(conn, stage: str | None = None):
    q = "SELECT * FROM batch_jobs WHERE completed_at IS NULL"
    args: list = []
    if stage:
        q += " AND stage = ?"
        args.append(stage)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
