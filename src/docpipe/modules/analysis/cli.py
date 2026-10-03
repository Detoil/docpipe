"""CLI for the analysis module: `docpipe analysis ...`.

Flow (after the core pipeline has triaged and reviewed):

  docpipe analysis profile      # local: structure + families, free
  docpipe analysis estimate     # what the batch pass will cost
  docpipe analysis map          # batch: one structured read per document
  docpipe status --wait         # shared poll loop ingests the results
  docpipe analysis pack         # assemble analysis_pack/ for the analyst LLM

Re-running after new files arrive processes only the new documents.
"""

from __future__ import annotations

import click

from ... import gemini
from ...config import Project
from . import db, mapping, pack as pack_mod, profile as profile_mod


def _project(ctx) -> Project:
    proj = Project(ctx.obj["root"])
    if not proj.exists():
        raise click.ClickException(
            f"No docpipe project at {proj.root}. Run `docpipe init` first."
        )
    return proj


@click.group(name="analysis")
def cli():
    """Prepare the corpus for analysis by a more capable model."""


@cli.command("profile")
@click.option("--verbose", "-v", is_flag=True, help="Print each file as it is profiled.")
@click.option("--all", "include_irrelevant", is_flag=True,
              help="Also profile documents triaged as irrelevant.")
@click.pass_context
def analysis_profile(ctx, verbose, include_irrelevant):
    """Profile document structure locally (no LLM, no API key)."""
    proj = _project(ctx)
    stats = profile_mod.run(proj, proj.config,
                            progress=click.echo if verbose else None,
                            relevant_only=not include_irrelevant)
    click.echo(
        f"Profiled {stats['profiled']} documents "
        f"({stats['tabular']} tabular, {stats['deck']} presentations, {stats['text']} text)."
    )
    click.echo(
        f"Grouped into {stats.get('families', 0)} families "
        f"({stats.get('variants', 0)} near-duplicate versions collapsed)."
    )
    click.echo("Next: docpipe analysis map")


@cli.command("estimate")
@click.option("--include-variants", is_flag=True,
              help="Cost of mapping every version, not just family representatives.")
@click.pass_context
def analysis_estimate(ctx, include_variants):
    """Token/cost preview for the analysis batch pass."""
    proj = _project(ctx)
    cfg = proj.config
    conn = proj.connect()
    try:
        docs = mapping.pending_docs(conn, include_variants=include_variants)
        tokens_in = 0
        for doc in docs:
            rendered = profile_mod.render_profile(doc["kind"], db.load_profile(doc))
            excerpt = mapping.EXCERPT_CHARS.get(doc["kind"], 6_000)
            tokens_in += (min(len(rendered), mapping.MAX_PROFILE_CHARS)
                          + min(len(doc["markdown"] or ""), excerpt) + 2_500) // 4
        raw = sum(len(d["markdown"] or "") for d in docs) // 4
    finally:
        conn.close()

    tokens_out = len(docs) * 1_200
    line = (f"Analysis map ({cfg.extract_model}): {len(docs)} documents, "
            f"~{tokens_in:,} in / ~{tokens_out:,} out tokens")
    price = cfg.batch_prices_per_1m_tokens.get(cfg.extract_model)
    if price:
        usd = tokens_in / 1e6 * price["input"] + tokens_out / 1e6 * price["output"]
        line += f" = roughly ${usd:.2f}"
    click.echo(line)
    if raw:
        click.echo(f"Sending the same documents in full would be ~{raw:,} input tokens "
                   f"({raw / max(tokens_in, 1):.1f}x more).")


@cli.command("map")
@click.option("--include-variants", is_flag=True,
              help="Map every version, not just one representative per family.")
@click.pass_context
def analysis_map(ctx, include_variants):
    """Submit the batch job that reads each document's purpose and processes."""
    proj = _project(ctx)
    try:
        job_names = mapping.submit(proj, proj.config, include_variants=include_variants)
    except (RuntimeError, gemini.GeminiError) as exc:
        raise click.ClickException(str(exc))
    if not job_names:
        click.echo(
            "Nothing to map: no profiled, relevant, reviewed documents are unmapped.\n"
            "(Run `docpipe analysis profile` first; new documents must pass triage "
            "and review — see `docpipe run`.)"
        )
        return
    for name in job_names:
        click.echo(f"Submitted analysis batch job: {name}")
    click.echo("Track it with: docpipe status --wait, then: docpipe analysis pack")


@cli.command("pack")
@click.option("--token-budget", default=pack_mod.DEFAULT_TOKEN_BUDGET, show_default=True,
              help="Token budget for the full-text documents in content/.")
@click.option("--all", "include_irrelevant", is_flag=True,
              help="Include documents triaged as irrelevant.")
@click.pass_context
def analysis_pack(ctx, token_budget, include_irrelevant):
    """Assemble analysis_pack/ — the bundle you hand to the analyst model."""
    proj = _project(ctx)
    try:
        result = pack_mod.build(proj, proj.config, token_budget=token_budget,
                                relevant_only=not include_irrelevant)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Built analysis pack at {result['pack']}")
    for path in result["files"]:
        click.echo(f"  {path.name}")
    click.echo(
        f"  content/ — {result['included']} documents in full "
        f"(~{result['packed_tokens']:,} tokens; {result['excluded']} left out of the budget)"
    )
    coverage = result["coverage"]
    if coverage["unmapped"]:
        click.echo(f"Note: {coverage['unmapped']} documents are profiled but not mapped — "
                   "run `docpipe analysis map` for a richer pack.")


@cli.command("status")
@click.pass_context
def analysis_status(ctx):
    """Show how far the corpus has been prepared for analysis."""
    proj = _project(ctx)
    for line in pack_mod.status_lines(proj):
        click.echo(line)
