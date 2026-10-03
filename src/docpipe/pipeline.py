"""Pipeline operations shared by the CLI commands and guided mode."""

from __future__ import annotations

import time

from . import db, extract, gemini, triage
from .config import Project


def stage_ingestors() -> dict:
    """Map each batch-job stage name to its ingest function, including
    stages contributed by plug-in modules."""
    from . import modules

    table = {"triage": triage.ingest_lines, "extract": extract.ingest_lines}
    table.update(modules.ingestors())
    return table


def poll_once(project: Project, echo=print) -> tuple[int, int]:
    """Refresh pending batch jobs, ingest finished ones.

    Returns (still_pending, ingested)."""
    conn = project.connect()
    try:
        jobs = db.pending_jobs(conn)
        if not jobs:
            return 0, 0
        client = gemini.get_client()
        ingestors = stage_ingestors()
        still_pending = 0
        ingested = 0
        for job in jobs:
            remote = gemini.get_job(client, job["job_name"])
            state = gemini.job_state(remote)
            db.update_job(conn, job["job_name"], state=state)
            if state == "JOB_STATE_SUCCEEDED":
                ingest = ingestors.get(job["stage"])
                if ingest is None:
                    db.update_job(conn, job["job_name"],
                                  error=f"no ingestor for stage '{job['stage']}'",
                                  completed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                    echo(f"{job['job_name']} ({job['stage']}): succeeded but no "
                         "ingestor is registered for this stage")
                    continue
                lines = gemini.fetch_result_lines(client, remote)
                stats = ingest(conn, lines)
                db.update_job(conn, job["job_name"],
                              completed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                ingested += 1
                echo(f"{job['job_name']} ({job['stage']}): ingested {stats}")
            elif state in gemini.COMPLETED_STATES:
                error = str(getattr(remote, "error", "") or state)
                db.update_job(conn, job["job_name"], error=error,
                              completed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                echo(f"{job['job_name']} ({job['stage']}): {state} - {error}")
            else:
                still_pending += 1
                echo(f"{job['job_name']} ({job['stage']}): {state}")
        conn.commit()
        return still_pending, ingested
    finally:
        conn.close()


def wait_for_jobs(project: Project, echo=print) -> None:
    """Poll until no batch jobs are pending. Safe to interrupt; jobs keep
    running server-side and the next poll picks them up."""
    interval = project.config.poll_interval_seconds
    while True:
        pending, _ = poll_once(project, echo)
        if pending == 0:
            return
        time.sleep(interval)


def estimate_lines(project: Project) -> list[str]:
    """Rough token/cost preview for the two batch passes."""
    cfg = project.config
    conn = project.connect()
    try:
        triage_docs = triage.pending_docs(conn)
        triage_in = sum(
            min(len(d["markdown"] or ""), cfg.triage_excerpt_chars) + 1500 for d in triage_docs
        ) // 4
        triage_out = len(triage_docs) * 150

        extract_docs = conn.execute(
            "SELECT markdown FROM documents WHERE relevant = 1 AND status != 'extracted'"
        ).fetchall()
        if not extract_docs:  # before triage, assume everything survives
            extract_docs = triage_docs
        extract_in = sum(
            min(len(d["markdown"] or ""), cfg.extract_max_chars) + 2500 for d in extract_docs
        ) // 4
        extract_out = len(extract_docs) * 1500
    finally:
        conn.close()

    prices = cfg.batch_prices_per_1m_tokens

    def cost(model, tokens_in, tokens_out):
        p = prices.get(model)
        if not p:
            return ""
        usd = tokens_in / 1e6 * p["input"] + tokens_out / 1e6 * p["output"]
        return f" = roughly ${usd:.2f}"

    return [
        f"Triage  ({cfg.triage_model}): {len(triage_docs)} docs, "
        f"~{triage_in:,} in / ~{triage_out:,} out tokens"
        + cost(cfg.triage_model, triage_in, triage_out),
        f"Extract ({cfg.extract_model}): {len(extract_docs)} docs, "
        f"~{extract_in:,} in / ~{extract_out:,} out tokens"
        + cost(cfg.extract_model, extract_in, extract_out),
        "Token counts are chars/4 estimates; prices come from "
        "batch_prices_per_1m_tokens in config.json - verify against "
        "https://ai.google.dev/gemini-api/docs/pricing",
    ]
