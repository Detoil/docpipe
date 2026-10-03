"""Local pass: profile the *structure* of every document, for free.

The point of this stage is token economy. A 40,000-row workbook converts to
megabytes of markdown that no model should ever read in full, but its
analytical shape — sheets, columns, types, ranges, fill rates, row counts —
is a kilobyte. Profiling turns bulk into shape:

    spreadsheet -> sheets -> columns (name, type, filled, distinct, range,
                                      examples) + row counts + formula use
    presentation -> slide count + per-slide titles and bullet outline
    document     -> heading outline, word count, tables, dates mentioned

Everything here is local, deterministic and needs no API key. Native readers
(openpyxl, python-pptx) are used when importable; otherwise the profile is
recovered from the markdown MarkItDown already produced, so the stage always
runs.

Documents are then grouped into *families* by a signature built from the
normalised file name plus the structural fingerprint, which is what collapses
`Budget.xlsx`, `Budget v2.xlsx` and `Budget v2 FINAL (copy).xlsx` into one
family with one representative. Identical content is already deduplicated
upstream by sha256; this catches the near-identical rest.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

from ... import db as core_db
from ...config import Config, Project
from . import db

TABULAR_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
DECK_EXTENSIONS = {".pptx", ".ppt"}

# Caps: profiling reads a sample, never the whole file.
SAMPLE_ROWS = 2000
MAX_COLUMNS = 60
MAX_SHEETS = 40
MAX_SLIDES = 200
MAX_EXAMPLES = 3
MAX_DISTINCT = 500
MAX_HEADINGS = 60

DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}"),
    re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"),
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
NUMBER_RE = re.compile(r"^-?[\d\s,]*\.?\d+%?$")
BOOLEANS = {"true", "false", "yes", "no", "y", "n"}

# Version/copy noise stripped when building a family signature.
VERSION_NOISE = re.compile(
    r"(\bv\d+(\.\d+)*\b|\bversion\s*\d+\b|\bfinal\b|\bdraft\b|\bcopy\b|\bnew\b|"
    r"\bold\b|\blatest\b|\bupdated?\b|\brev\s*\d*\b|\bbackup\b|\(\d+\)|"
    r"\b\d{4}[-_]?\d{2}[-_]?\d{2}\b|\b\d{8}\b)"
)


def _tokens(value: str) -> int:
    """The pipeline's convention: token estimate is characters / 4."""
    return len(value) // 4


# --------------------------------------------------------------------------
# value typing


def _looks_like_date(text: str) -> bool:
    return any(p.match(text) for p in DATE_PATTERNS)


def _value_type(value) -> str:
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (datetime, date)):
        return "date"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    text = str(value).strip()
    if not text:
        return "empty"
    if text.lower() in BOOLEANS:
        return "boolean"
    if _looks_like_date(text):
        return "date"
    if NUMBER_RE.match(text):
        return "number" if ("." in text or "%" in text) else "integer"
    return "text"


def _column_profile(name: str, values: list) -> dict:
    """Summarise one column: type, fill rate, cardinality, range, examples."""
    kinds: dict[str, int] = {}
    filled: list[str] = []
    distinct: set[str] = set()
    for value in values:
        kind = _value_type(value)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "empty":
            continue
        text = value.isoformat() if isinstance(value, (datetime, date)) else str(value).strip()
        filled.append(text)
        if len(distinct) < MAX_DISTINCT:
            distinct.add(text)

    non_empty = [k for k in kinds if k != "empty"]
    if not non_empty:
        dominant = "empty"
    else:
        dominant = max(non_empty, key=lambda k: kinds[k])

    profile = {
        "name": name,
        "type": dominant,
        "filled": len(filled),
        "distinct": len(distinct) if len(distinct) < MAX_DISTINCT else f"{MAX_DISTINCT}+",
        "examples": filled[:MAX_EXAMPLES],
    }
    if dominant in ("integer", "number", "date") and filled:
        # Dates sort correctly as ISO strings; numbers do not ("9" > "10").
        ordered = sorted(filled)
        if dominant in ("integer", "number"):
            numeric = []
            for text in filled:
                try:
                    numeric.append((float(text.replace(",", "").rstrip("%")), text))
                except ValueError:
                    continue
            if numeric:
                ordered = [text for _, text in sorted(numeric)]
        profile["min"], profile["max"] = ordered[0], ordered[-1]
    if len(non_empty) > 1:
        profile["mixed_types"] = sorted(non_empty)
    return profile


