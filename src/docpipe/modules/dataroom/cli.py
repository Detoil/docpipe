"""CLI for the dataroom module: `docpipe dataroom ...`.

Flow (after the core pipeline has run through review/extract):

  docpipe dataroom init [--country "South Africa"]   # editable checklist
  docpipe dataroom map                               # batch-classify docs
  docpipe status --wait                              # shared poll loop
  docpipe dataroom build                             # dataroom + reports

Re-running map+build later — after missing documents have been added and
pushed through `docpipe run` — processes only the new documents and reports
which gaps were closed.
"""

from __future__ import annotations

from pathlib import Path

import click

from ... import gemini
from ...config import Project
from . import build as build_mod
from . import checklist as checklist_mod
from . import mapping


def _project(ctx) -> Project:
    proj = Project(ctx.obj["root"])
    if not proj.exists():
        raise click.ClickException(
            f"No docpipe project at {proj.root}. Run `docpipe init` first."
        )
    return proj


@click.group(name="dataroom")
def cli():
    """Build an investor dataroom, investor report and gap report."""


@cli.command("init")
@click.option("--country", help="Localise the checklist for this country (one LLM call).")
@click.option("--force", is_flag=True, help="Overwrite an existing checklist file.")
@click.pass_context
def dataroom_init(ctx, country, force):
    """Write the editable due-diligence checklist (dataroom_checklist.json)."""
    proj = _project(ctx)
    path = checklist_mod.path_for(proj)
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists (use --force to overwrite).")
    checklist = checklist_mod.base_checklist()
    if country:
        try:
            stats = checklist_mod.localize(checklist, country, proj.config)
        except gemini.GeminiError as exc:
            raise click.ClickException(str(exc))
        click.echo(
            f"Localised for {country}: {stats['added']} items added, "
            f"{stats['notes']} local notes set."
        )
    checklist_mod.save(proj, checklist)
    click.echo(f"Wrote {path} — edit it freely (items, importance, notes).")
    click.echo("Next: docpipe dataroom map")


@cli.command("map")
@click.pass_context
def dataroom_map(ctx):
    """Submit the batch job mapping documents to checklist items."""
    proj = _project(ctx)
    try:
        job_names = mapping.submit(proj, proj.config)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
    except (RuntimeError, gemini.GeminiError) as exc:
        raise click.ClickException(str(exc))
    if not job_names:
        click.echo(
            "Nothing to map: no relevant, reviewed documents are unmapped.\n"
            "(New documents must pass triage and review first — see `docpipe run`.)"
        )
        return
    for name in job_names:
        click.echo(f"Submitted dataroom mapping batch job: {name}")
    click.echo("Track it with: docpipe status --wait, then: docpipe dataroom build")


@cli.command("build")
@click.option("--investor-report/--no-investor-report", default=True,
              help="Also synthesise the investor report with the LLM.")
@click.pass_context
def dataroom_build(ctx, investor_report):
    """Build the dataroom folder, INDEX.md, gap report and investor report."""
    proj = _project(ctx)
    try:
        paths = build_mod.build(proj, proj.config)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Built dataroom at {paths['dataroom']}")
    click.echo(f"Wrote {paths['index']}")
    click.echo(f"Wrote {paths['gap_report']}")
    if investor_report:
        try:
            path = build_mod.investor_report(proj, proj.config)
            click.echo(f"Wrote {path}")
        except gemini.GeminiError as exc:
            click.echo(f"Skipped investor report: {exc}", err=True)


@cli.command("status")
@click.pass_context
def dataroom_status(ctx):
    """Show checklist coverage and pending mapping work."""
    proj = _project(ctx)
    try:
        for line in build_mod.status_lines(proj):
            click.echo(line)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
