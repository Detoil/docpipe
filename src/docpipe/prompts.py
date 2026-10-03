"""Prompt templates for every LLM stage. {user_prompt} is the intention the
user gave at `docpipe init` — it steers every stage, which is what makes the
pipeline generic."""

TRIAGE_SYSTEM = """\
You are a document triage assistant working through a large, messy folder of files.

The user's goal:
{user_prompt}

For the document provided, decide whether it is RELEVANT to the user's goal.
When genuinely unsure, lean towards relevant=true: a human will review your
verdicts, and a false "irrelevant" risks losing information.

Respond with JSON only, matching the response schema:
- relevant: true/false
- reason: ONE short sentence explaining the verdict
- category: a short label for the document type (e.g. "shareholder register",
  "board minutes", "contract", "invoice", "marketing", "personal")
- doc_date: the date the document was created or is effective, as YYYY-MM-DD
  (or YYYY-MM / YYYY if that is all that can be determined); null if unknown.
  Filenames and folder names often contain the date.
- summary: 1-2 sentences describing what the document is and contains
"""

TRIAGE_USER = """\
File name: {name}
Folder path: {rel_path}
Last modified: {mtime}

Document content{truncated_note}:
---
{excerpt}
---
"""

TRIAGE_USER_FILE = """\
File name: {name}
Folder path: {rel_path}
Last modified: {mtime}

The document could not be converted to text locally (likely a scan or image).
Read the attached file directly.
"""

EXTRACT_SYSTEM = """\
You are a meticulous information-extraction engine.

The user's goal:
{user_prompt}
{guidance_section}
Extract every fact from the document that serves the user's goal, as atomic
assertions. Each fact has:
- entity: the canonical name of who/what the fact is about (use full names,
  consistently: "John A. Smith", "Acme Holdings Ltd")
- entity_type: one of person, company, shareholding, governance, contract,
  financial, legal, event, other
- attribute: what is being asserted (e.g. "shares held", "appointed director",
  "purchase price", "registered address")
- value: the asserted value, as text (keep numbers and units exact)
- as_of_date: the date the fact is true as of (YYYY-MM-DD if possible). If the
  fact does not state its own date, use the document's date. Null if unknown.
- confidence: 0.0-1.0, how certain you are the document asserts this
- quote: a short verbatim quote from the document supporting the fact

Important: facts are append-only assertions. Record exactly what THIS document
asserts, even if you suspect it is outdated — do not reconcile with other
documents. Later documents will supersede earlier ones by date.

Also return:
- doc_type: short label for the kind of document
- doc_date: the document's own date (YYYY-MM-DD), null if unknown
- summary: 2-4 sentences summarising the document
"""

EXTRACT_USER = """\
File name: {name}
Folder path: {rel_path}

Document content{truncated_note}:
---
{content}
---
"""

EXTRACT_USER_FILE = """\
File name: {name}
Folder path: {rel_path}

Read the attached file directly (it could not be converted to text locally).
"""

SCHEMA_PROPOSAL_SYSTEM = """\
You design extraction schemas for document-processing pipelines.

The user's goal:
{user_prompt}

Below is a sample of the relevant documents found (names, categories,
summaries). Propose concise extraction guidance for an LLM that will process
each document: which entities and attributes to extract, naming conventions so
the same entity/attribute is always written identically across documents, and
anything to ignore. Write it as a short markdown document of instructions
addressed to the extraction model. Do not include any preamble.
"""

SYNTHESIS_SYSTEM = """\
You are preparing a written briefing for a new company director.

The user's goal:
{user_prompt}

You are given the extracted facts (with dates and source documents) and
per-document summaries from the company's records. Using ONLY this material,
write a clear, well-structured markdown briefing:

1. Company overview
2. Timeline of major events (chronological)
3. Current ownership and governance (latest known state)
4. Key contracts, financials and obligations
5. Open questions, gaps and red flags (including conflicting facts)

Cite sources inline as [doc <id>: <file name>] after each claim. If facts
conflict, say so explicitly rather than silently choosing one. Do not invent
anything not present in the material.
"""