def _table_profile(name: str, header: list, rows: list[list], total_rows=None) -> dict:
    columns = []
    for index, raw_name in enumerate(header[:MAX_COLUMNS]):
        column_name = str(raw_name).strip() if raw_name not in (None, "") else f"column_{index + 1}"
        columns.append(_column_profile(column_name, [r[index] if index < len(r) else None
                                                     for r in rows]))
    return {
        "name": name,
        "rows": total_rows if total_rows is not None else len(rows),
        "rows_sampled": len(rows),
        "columns": len(header),
        "columns_truncated": len(header) > MAX_COLUMNS,
        "column_profiles": columns,
    }


def _pick_header(rows: list[list]) -> tuple[list, list[list], int]:
    """Find the header row: the *widest* mostly-text row near the top.

    Exported spreadsheets routinely carry a title and a blank line before the
    real header. Taking the first row that clears a low bar picks the title
    instead, because a two-cell title row is trivially "mostly text" — the
    real header, with ten named columns, sits a row or two below it and never
    gets considered. Scoring every candidate in the window and keeping the
    best one costs the same and gets those names back.

    Ties go to the earliest row, so a genuine header still wins over a wide
    row of text data further down.

    Returns (header, body, header_index) — the index matters because the rows
    above the header are not data and must not be counted as such."""
    best_score = None
    best_index = None
    for index, row in enumerate(rows[:10]):
        cells = [c for c in row if c not in (None, "")]
        if len(cells) < 2:
            continue
        textish = sum(1 for c in cells if _value_type(c) == "text")
        if textish < max(2, len(cells) // 2):
            continue
        score = (textish, len(cells))
        if best_score is None or score > best_score:
            best_score, best_index = score, index
    if best_index is None:
        return (rows[0], rows[1:], 0) if rows else ([], [], 0)
    return rows[best_index], rows[best_index + 1:], best_index


# --------------------------------------------------------------------------
# tabular


def _profile_workbook(path: Path) -> dict | None:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None
    try:
        book = load_workbook(filename=str(path), read_only=True, data_only=True)
    except Exception:  # openpyxl raises many types on damaged files
        return None
    sheets = []
    sheet_count = len(book.sheetnames)
    try:
        for sheet_name in book.sheetnames[:MAX_SHEETS]:
            worksheet = book[sheet_name]
            rows: list[list] = []
            for row in worksheet.iter_rows(max_row=SAMPLE_ROWS, values_only=True):
                rows.append(list(row))
            if not any(any(c is not None for c in r) for r in rows):
                sheets.append({"name": sheet_name, "rows": 0, "columns": 0,
                               "column_profiles": [], "empty": True})
                continue
            header, body, header_index = _pick_header(rows)
            total = worksheet.max_row or len(rows)
            sheets.append(_table_profile(sheet_name, header, body,
                                         total_rows=max(total - header_index - 1, 0)))
    except Exception:
        if not sheets:
            return None
    finally:
        book.close()
    return {"sheets": sheets, "sheet_count": sheet_count, "reader": "openpyxl"}


def _profile_delimited(path: Path, ext: str) -> dict | None:
    delimiter = "\t" if ext == ".tsv" else ","
    rows: list[list] = []
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            for total, row in enumerate(csv.reader(handle, delimiter=delimiter), start=1):
                if len(rows) < SAMPLE_ROWS:
                    rows.append(row)
    except (OSError, csv.Error):
        return None
    if not rows:
        return None
    header, body, header_index = _pick_header(rows)
    sheet = _table_profile(path.stem, header, body,
                           total_rows=max(total - header_index - 1, 0))
    return {"sheets": [sheet], "sheet_count": 1, "reader": "csv"}


def _markdown_tables(markdown: str) -> list[dict]:
    """Recover tables from MarkItDown's markdown as a last resort.

    MarkItDown emits one `## SheetName` heading per worksheet followed by a
    pipe table, so sheet boundaries survive the conversion."""
    sheets = []
    current = "sheet1"
    rows: list[list] = []

    def flush():
        if len(rows) >= 1:
            header, body, _ = _pick_header(rows)
            sheets.append(_table_profile(current, header, body))
        rows.clear()

    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            current = stripped.lstrip("#").strip() or current
        elif stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue  # the markdown separator row
            rows.append(cells)
        elif not stripped:
            continue
        else:
            flush()
    flush()
    return sheets[:MAX_SHEETS]


def _profile_tabular(path: Path, ext: str, markdown: str) -> dict:
    profile = None
    if ext in (".xlsx", ".xlsm"):
        profile = _profile_workbook(path)
    elif ext in (".csv", ".tsv"):
        profile = _profile_delimited(path, ext)
    if profile is None:
        sheets = _markdown_tables(markdown)
        profile = {"sheets": sheets, "sheet_count": len(sheets), "reader": "markdown"}
    profile["total_rows"] = sum(s.get("rows") or 0 for s in profile["sheets"])
    return profile


# --------------------------------------------------------------------------
# decks and text


def _profile_deck(path: Path, markdown: str) -> dict:
    slides = []
    try:
        from pptx import Presentation

        deck = Presentation(str(path))
        for number, slide in enumerate(deck.slides, start=1):
            if number > MAX_SLIDES:
                break
            texts = [shape.text_frame.text.strip()
                     for shape in slide.shapes
                     if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()]
            title = texts[0].splitlines()[0] if texts else f"(untitled slide {number})"
            body = " / ".join(t.replace("\n", " ")[:120] for t in texts[1:3])
            slides.append({"number": number, "title": title[:200], "outline": body[:300]})
        return {"slides": slides, "slide_count": len(deck.slides), "reader": "python-pptx"}
    except Exception:
        pass

    # Fallback: MarkItDown marks slides with an HTML comment.
    blocks = re.split(r"<!--\s*Slide number:\s*(\d+)\s*-->", markdown or "")
    if len(blocks) > 1:
        pairs = list(zip(blocks[1::2], blocks[2::2]))
        for number, text in pairs[:MAX_SLIDES]:
            lines = [line.strip("#* ").strip() for line in text.splitlines() if line.strip()]
            slides.append({
                "number": int(number),
                "title": (lines[0] if lines else f"(untitled slide {number})")[:200],
                "outline": " / ".join(lines[1:3])[:300],
            })
        return {"slides": slides, "slide_count": len(pairs), "reader": "markdown"}

    headings = [line.strip("#  ") for line in (markdown or "").splitlines()
                if line.strip().startswith("#")]
    slides = [{"number": i, "title": h[:200], "outline": ""}
              for i, h in enumerate(headings[:MAX_SLIDES], start=1)]
    return {"slides": slides, "slide_count": len(slides), "reader": "markdown"}


def _profile_text(markdown: str) -> dict:
    text = markdown or ""
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if title:
                headings.append({"level": level, "title": title[:200]})
    years = sorted({m.group(0) for m in YEAR_RE.finditer(text)})
    return {
        "words": len(text.split()),
        "headings": headings[:MAX_HEADINGS],
        "heading_count": len(headings),
        "table_rows": sum(1 for line in text.splitlines()
                          if line.strip().startswith("|") and line.strip().endswith("|")),
        "years_mentioned": years[:20],
    }


# --------------------------------------------------------------------------
# families


def _normalised_stem(name: str) -> str:
    stem = Path(name).stem.lower()
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = VERSION_NOISE.sub(" ", stem)
    # Whatever punctuation the noise left behind ("debtors ( )") would
    # otherwise split a family that is otherwise identical.
    stem = re.sub(r"[^a-z0-9]+", " ", stem)
    return re.sub(r"\s+", " ", stem).strip()


def _fingerprint(kind: str, profile: dict) -> str:
    """The structural shape of a document, independent of its contents."""
    if kind == "tabular":
        parts = []
        for sheet in profile.get("sheets", []):
            columns = ",".join(c["name"].lower() for c in sheet.get("column_profiles", []))
            # Sheet names carry the same version noise as file names — a CSV's
            # only "sheet" is its file name — so normalise them the same way.
            parts.append(f"{_normalised_stem(sheet.get('name', ''))}[{columns}]")
        return "|".join(parts)
    if kind == "deck":
        return "|".join(s["title"].lower() for s in profile.get("slides", [])[:30])
    return "|".join(h["title"].lower() for h in profile.get("headings", [])[:30])


def signature(kind: str, display_name: str, profile: dict) -> str:
    """Family key: normalised name + structural fingerprint.

    Both halves matter. The name alone would merge every `invoice.pdf` in the
    tree; the fingerprint alone would merge every document that happens to
    share a template."""
    stem = _normalised_stem(display_name)
    fingerprint = _fingerprint(kind, profile)
    if len(stem) < 4 and not fingerprint:
        return f"solo:{display_name}"
    digest = hashlib.sha1(f"{kind}\n{stem}\n{fingerprint}".encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------
# stage entry point


def document_kind(ext: str) -> str:
    if ext in TABULAR_EXTENSIONS:
        return "tabular"
    if ext in DECK_EXTENSIONS:
        return "deck"
    return "text"


def profile_document(doc, path: Path | None) -> tuple[str, dict]:
    """Profile one document. Returns (kind, profile)."""
    ext = (doc["ext"] or "").lower()
    kind = document_kind(ext)
    markdown = doc["markdown"] or ""
    if kind == "tabular" and path is not None and path.exists():
        profile = _profile_tabular(path, ext, markdown)
    elif kind == "tabular":
        profile = _profile_tabular(Path(doc["display_name"]), ".none", markdown)
    elif kind == "deck":
        profile = _profile_deck(path if path else Path(doc["display_name"]), markdown)
    else:
        profile = _profile_text(markdown)
    profile["needs_ocr"] = bool(doc["needs_ocr"])
    return kind, profile


def pending_docs(conn, relevant_only: bool = True):
    """Converted documents that have no profile yet."""
    db.ensure_schema(conn)
    q = ("SELECT * FROM documents WHERE status IN ('converted', 'triaged', 'extracted')"
         " AND id NOT IN (SELECT document_id FROM analysis_profiles)")
    if relevant_only:
        q += " AND (relevant = 1 OR relevant IS NULL)"
    return conn.execute(q + " ORDER BY id").fetchall()


def run(project: Project, cfg: Config, progress=None, relevant_only: bool = True) -> dict:
    """Profile every unprofiled document, then (re)compute families."""
    conn = project.connect()
    stats = {"profiled": 0, "tabular": 0, "deck": 0, "text": 0}
    try:
        docs = pending_docs(conn, relevant_only=relevant_only)
        total = len(docs)
        for index, doc in enumerate(docs, start=1):
            path_row = core_db.first_path(conn, doc["id"])
            path = Path(path_row["path"]) if path_row else None
            if progress:
                progress(f"[{index}/{total}] {path_row['rel_path'] if path_row else doc['display_name']}")
            kind, profile = profile_document(doc, path)
            db.save_profile(
                conn, doc["id"], kind, profile,
                signature(kind, doc["display_name"], profile),
                _tokens(doc["markdown"] or ""),
            )
            stats["profiled"] += 1
            stats[kind] += 1
            conn.commit()
        stats.update(db.assign_families(conn))
        conn.commit()
    finally:
        conn.close()
    return stats


# --------------------------------------------------------------------------
# rendering, for prompts and for the pack


def render_profile(kind: str, profile: dict, max_sheets: int = 12,
                   max_columns: int = 40) -> str:
    """The compact text form of a profile — this is what the model reads
    instead of the document's data."""
    lines = []
    if kind == "tabular":
        lines.append(f"Workbook: {profile.get('sheet_count', 0)} sheet(s), "
                     f"~{profile.get('total_rows', 0):,} data rows "
                     f"(profiled by {profile.get('reader', '?')})")
        for sheet in profile.get("sheets", [])[:max_sheets]:
            if sheet.get("empty"):
                lines.append(f"- sheet '{sheet['name']}': empty")
                continue
            lines.append(f"- sheet '{sheet['name']}': {sheet.get('rows', 0):,} rows x "
                         f"{sheet.get('columns', 0)} columns")
            for column in sheet.get("column_profiles", [])[:max_columns]:
                bits = [f"type={column['type']}", f"filled={column['filled']}",
                        f"distinct={column['distinct']}"]
                if "min" in column:
                    bits.append(f"range={column['min']}..{column['max']}")
                if column.get("examples"):
                    bits.append("e.g. " + ", ".join(str(e)[:40] for e in column["examples"]))
                lines.append(f"    - {column['name']}: " + "; ".join(bits))
    elif kind == "deck":
        lines.append(f"Presentation: {profile.get('slide_count', 0)} slides")
        for slide in profile.get("slides", [])[:60]:
            outline = f" — {slide['outline']}" if slide.get("outline") else ""
            lines.append(f"- {slide['number']}. {slide['title']}{outline}")
    else:
        lines.append(f"Document: ~{profile.get('words', 0):,} words, "
                     f"{profile.get('heading_count', 0)} headings, "
                     f"{profile.get('table_rows', 0)} table rows")
        for heading in profile.get("headings", [])[:40]:
            lines.append("  " * max(heading["level"] - 1, 0) + f"- {heading['title']}")
        if profile.get("years_mentioned"):
            lines.append("Years mentioned: " + ", ".join(profile["years_mentioned"]))
    if profile.get("needs_ocr"):
        lines.append("(no local text: scanned or image-only)")
    return "\n".join(lines)


def profile_json(kind: str, profile: dict) -> str:
    return json.dumps({"kind": kind, **profile}, ensure_ascii=False)
