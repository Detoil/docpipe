"""Plug-in product modules built on top of the core pipeline.

A module is a subpackage of docpipe.modules that exposes:

- CLI_GROUP:  a click group, added to the docpipe CLI as a subcommand
- INGESTORS:  {batch stage name: ingest_lines(conn, lines)} so the shared
              poll loop (`docpipe status`) can ingest the module's batch jobs

Modules store their state in the project's pipeline.db (own tables, created
idempotently) and write their outputs under the project directory, so they
inherit the core pipeline's resumability.
"""

from __future__ import annotations

from . import analysis, corpus, dataroom

ALL = [dataroom, corpus, analysis]


def cli_groups():
    """Click groups contributed by every bundled module."""
    return [m.CLI_GROUP for m in ALL]


def ingestors() -> dict:
    """Batch-stage ingest functions contributed by every bundled module."""
    table: dict = {}
    for m in ALL:
        table.update(m.INGESTORS)
    return table
