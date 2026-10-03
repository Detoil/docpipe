"""Final outputs: a deterministic markdown report assembled from the
database, plus an optional LLM-synthesised briefing with source citations."""

from __future__ import annotations

from pathlib import Path

from . import db, gemini, prompts
from .config import Config, Project


def build(project: Project, cfg: Config) -> Path:
    conn = project.connect()
    try:
        out = ["# docpipe report", "", f"**Goal:** {cfg.prompt}", ""]

        counts = db.status_counts(conn)
        total = sum(counts.values())
        n_paths = conn.execute("SELECT count(*) AS n FROM document_paths").fetchone()["n"]
        n_skipped = conn.execute("SELECT count(*) AS n FROM skipped_files").fetchone()["n"]
        n_facts = conn.execute("SELECT count(*) AS n FROM facts").fetchone()["n"]

        out += ["## Pipeline summary", ""]
        out.append(f"- Files seen at {n_paths} paths, deduplicated to {total} unique documents")
        out.append(f"- Skipped by local filters: {n_skipped}")
        for status, n in sorted(counts.items()):
            out.append(f"- {status}: {n}")
        out.append(f"- Facts extracted: {n_facts}")
        out.append("")

        failed = conn.execute(
            "SELECT id, display_name, convert_error FROM documents"
            " WHERE status = 'convert_failed' ORDER BY id"
        ).fetchall()
        if failed:
            out += ["## Conversion failures", ""]
            for doc in failed:
                out.append(f"- doc {doc['id']} `{doc['display_name']}`: {doc['convert_error']}")
            out.append("")

        out += ["## Relevant documents (by date)", ""]
        out.append("| id | date | type | file | summary |")
        out.append("|---|---|---|---|---|")
        docs = conn.execute(
            "SELECT * FROM documents WHERE relevant = 1"
            " ORDER BY (doc_date IS NULL), doc_date, id"
        ).fetchall()
        for doc in docs:
            summary = (doc["summary"] or "").replace("|", "\\|").replace("\n", " ")
            out.append(
                f"| {doc['id']} | {doc['doc_date'] or '?'} | {doc['doc_type'] or doc['triage_category'] or ''} "
                f"| {doc['display_name']} | {summary} |"
            )
        out.append("")

        out += ["## Current state (latest fact per entity/attribute)", ""]
        out.append("| entity | attribute | value | as of | source doc |")
        out.append("|---|---|---|---|---|")
        for f in conn.execute(
            "SELECT * FROM current_facts ORDER BY entity, attribute"
        ).fetchall():
            out.append(
                f"| {f['entity']} | {f['attribute']} | {f['value']} "
                f"| {f['as_of_date'] or '?'} | {f['document_id']} |"
            )
        out.append("")

        out += ["## Fact timeline", ""]
        out.append("| date | entity | attribute | value | source doc |")
        out.append("|---|---|---|---|---|")
        for f in conn.execute(
            "SELECT * FROM facts ORDER BY (as_of_date IS NULL), as_of_date, id"
        ).fetchall():
            out.append(
                f"| {f['as_of_date'] or '?'} | {f['entity']} | {f['attribute']} "
                f"| {f['value']} | {f['document_id']} |"
            )
        out.append("")

        irrelevant = conn.execute(
            "SELECT id, display_name, triage_reason FROM documents"
            " WHERE relevant = 0 ORDER BY id"
        ).fetchall()
        out += ["## Irrelevant documents", ""]
        for doc in irrelevant:
            out.append(f"- doc {doc['id']} `{doc['display_name']}`: {doc['triage_reason']}")
        out.append("")

        path = project.report_dir / "report.md"
        path.write_text("\n".join(out), encoding="utf-8")
        return path
    finally:
        conn.close()


def synthesise_briefing(project: Project, cfg: Config, max_chars: int = 600_000) -> Path:
    """One non-batch call that turns the facts DB into a narrative briefing."""
    conn = project.connect()
    try:
        blocks = ["DOCUMENTS:"]
        for doc in conn.execute(
            "SELECT * FROM documents WHERE relevant = 1"
            " ORDER BY (doc_date IS NULL), doc_date, id"
        ).fetchall():
            blocks.append(
                f"[doc {doc['id']}: {doc['display_name']}] date={doc['doc_date'] or '?'} "
                f"type={doc['doc_type'] or doc['triage_category'] or '?'} :: {doc['summary'] or ''}"
            )
        blocks.append("")
        blocks.append("FACTS (chronological):")
        for f in conn.execute(
            "SELECT f.*, d.display_name FROM facts f JOIN documents d ON d.id = f.document_id"
            " ORDER BY (f.as_of_date IS NULL), f.as_of_date, f.id"
        ).fetchall():
            blocks.append(
                f"{f['as_of_date'] or '????-??-??'} | {f['entity']} | {f['attribute']} = {f['value']}"
                f" [doc {f['document_id']}: {f['display_name']}]"
            )
        context = "\n".join(blocks)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...[truncated]"

        client = gemini.get_client()
        text = gemini.generate(
            client,
            cfg.synthesis_model,
            prompts.SYNTHESIS_SYSTEM.format(user_prompt=cfg.prompt),
            context,
        )
        path = project.report_dir / "briefing.md"
        path.write_text(text, encoding="utf-8")
        return path
    finally:
        conn.close()


def propose_schema(project: Project, cfg: Config, sample_size: int = 40) -> Path:
    """One non-batch call proposing extraction guidance from triaged samples."""
    conn = project.connect()
    try:
        docs = conn.execute(
            "SELECT * FROM documents WHERE relevant = 1 ORDER BY RANDOM() LIMIT ?",
            (sample_size,),
        ).fetchall()
        if not docs:
            raise RuntimeError("No relevant documents yet — run triage first.")
        sample = "\n".join(
            f"- {d['display_name']} | {d['triage_category'] or '?'} | {d['summary'] or ''}"
            for d in docs
        )
        client = gemini.get_client()
        text = gemini.generate(
            client,
            cfg.synthesis_model,
            prompts.SCHEMA_PROPOSAL_SYSTEM.format(user_prompt=cfg.prompt),
            f"Sample of relevant documents:\n{sample}",
        )
        project.guidance_path.write_text(text, encoding="utf-8")
        return project.guidance_path
    finally:
        conn.close()
