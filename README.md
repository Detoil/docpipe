# docpipe

A generic CLI pipeline that takes **an intention prompt and a folder of messy
documents**, and produces **a searchable document database, a dated facts
database, an irrelevant-documents folder, and a cited report/briefing** — using
the [Gemini Batch API](https://ai.google.dev/gemini-api/docs/batch-api)
(50% of interactive cost) for all bulk LLM work and
[MarkItDown](https://github.com/microsoft/markitdown) for local conversion.

Built for the "get a new director up to speed on the whole company drive"
problem, but the prompt drives every stage, so any corpus + goal works.

## Easy setup (no command-line experience needed)

1. **Install Python** (one time). Go to <https://www.python.org/downloads/>
   and run the installer. You need Python **3.10 or newer** — the Python that
   comes pre-installed on Macs is too old, so install it even if you think
   you have Python.
   - **Windows — important:** tick the box **"Add python.exe to PATH"** at the
     bottom of the installer's first screen before clicking Install.
2. **Get the tool onto your computer.** On this page, click the green
   **Code** button, then **Download ZIP** (or copy the folder from a flash
   drive). Unzip/put it somewhere easy, like your Documents folder.
3. **Start the tool.** Open the tool's folder and:
   - Windows: double-click **`docpipe.bat`**
   - Mac: double-click **`docpipe.command`** — if Mac says it "cannot be
     opened", right-click it, choose **Open**, then **Open** again.
   - Linux: run `./docpipe.sh`

   The first start sets everything up by itself (needs internet once, takes a
   few minutes), then creates a file called **`docpipe_settings.txt`** in the
   tool's folder and tells you to fill it in: where your documents are, your
   free Gemini API key (from <https://aistudio.google.com/apikey>), and what
   you want from the documents — in your own words.
4. **Start the tool again.** It walks you through every step, tells you what
   it is doing and roughly what it will cost, and pauses once for you to
   review which documents it kept (a spreadsheet you can open in Excel).
   You can close the window at any point and start it again later — it
   continues where it left off.

If anything ever looks broken (for example after moving the folder or
updating Python), just double-click the start file again: it checks its own
setup every time and repairs it automatically.

Your results end up in the `docpipe_project/report` folder; your original
documents are never moved or changed.

## How it works

```
init ─ scan ─ convert ─ triage ─ status --wait ─ review ─ extract ─ status --wait ─ report
        │       │          │                       │         │                        │
      hash &  MarkItDown  Flash-Lite batch:     human gate: Flash batch:          report.md +
      dedupe  → markdown  relevant or not?      edit CSV,   dated facts,          briefing.md
      (local, (local,     excerpt only, cheap   apply       structured output     (cited)
       free)   free)
```

Key design decisions:

- **One SQLite file** (`pipeline.db`) is both the output and the pipeline's
  state store: `documents` (deduplicated, full markdown, FTS5-searchable),
  `facts`, `batch_jobs`. Every stage is idempotent and resumable — crash,
  restart, re-run, it picks up where it left off.
- **The source folder is read-only.** Duplicates (same content, many paths)
  are detected by sha256 and processed once. The "irrelevant" folder is built
  from symlinks; nothing in your drive is ever moved or modified.
- **Facts are append-only assertions**, not updates:
  `(entity, attribute, value, as_of_date, source document, confidence, quote)`.
  Two shareholder registers yield two dated assertions; the `current_facts`
  SQL view resolves the latest state, and the full history becomes the event
  timeline. The LLM never decides which row to overwrite.
- **Triage is cheap by construction**: flash-lite, batch rates, and only an
  excerpt (filename + folder path + first ~6k chars) per document.
- **Scanned PDFs and images** (MarkItDown can't OCR) are flagged and the
  original file is uploaded via the Files API so Gemini reads it natively.
- **A human review gate** sits between triage and extraction: you get a
  CSV/HTML table of verdicts with the model's one-line reasons, flip any
  wrong ones, and apply.

## For command-line users

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export GEMINI_API_KEY=...   # https://aistudio.google.com/apikey
```

Running plain `docpipe` (or `docpipe start`) gives the same guided,
settings-file-driven flow as the double-click launchers. The subcommands
below offer finer control:

## Quick start

```bash
docpipe init /path/to/company-drive --prompt \
  "I need to get a new director up to speed on the company: ownership history,
   governance, major events, key contracts and obligations. Internal records
   are relevant; marketing material and personal files are not."

docpipe estimate        # rough cost preview before anything is submitted
docpipe run             # scan → convert → triage batch → waits → exports review

# ... edit docpipe_project/review/review.csv (flip the 'relevant' column) ...
docpipe review apply    # or: docpipe review apply --accept-all

docpipe run             # extract batch → waits → report.md + briefing.md
```

`docpipe run` stops at the review gate and tells you what to do; run it again
to continue. Use `--no-wait` to submit batch jobs and come back later with
`docpipe status --wait` (batch jobs can take up to 24h, usually much less).

## Commands

| Command | What it does |
|---|---|
| *(none)* / `start` | Guided mode: settings template + walk every step |
| `init SOURCE --prompt ...` | Create a project for a source folder and goal |
| `scan` | Walk folders/subfolders, hash, dedupe, filter junk (local) |
| `convert` | MarkItDown → markdown; flag scans for Gemini file upload (local) |
| `estimate` | Token/cost preview for both batch passes |
| `triage` | Submit the relevance batch job (flash-lite, excerpts) |
| `status [--wait]` | Poll batch jobs, ingest finished results, show pipeline state |
| `review export` | Write `review.csv` + `review.html` with verdicts and reasons |
| `review apply [--accept-all]` | Apply your edits; build the irrelevant/ symlink folder |
| `schema` | (Optional) LLM proposes extraction guidance from samples; edit `extraction_guidance.md` |
| `extract` | Submit the fact-extraction batch job (structured output) |
| `report [--no-brief]` | Build `report.md`; with brief, also the synthesised `briefing.md` |
| `search QUERY` | Full-text search across all converted documents |
| `jobs` | List every batch job submitted by this project |
| `run [--no-wait]` | Chain everything, stopping at the review gate |

## Project layout

```
docpipe_project/
├── config.json              # models, filters, prices — edit freely
├── pipeline.db              # documents + facts + state (the main output)
├── extraction_guidance.md   # optional, from `docpipe schema`, editable
├── batches/                 # JSONL request files (audit trail)
├── review/review.csv|html   # the human gate
├── irrelevant/              # symlinks to documents triaged out
└── report/report.md|briefing.md
```

## Modules

Modules are optional products built on top of the processed document base
(`src/docpipe/modules/`). They plug into the same project: their batch jobs
are tracked by `docpipe status`, their state lives in `pipeline.db`, and
their outputs land in the project folder. A module contributes a CLI group
(`docpipe <module> ...`) and an ingest function per batch stage it submits.

### Dataroom (`docpipe dataroom`)

Turns the processed documents into an investor due-diligence dataroom plus
two reports:

- **`dataroom/`** — your documents organised into numbered due-diligence
  categories (corporate, equity, financial, tax, contracts, IP, HR, property,
  regulatory, insurance, litigation) as symlinks back into the source tree,
  with an `INDEX.md`. Ready to share or upload to a dataroom provider.
- **`dataroom/INVESTOR_REPORT.md`** — an LLM-synthesised, cited report for an
  investor evaluating the company: overview, ownership and governance,
  financial position, contracts and IP, risks and red flags, and what is
  missing.
- **`report/dataroom_gap_report.md`** — a deterministic gap report: which
  checklist items are evidenced, which required/recommended documents are
  missing (your "request these from the company" list), and — on later
  builds — which gaps were closed since the last build.

The checklist driving all of this is **globally applicable**: it ships
jurisdiction-neutral (with notes where document names differ by country) and
is written to `dataroom_checklist.json`, which you can edit freely. Pass
`--country` at init and one LLM call localises it — e.g. for South Africa it
adds the CIPC/SARS/B-BBEE-type items an investor there would expect.

```bash
# after the core pipeline has run (triage reviewed, extraction done):
docpipe dataroom init --country "South Africa"   # or no --country: neutral baseline
docpipe dataroom map          # batch job: classify every relevant doc
docpipe status --wait
docpipe dataroom build        # dataroom/ + INDEX.md + investor report + gap report
docpipe dataroom status       # coverage summary in the terminal
```

**The follow-up pass.** When the missing documents from the gap report have
been added to the source folder (any time later — everything is resumable):

```bash
docpipe run                          # picks up only the new files
docpipe review apply --accept-all    # or review the new verdicts first
docpipe dataroom map                 # maps only the not-yet-mapped documents
docpipe status --wait
docpipe dataroom build               # regenerates everything; the gap report
                                     # now lists the gaps closed since last build
```

### Corpus (`docpipe corpus`)

Emits a **Corpus Ingest Contract** file — the JSON an external producer must
hand to the `detoil_corpus seed` loader, which upserts it into 13 Notion
registers.

**Always available (no LLM):** `emit` populates the contract's primary
Drive-extraction target, the **`document_index`** register — one row per unique
document, with a stable content-hash natural key (`file_id: "sha256:…"`) so
re-emitting upserts in place instead of duplicating, a `doc_type` classified
into the contract's fixed select set, the folder path and the modified time.

**Optional targeted extraction (`corpus extract`):** a batch pass that fills
nine more register types from document *content* — `company_identity`,
`financial_profile`, `staff_register`, `contract_register`,
`compliance_calendar`, `supplier_register`, `customer_register`,
`tool_access_register` and `hmcp_provider_register`. The model returns
register-shaped records against a schema generated from the contract (select
options baked in as enums, dates/numbers typed); docpipe computes every natural
key itself — §2b keys via the documented hash, supplier/customer/provider slugs
minted from the name — so the model never invents an identifier. Records are
merged and deduplicated by natural key across documents (the company singletons
into one row keyed by the registration number). The three registers the
contract does *not* source from documents — `financial_snapshot` (Xero sync),
`task_queue` and `audit_log` (system-written) — are deliberately left out.

```bash
docpipe corpus extract                # batch: fill registers from document content
docpipe status --wait                 # shared poll loop ingests the results
docpipe corpus emit                   # write report/corpus.json
docpipe corpus emit --relevant-only   # index only docs triaged as relevant
docpipe corpus status                 # what an emit would produce
docpipe corpus validate corpus.json   # conformance-check any producer's file
```

The full 13-register contract (natural keys, select option sets, ISO dates,
number coercion, the §2b key-derivation hash) is encoded in the module as a
producer-side validator. Every field the model returns is coerced against it
before emit — anything that would not load (a bad date, an off-enum value, a
non-numeric amount) is dropped rather than emitted, so `emit` is **fail-closed**:
a non-conforming document is never written, and `validate` works on any
external producer's file too. Load the result downstream with:

```bash
NOTION_TOKEN=… python -m detoil_corpus seed report/corpus.json
```

### Analysis (`docpipe analysis`)

Turns a bulk drive of spreadsheets, documents and presentations into an
**analysis pack**: a compact, navigable bundle that a more capable model can
reason over in one sitting, instead of being handed thousands of files.

The core pipeline answers *which documents matter and what do they assert*.
This module answers the question an analyst has to settle first: **what is all
this** — what data exists, in what shape, covering what period, produced by
which processes, and what can it actually be asked?

```
profile ─ estimate ─ map ─ status --wait ─ pack
   │                  │                      │
structure of every   one structured read   analysis_pack/: corpus map,
file + near-dup      per document, built    data dictionary, processes,
families (local,     from the profile,      entities, timeline, questions,
free)                not the data           index, and budgeted full text
```

**Why it is cheap.** A 200,000-row workbook converts to megabytes of markdown
that no model should read in full, but its analytical shape — sheets, columns,
types, value ranges, fill rates, row counts — is a kilobyte. `profile` reads
spreadsheets with openpyxl, presentations with python-pptx and documents from
their markdown, and stores that shape. The LLM pass then sends the *profile*
plus a short excerpt, so a huge workbook costs about what a memo costs.
`analysis estimate` prints the ratio for your corpus.

On top of that, documents are grouped into **families** by normalised name
plus structural fingerprint — `Budget.xlsx`, `Budget v2 FINAL.xlsx` and
`Budget 2024 (copy).xlsx` become one family — and only the newest member of
each is sent to the model and packed. (Byte-identical files were already
deduplicated by sha256 during `scan`; this catches the near-identical rest.)

**What the pack contains** (in `analysis_pack/`, read in numbered order):

| File | Use it for |
|---|---|
| `00_BRIEF.md` | Orientation: what the corpus is, how to cite it, and **what is not in the pack** |
| `01_CORPUS_MAP.md` | Folder structure, volumes, formats, near-duplicate families |
| `02_DATASETS.md` | The data dictionary — every sheet, column, type, range, join key |
| `03_PROCESSES.md` | Processes observed in the files, with actors, systems and cadence |
| `04_ENTITIES_AND_SYSTEMS.md` | Organisations, people, tools and metrics, and where they appear |
| `05_TIMELINE.md` | Dated documents and extracted facts, chronologically |
| `06_QUESTIONS_AND_GAPS.md` | What the corpus can answer, and the data-quality issues limiting it |
| `07_DOCUMENT_INDEX.md` | Every document: id, path, purpose, analytical value, family |
| `content/` | Full text of the highest-value documents, packed to a token budget |
| `pack.json` | All of the above, machine-readable |

```bash
docpipe analysis profile          # local: structure + families, no API key
docpipe analysis estimate         # what the batch pass will cost
docpipe analysis map              # batch: one structured read per document
docpipe status --wait             # shared poll loop ingests the results
docpipe analysis pack             # build analysis_pack/
docpipe analysis pack --token-budget 800000   # bigger content/ budget
docpipe analysis status           # how far the corpus has been prepared
```

`profile` and `pack` are local and free — only `map` calls the API, so
`profile` + `pack` alone already produces a usable pack (structure, corpus map
and data dictionary) without an API key. Every stage is incremental: a later
run after new files arrive processes only what is new.

The pack is written to be handed to the analyst model as-is. `00_BRIEF.md`
states its own limits — that spreadsheets are represented by structure and a
sample rather than every row, which documents were left outside the token
budget, and that relevance triage already removed material — so the model does
not mistake the pack for the whole drive.

## Configuration notes

- **Models** default to `gemini-flash-lite-latest` (triage) and
  `gemini-flash-latest` (extraction/synthesis); set any batch-supported model
  in `config.json`.
- **Prices** in `batch_prices_per_1m_tokens` only feed `docpipe estimate` —
  verify against [current pricing](https://ai.google.dev/gemini-api/docs/pricing).
- **Privacy:** document content is sent to the Gemini API. Make sure that is
  acceptable for your corpus before running triage/extract.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the local stages and the batch request/response handling with
faked API responses; no API key or network needed.
