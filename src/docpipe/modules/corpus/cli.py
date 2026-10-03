"""CLI for the corpus module: ``docpipe corpus ...``.

Turns a processed docpipe project into a Corpus Ingest Contract file that
the ``detoil_corpus seed`` loader can load into its Notion registers.

  docpipe corpus status                 # what an emit would produce
  docpipe corpus emit                   # write report/corpus.json
  docpipe corpus validate corpus.json   # check any file against the contract

Load it downstream with:

  NOTION_TOKEN=… python -m detoil_corpus seed corpus.json
"""

from __future__ import annotations

from pathlib import Path

import click

from ... import gemini
from ...config import Project
from . import emit as emit_mod
from . import extract as extract_mod
from . import schema


def _project(ctx) -> Project:
    proj = Project(ctx.obj["root"])
    if not proj.exists():
        raise click.ClickException(
            f"No docpipe project at {proj.root}. Run `docpipe init` first."
        )
    return proj


@click.group(name="corpus")
def cli():
    """Emit a Corpus Ingest Contract file for the corpus loader's Notion registers."""


@cli.command("extract")
@click.pass_context
def corpus_extract(ctx):
    """Submit the batch job that fills registers from document content."""
    proj = _project(ctx)
    try:
        job_names = extract_mod.submit(proj, proj.config)
    except (RuntimeError, gemini.GeminiError) as exc:
        raise click.ClickException(str(exc))
    if not job_names:
        click.echo(
            "Nothing to extract: no relevant, reviewed documents are unextracted.\n"
            "(Documents must pass triage and review first — see `docpipe run`.)"
        )
        return
    for name in job_names:
        click.echo(f"Submitted corpus extraction batch job: {name}")
    click.echo("Track it with: docpipe status --wait, then: docpipe corpus emit")


@cli.command("emit")
@click.option("--output", "-o", type=click.Path(dir_okay=False),
              help="Where to write the corpus JSON (default: report/corpus.json).")
@click.option("--relevant-only", is_flag=True,
              help="Index only documents triaged as relevant (default: all).")
@click.pass_context
def corpus_emit(ctx, output, relevant_only):
    """Build report/corpus.json from the database (document_index + extracted registers)."""
    proj = _project(ctx)
    try:
        summary = emit_mod.emit(
            proj, output=Path(output) if output else None, relevant_only=relevant_only
        )
    except schema.CorpusError as exc:
        raise click.ClickException(f"Refusing to write a non-conforming corpus: {exc}")

    if not summary["registers"]:
        click.echo(
            "No documents to index yet — run `docpipe scan` (and ideally the "
            "full pipeline) first. Nothing written."
        )
        return
    click.echo(f"Wrote {summary['path']}")
    for register, n in summary["registers"].items():
        click.echo(f"  {register}: {n} rows")
    skipped = {f"{r}.{f}" for r, f in summary["unknown_fields"]}
    if skipped:
        click.echo(f"  (fields not in the contract, dropped: {', '.join(sorted(skipped))})")
    click.echo("Load it with: NOTION_TOKEN=… python -m detoil_corpus seed "
               f"{summary['path'].name}")


@cli.command("validate")
@click.argument("corpus_file", type=click.Path(exists=True, dir_okay=False))
def corpus_validate(corpus_file):
    """Check a corpus JSON file against the contract (fail-closed rules)."""
    text = Path(corpus_file).read_text(encoding="utf-8")
    try:
        result = schema.loads_and_validate(text)
    except schema.CorpusError as exc:
        raise click.ClickException(f"Would NOT load: {exc}")
    click.echo(f"OK — {result.rows} rows across {len(result.registers)} register(s):")
    for register, n in result.registers.items():
        click.echo(f"  {register}: {n} rows")
    if result.unknown_fields:
        skipped = {f"{r}.{f}" for r, f in result.unknown_fields}
        click.echo(f"Warning — fields not in the contract (dropped on load): "
                   f"{', '.join(sorted(skipped))}")


@cli.command("status")
@click.pass_context
def corpus_status(ctx):
    """Show what an emit would produce (document counts by doc_type)."""
    proj = _project(ctx)
    for line in emit_mod.status_lines(proj):
        click.echo(line)
