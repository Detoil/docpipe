"""docpipe command-line interface.

Pipeline: init -> scan -> convert -> triage -> status --wait ->
review export -> (human edits review.csv) -> review apply ->
extract -> status --wait -> report.

`docpipe run` chains the stages and stops at the human review gate.
Every stage is idempotent and resumable: state lives in pipeline.db.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from . import convert as convert_mod
from . import db
from . import extract as extract_mod
from . import gemini
from . import pipeline
from . import report as report_mod
from . import review as review_mod
from . import scan as scan_mod
from . import triage as triage_mod
from .config import Config, Project


def _project(ctx) -> Project:
    proj = Project(ctx.obj["root"])
    if not proj.exists():
        raise click.ClickException(
            f"No docpipe project at {proj.root}. Run `docpipe init` first "
            "(or pass --project / set DOCPIPE_PROJECT)."
        )
    return proj


@click.group(invoke_without_command=True)
@click.option(
    "--project", "-p", "root", default="docpipe_project", envvar="DOCPIPE_PROJECT",
    show_default=True, help="Project directory holding the database and outputs.",
)
@click.pass_context
def main(ctx, root):
    """Triage folders of documents and extract facts with the Gemini Batch API.

    Run with no command for the guided, settings-file-driven mode."""
    ctx.obj = {"root": Path(root)}
    if ctx.invoked_subcommand is None:
        from . import guided

        guided.run(Path(root))


@main.command()
@click.pass_context
def start(ctx):
    """Guided mode: creates/reads docpipe_settings.txt and walks every step."""
    from . import guided

    guided.run(ctx.obj["root"])


@main.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--prompt", help="What you want from these documents (the intention prompt).")
@click.option("--prompt-file", type=click.Path(exists=True, dir_okay=False),
              help="Read the intention prompt from a file instead.")
@click.option("--include-images", is_flag=True,
              help="Also process image files (sent to Gemini directly).")
@click.pass_context
def init(ctx, source_dir, prompt, prompt_file, include_images):
    """Create a project for SOURCE_DIR with your intention prompt."""
    if prompt_file:
        prompt = Path(prompt_file).read_text().strip()
    if not prompt:
        raise click.ClickException("Provide --prompt or --prompt-file.")
    proj = Project(ctx.obj["root"])
    if proj.exists():
        raise click.ClickException(f"Project already exists at {proj.root}.")
    cfg = Config(
        source_dir=str(Path(source_dir).resolve()),
        prompt=prompt,
        include_images=include_images,
    )
    proj.create(cfg)
    click.echo(f"Created project at {proj.root}")
    click.echo("Next: docpipe run   (or step-by-step: scan, convert, triage, ...)")


@main.command()
@click.pass_context
def scan(ctx):
    """Walk the source folder: hash, deduplicate and register files."""
    proj = _project(ctx)
    stats = scan_mod.run(proj, proj.config)
    click.echo(
        f"Scanned {stats['seen']} files: {stats['new']} new unique documents, "
        f"{stats['duplicates']} duplicate paths, {stats['skipped']} skipped, "
        f"{stats['already_known']} already known."
    )


@main.command()
@click.option("--verbose", "-v", is_flag=True, help="Print each file as it converts.")
@click.option("--workers", "-j", type=int, default=None,
              help="Files to convert at once (default: one per CPU core, capped at 8).")
@click.pass_context
def convert(ctx, verbose, workers):
    """Convert registered documents to markdown with MarkItDown."""
    proj = _project(ctx)
    progress = click.echo if verbose else None
    used = workers if workers and workers > 0 else convert_mod.resolve_workers(proj.config)
    click.echo(f"Converting with {used} worker{'s' if used != 1 else ''}...")
    stats = convert_mod.run(proj, proj.config, progress=progress, workers=workers)
    click.echo(
        f"Converted {stats['converted']} documents "
        f"({stats['needs_ocr']} flagged for Gemini file upload, "
        f"{stats['failed']} failed)."
    )


@main.command()
@click.pass_context
def triage(ctx):
    """Submit the relevance-triage batch job(s)."""
    proj = _project(ctx)
    _submit(proj, "triage")


@main.command()
@click.pass_context
def extract(ctx):
    """Submit the fact-extraction batch job(s) for reviewed, relevant docs."""
    proj = _project(ctx)
    conn = proj.connect()
    try:
        n = len(extract_mod.pending_docs(conn))
        unreviewed = conn.execute(
            "SELECT count(*) AS n FROM documents WHERE status = 'triaged' AND reviewed = 0"
        ).fetchone()["n"]
    finally:
        conn.close()
    if n == 0 and unreviewed > 0:
        raise click.ClickException(
            f"{unreviewed} triaged documents are not reviewed yet. Run "
            "`docpipe review export`, edit review.csv, then `docpipe review apply` "
            "(or `docpipe review apply --accept-all`)."
        )
    _submit(proj, "extract")


def _submit(proj: Project, stage: str) -> bool:
    mod = triage_mod if stage == "triage" else extract_mod
    try:
        job_names = mod.submit(proj, proj.config)
    except (RuntimeError, gemini.GeminiError) as exc:
        raise click.ClickException(str(exc))
    if not job_names:
        click.echo(f"Nothing to {stage}: no documents in the right state.")
        return False
    for name in job_names:
        click.echo(f"Submitted {stage} batch job: {name}")
    click.echo("Track it with: docpipe status --wait")
    return True


@main.command()
@click.option("--wait", is_flag=True, help="Poll until all pending batch jobs finish.")
@click.pass_context
def status(ctx, wait):
    """Show pipeline state; poll pending batch jobs and ingest results."""
    proj = _project(ctx)
    while True:
        try:
            pending, _ = pipeline.poll_once(proj, echo=click.echo)
        except gemini.GeminiError as exc:
            raise click.ClickException(str(exc))
        if not wait or pending == 0:
            break
        time.sleep(proj.config.poll_interval_seconds)

    conn = proj.connect()
    try:
        counts = db.status_counts(conn)
        n_facts = conn.execute("SELECT count(*) AS n FROM facts").fetchone()["n"]
    finally:
        conn.close()
    click.echo("Documents: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    click.echo(f"Facts: {n_facts}")


@main.command()
@click.pass_context
def jobs(ctx):
    """List all batch jobs ever submitted by this project."""
    proj = _project(ctx)
    conn = proj.connect()
    try:
        rows = conn.execute("SELECT * FROM batch_jobs ORDER BY id").fetchall()
    finally:
        conn.close()
    if not rows:
        click.echo("No batch jobs submitted yet.")
        return
    for j in rows:
        click.echo(
            f"{j['created_at']}  {j['stage']:<8} {j['state']:<22} "
            f"{j['request_count']:>5} reqs  {j['job_name']}"
        )


@main.group()
def review():
    """Export triage verdicts for human review, then apply edits."""


@review.command("export")
@click.pass_context
def review_export(ctx):
    """Write review.csv and review.html with the triage verdicts."""
    proj = _project(ctx)
    csv_path, html_path = review_mod.export(proj, proj.config)
    click.echo(f"Wrote {csv_path}")
    click.echo(f"Wrote {html_path}")
    click.echo("Edit the 'relevant' column (1/0) in review.csv, then run: docpipe review apply")


@review.command("apply")
@click.option("--csv", "csv_path", type=click.Path(exists=True, dir_okay=False),
              help="Apply a specific CSV (default: the exported review.csv).")
@click.option("--accept-all", is_flag=True,
              help="Accept the model's verdicts as-is without an edited CSV.")
@click.pass_context
def review_apply(ctx, csv_path, accept_all):
    """Apply reviewed verdicts and build the irrelevant-documents folder."""
    proj = _project(ctx)
    try:
        stats = review_mod.apply(
            proj, proj.config,
            csv_path=Path(csv_path) if csv_path else None,
            accept_all=accept_all,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"Reviewed {stats['reviewed']} documents ({stats['changed']} verdicts changed); "
        f"{stats['irrelevant']} irrelevant documents linked in {proj.irrelevant_dir}."
    )
    click.echo("Next: docpipe extract")


@main.command()
@click.pass_context
def schema(ctx):
    """Propose extraction guidance from a sample of relevant documents."""
    proj = _project(ctx)
    try:
        path = report_mod.propose_schema(proj, proj.config)
    except (RuntimeError, gemini.GeminiError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Wrote {path} — review/edit it; extraction prompts will include it.")


@main.command()
@click.option("--brief/--no-brief", default=True,
              help="Also synthesise the narrative briefing with the LLM.")
@click.pass_context
def report(ctx, brief):
    """Build report.md (and briefing.md) from the database."""
    proj = _project(ctx)
    path = report_mod.build(proj, proj.config)
    click.echo(f"Wrote {path}")
    if brief:
        try:
            briefing = report_mod.synthesise_briefing(proj, proj.config)
            click.echo(f"Wrote {briefing}")
        except gemini.GeminiError as exc:
            click.echo(f"Skipped briefing: {exc}", err=True)


@main.command()
@click.argument("query")
@click.pass_context
def search(ctx, query):
    """Full-text search across all converted documents."""
    proj = _project(ctx)
    conn = proj.connect()
    try:
        if not db.has_fts(conn):
            raise click.ClickException("This SQLite build has no FTS5 support.")
        rows = conn.execute(
            "SELECT d.id, d.display_name,"
            " snippet(documents_fts, 0, '[', ']', '…', 12) AS snip"
            " FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid"
            " WHERE documents_fts MATCH ? LIMIT 25",
            (query,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        click.echo(f"doc {r['id']:>5}  {r['display_name']}: {r['snip']}")
    if not rows:
        click.echo("No matches.")


@main.command()
@click.pass_context
def estimate(ctx):
    """Rough batch cost estimate before submitting (edit prices in config.json)."""
    proj = _project(ctx)
    for line in pipeline.estimate_lines(proj):
        click.echo(line)


@main.command()
@click.option("--no-wait", is_flag=True, help="Submit batch jobs but don't poll for them.")
@click.pass_context
def run(ctx, no_wait):
    """Run the whole pipeline, stopping at the human review gate."""
    proj = _project(ctx)
    cfg = proj.config

    def wait_for_jobs():
        if no_wait:
            click.echo("Batch job pending — re-run `docpipe run` (or `docpipe status --wait`) later.")
            sys.exit(0)
        while True:
            pending, _ = pipeline.poll_once(proj, echo=click.echo)
            if pending == 0:
                return
            time.sleep(cfg.poll_interval_seconds)

    conn = proj.connect()
    try:
        total = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
    finally:
        conn.close()
    if total == 0:
        click.echo("== scan ==")
        ctx.invoke(scan)

    conn = proj.connect()
    try:
        discovered = conn.execute(
            "SELECT count(*) AS n FROM documents WHERE status = 'discovered'"
        ).fetchone()["n"]
    finally:
        conn.close()
    if discovered:
        click.echo("== convert ==")
        ctx.invoke(convert)

    # Triage anything converted but unjudged.
    conn = proj.connect()
    try:
        to_triage = len(triage_mod.pending_docs(conn))
        triage_jobs_pending = bool(db.pending_jobs(conn, "triage"))
    finally:
        conn.close()
    if to_triage and not triage_jobs_pending:
        click.echo("== triage (batch) ==")
        _submit(proj, "triage")
        triage_jobs_pending = True
    if triage_jobs_pending:
        wait_for_jobs()

    # Human review gate.
    conn = proj.connect()
    try:
        unreviewed = conn.execute(
            "SELECT count(*) AS n FROM documents WHERE status = 'triaged' AND reviewed = 0"
        ).fetchone()["n"]
    finally:
        conn.close()
    if unreviewed:
        click.echo("== review gate ==")
        csv_path, html_path = review_mod.export(proj, cfg)
        click.echo(f"Triage verdicts exported to:\n  {csv_path}\n  {html_path}")
        click.echo(
            "Review them (flip the 'relevant' column where the model was wrong),\n"
            "then run `docpipe review apply` (or `docpipe review apply --accept-all`)\n"
            "and `docpipe run` again to continue."
        )
        return

    # Extraction.
    conn = proj.connect()
    try:
        to_extract = len(extract_mod.pending_docs(conn))
        extract_jobs_pending = bool(db.pending_jobs(conn, "extract"))
    finally:
        conn.close()
    if to_extract and not extract_jobs_pending:
        click.echo("== extract (batch) ==")
        _submit(proj, "extract")
        extract_jobs_pending = True
    if extract_jobs_pending:
        wait_for_jobs()

    click.echo("== report ==")
    ctx.invoke(report, brief=True)
    click.echo("Done.")


def _register_modules() -> None:
    from . import modules

    for group in modules.cli_groups():
        main.add_command(group)


_register_modules()


if __name__ == "__main__":
    main()
