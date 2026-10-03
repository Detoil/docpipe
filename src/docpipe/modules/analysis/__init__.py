"""Analysis module: turn a processed corpus into an analysis pack that a
more capable model can reason over in one sitting.

The core pipeline answers "which documents matter and what do they assert".
This module answers the prior question an analyst has to settle first: what
IS all this — what data exists, in what shape, covering what period, produced
by which processes, and what can it actually be asked?

  profile -> map (batch) -> status --wait -> pack
     │         │                               │
  structure   one structured read per       analysis_pack/: corpus map,
  of every    document, built from the      data dictionary, processes,
  file, plus  profile rather than the       entities, timeline, questions,
  near-dup    data, so a 200k-row book      document index, and the
  families    costs the same as a memo      highest-value text, budgeted

`profile` and `pack` are local and free; only `map` calls the API. Every
stage is incremental, so a later run after new files arrive processes only
what is new.
"""

from .cli import cli as CLI_GROUP
from .mapping import STAGE, ingest_lines

INGESTORS = {STAGE: ingest_lines}
