"""Prompt templates and response schemas for the dataroom module."""

LOCALIZE_SYSTEM = """\
You maintain due-diligence checklists for company datarooms worldwide.

You are given a jurisdiction-neutral baseline checklist. The company operates
primarily in: {country}.

Propose how to localise the checklist for that jurisdiction, as JSON:
- additions: items an investor would expect for this jurisdiction that the
  baseline is missing (statutory registrations, local compliance certificates,
  industry documents commonly requested there). For each: category (one of the
  category codes shown in the checklist, e.g. COR, TAX, REG), title,
  description, importance ("required", "recommended" or "conditional"), and a
  note saying why it matters or which authority issues it.
- local_notes: for existing items whose document name, issuer or regime
  differs locally, the item code plus a short note giving the local name
  (e.g. the local company registry, tax authority or privacy law).

Do not duplicate baseline items. Keep it to documents investors genuinely ask
for in this jurisdiction.
"""

LOCALIZE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "additions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "category": {"type": "STRING"},
                    "title": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "importance": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["category", "title"],
            },
        },
        "local_notes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "code": {"type": "STRING"},
                    "note": {"type": "STRING"},
                },
                "required": ["code", "note"],
            },
        },
    },
    "required": ["additions", "local_notes"],
}

MAP_SYSTEM = """\
You are organising a company's internal documents into an investor
due-diligence dataroom.

Below is the dataroom checklist. For the document provided, decide which
checklist item(s) it belongs to. A document may satisfy several items (for
example, a shareholders' agreement can evidence both the agreement item and
the cap table item), or none.

Checklist:
{checklist}

Respond with JSON only, matching the response schema:
- matches: a list (possibly empty) of checklist items this document belongs
  in. For each match:
  - code: the checklist item code, exactly as listed above
  - confidence: 0.0-1.0, how certain you are the document belongs there
  - period: the period the document covers, if meaningful (e.g. "FY2023",
    "2021-03", "2020-2023"); null otherwise
  - note: one short phrase on what the document contributes to that item

Only use codes from the checklist. If the document fits no checklist item,
return an empty matches list — do not force a match.
"""

MAP_USER = """\
File name: {name}
Folder path: {rel_path}
Document type: {doc_type}
Document date: {doc_date}
Summary: {summary}

Document content{truncated_note}:
---
{excerpt}
---
"""

MAP_USER_FILE = """\
File name: {name}
Folder path: {rel_path}
Document type: {doc_type}
Document date: {doc_date}
Summary: {summary}

The document could not be converted to text locally (likely a scan or image).
Read the attached file directly.
"""

MAP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "matches": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "code": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "period": {"type": "STRING", "nullable": True},
                    "note": {"type": "STRING"},
                },
                "required": ["code"],
            },
        },
    },
    "required": ["matches"],
}

INVESTOR_REPORT_SYSTEM = """\
You are preparing a due-diligence report for a prospective investor who is
evaluating this company.

You are given, from the company's own records: extracted facts (with dates
and source documents), document summaries organised by dataroom category, and
the dataroom coverage summary (which checklist items are evidenced and which
are missing). Using ONLY this material, write a clear, well-structured
markdown report:

1. Executive summary
2. Company overview (what the business is and does)
3. Ownership, equity and governance (latest known state, and how it evolved)
4. Financial position (what the available records show)
5. Operations: key contracts, suppliers, customers and intellectual property
6. People and organisation
7. Regulatory, tax and compliance standing
8. Risks and red flags (including conflicting facts)
9. Due-diligence gaps: which expected documents are missing or outdated, and
   what an investor should request before relying on this report

Cite sources inline as [doc <id>: <file name>] after each claim. If facts
conflict, say so explicitly rather than silently choosing one. Do not invent
anything not present in the material. The company may be incorporated in any
country — do not assume a jurisdiction beyond what the documents state, and
briefly explain jurisdiction-specific document names where they appear.
"""
