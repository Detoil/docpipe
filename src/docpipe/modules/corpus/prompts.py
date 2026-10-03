"""Prompt templates for the targeted corpus-extraction pass.

The field guide is rendered from the register specs in ``schema.py`` so the
prompt, the response schema and the validator never drift apart.
"""

from __future__ import annotations

from . import schema

EXTRACT_SYSTEM = """\
You extract structured records from one company's own internal documents to
populate a fixed set of registers. You are given a single document; return the
records it is genuine evidence for.

Hard rules:
- Populate a register ONLY when THIS document actually evidences it. Most
  documents populate one register or none. Leave everything else empty/null —
  never guess, never carry over facts the document does not state.
- Use ONLY the enum options listed for select fields. If none fit, leave the
  field null.
- Dates: ISO 8601 (YYYY-MM-DD). Money and amounts: plain numbers, no currency
  symbols or thousands separators. If a value is not stated, leave it null.
- For fields marked (JSON), emit a JSON string (e.g. a contact object, or an
  array like [30, 7, 1]).
- Do NOT emit identifier/uid/slug fields — those are assigned downstream. Emit
  the underlying facts (names, numbers, dates) and nothing else.
- For each record, set "confidence" to how sure you are (0.0-1.0) that the
  document evidences that record.

Registers and their fields:
{field_guide}
"""

EXTRACT_USER = """\
File name: {name}
Folder path: {rel_path}
Document type: {doc_type}
Document date: {doc_date}
Summary: {summary}

Document content{truncated_note}:
---
{content}
---
"""

EXTRACT_USER_FILE = """\
File name: {name}
Folder path: {rel_path}
Document type: {doc_type}
Document date: {doc_date}
Summary: {summary}

The document could not be converted to text locally (likely a scan or image).
Read the attached file directly and extract the records it evidences.
"""


def _field_line(register: str, field_name: str, json_fields: set) -> str:
    fspec = schema.REGISTERS[register].fields[field_name]
    if fspec.type == schema.SELECT:
        kind = "one of: " + " | ".join(fspec.options)
    elif fspec.type == schema.NUMBER:
        kind = "number"
    elif fspec.type == schema.DATE:
        kind = "ISO date"
    elif fspec.type == schema.EMAIL:
        kind = "email"
    else:
        kind = "text"
    if field_name in json_fields:
        kind += ", JSON"
    return f"    - {field_name} ({kind})"


def render_field_guide(targets, json_fields: set) -> str:
    """Human-readable per-register field guide for the system prompt."""
    blocks = []
    for target in targets:
        shape = "one record for the company" if target.singleton else "zero or more records"
        blocks.append(f"- {target.register} ({shape}): {target.purpose}")
        for field_name in target.emit_fields():
            blocks.append(_field_line(target.register, field_name, json_fields))
    return "\n".join(blocks)
