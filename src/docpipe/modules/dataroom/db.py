"""Dataroom tables, created idempotently inside the project's pipeline.db.

- dataroom_items  the checklist, synced from dataroom_checklist.json (the
                  JSON file is the source of truth; users edit it freely)
- dataroom_map    which documents evidence which checklist items
- dataroom_docs   per-document mapping state, so re-running `dataroom map`
                  only submits documents that have not been mapped yet
"""

from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataroom_items (
    code        TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    importance  TEXT NOT NULL DEFAULT 'required',
    note        TEXT,
    position    INTEGER
);

CREATE TABLE IF NOT EXISTS dataroom_map (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    item_code   TEXT NOT NULL,
    confidence  REAL,
    period      TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(document_id, item_code)
);

CREATE TABLE IF NOT EXISTS dataroom_docs (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id),
    mapped_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_dataroom_map_item ON dataroom_map(item_code);
CREATE INDEX IF NOT EXISTS idx_dataroom_map_doc ON dataroom_map(document_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def sync_items(conn: sqlite3.Connection, checklist: dict) -> None:
    """Replace dataroom_items with the current contents of the JSON file."""
    ensure_schema(conn)
    conn.execute("DELETE FROM dataroom_items")
    for pos, item in enumerate(checklist["items"]):
        conn.execute(
            "INSERT INTO dataroom_items (code, category, title, description,"
            " importance, note, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["code"], item["category"], item["title"],
             item.get("description"), item.get("importance") or "required",
             item.get("note"), pos),
        )


def item_codes(conn: sqlite3.Connection) -> set[str]:
    return {r["code"] for r in conn.execute("SELECT code FROM dataroom_items")}


def replace_doc_matches(conn: sqlite3.Connection, document_id: int,
                        matches: list[dict]) -> int:
    """Idempotent ingest: replace a document's matches and mark it mapped."""
    conn.execute("DELETE FROM dataroom_map WHERE document_id = ?", (document_id,))
    n = 0
    for m in matches:
        conn.execute(
            "INSERT OR IGNORE INTO dataroom_map"
            " (document_id, item_code, confidence, period, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (document_id, m["code"], m.get("confidence"), m.get("period"),
             m.get("note")),
        )
        n += 1
    conn.execute(
        "INSERT INTO dataroom_docs (document_id, mapped_at) VALUES (?, ?)"
        " ON CONFLICT(document_id) DO UPDATE SET mapped_at = excluded.mapped_at",
        (document_id, time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    return n
