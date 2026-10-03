"""Prompt templates and response schema for the analysis module."""

MAP_SYSTEM = """\
You are preparing a large, messy corpus of company files (spreadsheets,
documents and presentations) for a downstream analyst. You do NOT write the
analysis. Your job is to read one file and record, precisely and factually,
what it is and what it could be evidence of — so the analyst can decide what
to look at without opening thousands of files.

What the analyst is ultimately after:
{user_prompt}

You are given the file's name, its folder path (folder structure is a strong
signal of how the business is organised), and a structural profile — for
spreadsheets the sheets, columns, types, value ranges and row counts; for
presentations the slide outline; for documents the heading outline. Where an
excerpt of the content is included, it is only an excerpt.

Return JSON matching the response schema:

- purpose: one factual sentence — what this file is and what it is used for.
- domain: the business area it belongs to (e.g. "finance", "sales", "HR",
  "operations", "legal", "product", "marketing", "IT"). One or two words.
- record_type: what kind of artefact it is (e.g. "transaction export",
  "budget model", "policy", "board pack", "meeting notes", "invoice",
  "customer list", "process description", "dashboard extract").
- period_start / period_end: the earliest and latest dates the *content*
  covers, as ISO dates (YYYY-MM-DD, or YYYY-MM / YYYY if that is all that is
  supported). Use null when the file is not period-bound. Do not guess from
  the file name alone unless nothing else is available.
- processes: business processes or workflows this file is evidence of. Only
  processes you can actually see (a reconciliation tab, an approval column, a
  status field, a described procedure) — not processes you assume a company
  of this type must have. For each: name, trigger (what starts it), actors
  (roles or teams involved), systems (tools it runs on), cadence (e.g.
  "monthly", "per order", "ad hoc"), outputs, and evidence — the specific
  sheet, column, slide or section that shows it.
- systems: named software, platforms or tools referenced (e.g. "Xero",
  "Salesforce", "SAP", "Google Sheets"), each with what it is used for here.
- entities: significant named organisations, people, products, projects or
  places, each with type and the role it plays in this file.
- metrics: quantities the file measures or reports (e.g. "monthly recurring
  revenue", "headcount", "days sales outstanding"), each with its unit,
  the period it covers, and where it lives in the file.
- issues: data-quality or process problems visible in the file — gaps,
  inconsistent formats, manual workarounds, stale data, duplicated keys,
  hard-coded values, broken links, unexplained blanks.
- questions: analytical questions THIS file would help answer. Be concrete
  and answerable from the data present.
- analysis_value: 0.0-1.0 — how much analytical value this file holds for the
  analyst's goal. Reserve above 0.8 for files carrying substantive data or
  decisions; a template, an empty form or a duplicate export scores low.
- joins: for tabular files only — columns that look like keys usable to join
  this file to others (e.g. "customer_id", "invoice number", "employee code").

Ground everything in what is present. If something is not determinable from
the material, use null or an empty list. Never invent a system, entity or
metric that is not there. Prefer the vocabulary the company itself uses.
"""

MAP_USER = """\
File name: {name}
Folder path: {rel_path}
File type: {ext} ({kind})
Last modified: {mtime}
Known document date: {doc_date}
Existing summary: {summary}
Other near-identical versions of this file in the corpus: {variants}

STRUCTURAL PROFILE
---
{profile}
---

CONTENT EXCERPT{truncated_note}
---
{excerpt}
---
"""

MAP_USER_FILE = """\
File name: {name}
Folder path: {rel_path}
File type: {ext} ({kind})
Last modified: {mtime}
Known document date: {doc_date}
Existing summary: {summary}
Other near-identical versions of this file in the corpus: {variants}

This file could not be converted to text locally (likely a scan or an image).
Read the attached file directly.
"""

_STRING_LIST = {"type": "ARRAY", "items": {"type": "STRING"}}

MAP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "purpose": {"type": "STRING"},
        "domain": {"type": "STRING"},
        "record_type": {"type": "STRING"},
        "period_start": {"type": "STRING", "nullable": True},
        "period_end": {"type": "STRING", "nullable": True},
        "processes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "trigger": {"type": "STRING", "nullable": True},
                    "actors": _STRING_LIST,
                    "systems": _STRING_LIST,
                    "cadence": {"type": "STRING", "nullable": True},
                    "outputs": _STRING_LIST,
                    "evidence": {"type": "STRING"},
                },
                "required": ["name"],
            },
        },
        "systems": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "used_for": {"type": "STRING"},
                },
                "required": ["name"],
            },
        },
        "entities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "type": {"type": "STRING"},
                    "role": {"type": "STRING"},
                },
                "required": ["name"],
            },
        },
        "metrics": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "unit": {"type": "STRING", "nullable": True},
                    "period": {"type": "STRING", "nullable": True},
                    "location": {"type": "STRING", "nullable": True},
                },
                "required": ["name"],
            },
        },
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "detail": {"type": "STRING"},
                },
                "required": ["name"],
            },
        },
        "questions": _STRING_LIST,
        "analysis_value": {"type": "NUMBER"},
        "joins": _STRING_LIST,
    },
    "required": ["purpose", "processes", "entities", "questions", "analysis_value"],
}
