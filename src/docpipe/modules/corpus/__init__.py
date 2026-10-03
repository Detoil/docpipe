"""Corpus module: emit a Corpus Ingest Contract file for the corpus loader.

After the core pipeline has scanned (and ideally triaged/extracted) a folder,
this module turns the database into a JSON document that the corpus loader's
``corpus/detoil_corpus/seed.py`` loads into its Notion registers:

  docpipe corpus extract                # batch pass: fill registers from docs
  docpipe status --wait                 # shared poll loop ingests the results
  docpipe corpus emit                   # write report/corpus.json
  docpipe corpus validate corpus.json   # conformance-check any producer's file

``emit`` always populates the ``document_index`` register — the contract's
primary Drive-extraction target — with a stable, content-hash natural key so
re-emitting upserts cleanly. The optional ``extract`` batch pass additionally
fills nine register types (company identity, financial profile, staff,
contracts, compliance, suppliers, customers, tool access, HMCP providers) from
document content, computing every natural key producer-side.

The full 13-register contract is encoded in ``schema.py`` as a producer-side
validator, so output is fail-closed: a non-conforming document is never
written.
"""

from .cli import cli as CLI_GROUP
from .extract import STAGE, ingest_lines

# `extract` submits a batch stage; the shared poll loop ingests it here.
INGESTORS = {STAGE: ingest_lines}
