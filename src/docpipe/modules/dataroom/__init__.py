"""Dataroom module: organise processed documents into an investor
due-diligence dataroom, with an investor report and a gap report.

Pipeline (after the core run has triaged/reviewed/extracted):

  init -> map (batch) -> status --wait -> build
           │                               │
       classify each doc against       dataroom/ folder of symlinks,
       the (editable, localisable)     INDEX.md, INVESTOR_REPORT.md,
       due-diligence checklist         report/dataroom_gap_report.md

Everything is incremental: after missing documents are added to the source
folder and pushed through `docpipe run`, another map+build maps only the new
documents and the gap report shows which gaps were closed.
"""

from .cli import cli as CLI_GROUP
from .mapping import STAGE, ingest_lines

INGESTORS = {STAGE: ingest_lines}
