"""Analysis tables, created idempotently inside the project's pipeline.db.

- analysis_profiles  one row per document: the local structural profile
                     (sheets/columns, slide outline, heading outline), its
                     family signature and its size in estimated tokens
- analysis_signals   one row per mapped document: the model's structured
                     read of what the document is and what it is evidence of
- analysis_items     the parts of a signal worth aggregating across the whole
                     corpus (processes, entities, systems, metrics, questions)

Both the profile and the signal tables are keyed by document_id, so re-running
a stage replaces a document's rows rather than duplicating them.
"""

from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_profiles (
    document_id    INTEGER PRIMARY KEY REFERENCES documents(id),
    kind           TEXT NOT NULL,
    profile        TEXT NOT NULL,
    signature      TEXT,
    family_id      INTEGER,
    representative INTEGER NOT NULL DEFAULT 1,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    profiled_at    TEXT
);

CREATE TABLE IF NOT EXISTS analysis_signals (
    document_id  INTEGER PRIMARY KEY REFERENCES documents(id),
    payload      TEXT NOT NULL,
    purpose      TEXT,
    domain       TEXT,
    period_start TEXT,
    period_end   TEXT,
    value_score  REAL,
    mapped_at    TEXT
);

CREATE TABLE IF NOT EXISTS analysis_items (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    detail      TEXT,
    extra       TEXT
);

CREATE INDEX IF NOT EXISTS idx_analysis_items_doc ON analysis_items(document_id);
CREATE INDEX IF NOT EXISTS idx_analysis_items_kind ON analysis_items(kind, name);
CREATE INDEX IF NOT EXISTS idx_analysis_profiles_family ON analysis_profiles(family_id);
"""

ITEM_KINDS = ("process", "entity", "system", "metric", "question", "issue")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def save_profile(conn: sqlite3.Connection, document_id: int, kind: str,
                 profile: dict, signature: str, token_estimate: int) -> None:
    """Insert or replace a document's structural profile."""
    conn.execute(
        "INSERT INTO analysis_profiles (document_id, kind, profile, signature,"
        " token_estimate, profiled_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(document_id) DO UPDATE SET kind = excluded.kind,"
        " profile = excluded.profile, signature = excluded.signature,"
        " token_estimate = excluded.token_estimate, profiled_at = excluded.profiled_at",
        (document_id, kind, json.dumps(profile, ensure_ascii=False), signature,
         token_estimate, _now()),
    )


def profiles(conn: sqlite3.Connection, relevant_only: bool = True):
    q = ("SELECT p.*, d.display_name, d.ext, d.mtime, d.size_bytes, d.doc_date,"
         "       d.summary, d.relevant"
         " FROM analysis_profiles p JOIN documents d ON d.id = p.document_id")
    if relevant_only:
        q += " WHERE d.relevant = 1"
    return conn.execute(q + " ORDER BY p.document_id").fetchall()


def load_profile(row) -> dict:
    try:
        return json.loads(row["profile"])
    except (TypeError, ValueError):
        return {}


def assign_families(conn: sqlite3.Connection) -> dict:
    """Group profiled documents by identical signature into families and pick
    one representative each (newest, then largest).

    Families are what keep the LLM pass cheap: eight saved versions of the
    same workbook are one family, and only the representative is sent."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT p.document_id, p.signature, d.mtime, d.size_bytes"
        " FROM analysis_profiles p JOIN documents d ON d.id = p.document_id"
        " ORDER BY p.document_id"
    ).fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["signature"] or f"doc-{r['document_id']}", []).append(r)

    families = 0
    variants = 0
    for family_id, signature in enumerate(sorted(groups), start=1):
        members = sorted(
            groups[signature],
            key=lambda r: (r["mtime"] or "", r["size_bytes"] or 0, r["document_id"]),
            reverse=True,
        )
        families += 1
        for position, member in enumerate(members):
            is_rep = 1 if position == 0 else 0
            variants += 0 if is_rep else 1
            conn.execute(
                "UPDATE analysis_profiles SET family_id = ?, representative = ?"
                " WHERE document_id = ?",
                (family_id, is_rep, member["document_id"]),
            )
    return {"families": families, "variants": variants}


def family_members(conn: sqlite3.Connection, family_id: int):
    return conn.execute(
        "SELECT p.document_id, p.representative, d.display_name, d.mtime"
        " FROM analysis_profiles p JOIN documents d ON d.id = p.document_id"
        " WHERE p.family_id = ? ORDER BY p.representative DESC, d.mtime DESC",
        (family_id,),
    ).fetchall()


def save_signal(conn: sqlite3.Connection, document_id: int, payload: dict,
                items: list[dict]) -> int:
    """Replace a document's signal and its aggregated items (idempotent)."""
    conn.execute(
        "INSERT INTO analysis_signals (document_id, payload, purpose, domain,"
        " period_start, period_end, value_score, mapped_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(document_id) DO UPDATE SET payload = excluded.payload,"
        " purpose = excluded.purpose, domain = excluded.domain,"
        " period_start = excluded.period_start, period_end = excluded.period_end,"
        " value_score = excluded.value_score, mapped_at = excluded.mapped_at",
        (document_id, json.dumps(payload, ensure_ascii=False),
         payload.get("purpose"), payload.get("domain"),
         payload.get("period_start"), payload.get("period_end"),
         payload.get("analysis_value"), _now()),
    )
    conn.execute("DELETE FROM analysis_items WHERE document_id = ?", (document_id,))
    for item in items:
        conn.execute(
            "INSERT INTO analysis_items (document_id, kind, name, detail, extra)"
            " VALUES (?, ?, ?, ?, ?)",
            (document_id, item["kind"], item["name"], item.get("detail"),
             json.dumps(item["extra"], ensure_ascii=False) if item.get("extra") else None),
        )
    return len(items)


def items_of_kind(conn: sqlite3.Connection, kind: str, relevant_only: bool = True):
    q = ("SELECT i.*, d.display_name FROM analysis_items i"
         " JOIN documents d ON d.id = i.document_id WHERE i.kind = ?")
    if relevant_only:
        q += " AND d.relevant IS NOT 0"
    return conn.execute(q + " ORDER BY i.name COLLATE NOCASE, i.document_id",
                        (kind,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    ensure_schema(conn)

    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()["n"]

    return {
        "relevant": scalar(
            "SELECT count(*) AS n FROM documents WHERE relevant IS NOT 0"),
        "profiled": scalar(
            "SELECT count(*) AS n FROM analysis_profiles p"
            " JOIN documents d ON d.id = p.document_id WHERE d.relevant IS NOT 0"),
        "families": scalar(
            "SELECT count(DISTINCT family_id) AS n FROM analysis_profiles"
            " WHERE family_id IS NOT NULL"),
        "representatives": scalar(
            "SELECT count(*) AS n FROM analysis_profiles p"
            " JOIN documents d ON d.id = p.document_id"
            " WHERE d.relevant IS NOT 0 AND p.representative = 1"),
        "mapped": scalar(
            "SELECT count(*) AS n FROM analysis_signals s"
            " JOIN documents d ON d.id = s.document_id WHERE d.relevant IS NOT 0"),
        "items": scalar("SELECT count(*) AS n FROM analysis_items"),
    }
