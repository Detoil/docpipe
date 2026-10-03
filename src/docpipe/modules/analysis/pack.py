"""Assemble the analysis pack: the corpus, compressed into something a
frontier model can actually read.

The pack is deliberately *deterministic and LLM-free* — it is built from what
the earlier stages already put in the database, so it costs nothing to
rebuild and works without an API key. The expensive reasoning is the job of
the model you hand the pack to.

Layout (under the project's `analysis_pack/`):

    00_BRIEF.md              how to use the pack, what is in it, what is not
    01_CORPUS_MAP.md         the tree: folders, formats, volumes, time span
    02_DATASETS.md           data dictionary: every sheet, column, type, range
    03_PROCESSES.md          processes observed, with actors, systems, cadence
    04_ENTITIES_AND_SYSTEMS.md  who and what appears, and where
    05_TIMELINE.md           dated documents and facts, chronologically
    06_QUESTIONS_AND_GAPS.md what the corpus can answer, and what is missing
    07_DOCUMENT_INDEX.md     every document: id, path, purpose, value, family
    pack.json                all of the above, machine-readable
    content/                 full text of the highest-value documents, packed
                             to a token budget

Reading order is the numbering: an analyst model reads 00, then whichever of
01-07 its question needs, then pulls specific documents out of content/.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import defaultdict

from ...config import Config, Project
from . import db, profile as profile_mod

DEFAULT_TOKEN_BUDGET = 400_000
MAX_DOC_CHARS = 120_000
MAX_TIMELINE_ROWS = 400
MAX_FACTS = 300


def pack_dir(project: Project):
    return project.root / "analysis_pack"


def _tokens(text: str) -> int:
    return len(text) // 4


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name)
    return keep.strip().replace(" ", "_")[:80] or "document"


# --------------------------------------------------------------------------
# gathering


def gather(conn, relevant_only: bool = True) -> dict:
    """Everything the pack renders, read out of the database once."""
    db.ensure_schema(conn)
    # "Relevant" here means *not triaged out*: documents that have not been
    # triaged yet (relevant IS NULL) still belong in the pack, so that
    # profile + pack works on its own, before or without the LLM passes.
    where = "WHERE d.relevant IS NOT 0" if relevant_only else ""
    rows = conn.execute(
        "SELECT d.id, d.display_name, d.ext, d.mtime, d.size_bytes, d.doc_date,"
        "       d.doc_type, d.summary, d.needs_ocr, d.markdown,"
        "       p.kind, p.profile, p.family_id, p.representative, p.token_estimate,"
        "       s.purpose, s.domain, s.period_start, s.period_end, s.value_score,"
        "       s.payload"
        " FROM documents d"
        " JOIN analysis_profiles p ON p.document_id = d.id"
        " LEFT JOIN analysis_signals s ON s.document_id = d.id"
        f" {where} ORDER BY d.id"
    ).fetchall()

    documents = []
    for row in rows:
        path = conn.execute(
            "SELECT rel_path FROM document_paths WHERE document_id = ? ORDER BY id LIMIT 1",
            (row["id"],),
        ).fetchone()
        payload = {}
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                payload = {}
        documents.append({
            "id": row["id"],
            "name": row["display_name"],
            "rel_path": path["rel_path"] if path else row["display_name"],
            "ext": row["ext"],
            "kind": row["kind"],
            "mtime": row["mtime"],
            "size_bytes": row["size_bytes"],
            "doc_date": row["doc_date"],
            "doc_type": row["doc_type"],
            "summary": row["summary"],
            "needs_ocr": bool(row["needs_ocr"]),
            "family_id": row["family_id"],
            "representative": bool(row["representative"]),
            "token_estimate": row["token_estimate"] or 0,
            "purpose": row["purpose"],
            "domain": row["domain"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "value_score": row["value_score"],
            "profile": profile_mod.render_profile(row["kind"], db.load_profile(row)),
            "profile_data": db.load_profile(row),
            "joins": payload.get("joins") or [],
            "markdown": row["markdown"] or "",
        })

    items: dict[str, list] = {}
    for kind in db.ITEM_KINDS:
        items[kind] = [dict(r) for r in db.items_of_kind(conn, kind, relevant_only)]

    facts = conn.execute(
        "SELECT f.entity, f.attribute, f.value, f.as_of_date, f.document_id,"
        "       d.display_name FROM facts f JOIN documents d ON d.id = f.document_id"
        " ORDER BY (f.as_of_date IS NULL), f.as_of_date, f.id"
    ).fetchall()

    return {"documents": documents, "items": items,
            "facts": [dict(f) for f in facts]}


# --------------------------------------------------------------------------
# roll-ups


def _folder_tree(documents: list[dict]) -> list[dict]:
    folders: dict[str, dict] = defaultdict(
        lambda: {"documents": 0, "tokens": 0, "kinds": defaultdict(int), "exts": defaultdict(int)}
    )
    for doc in documents:
        folder = doc["rel_path"].rsplit("/", 1)[0] if "/" in doc["rel_path"] else "(root)"
        entry = folders[folder]
        entry["documents"] += 1
        entry["tokens"] += doc["token_estimate"]
        entry["kinds"][doc["kind"]] += 1
        entry["exts"][doc["ext"] or "?"] += 1
    out = []
    for name in sorted(folders):
        entry = folders[name]
        out.append({
            "folder": name,
            "documents": entry["documents"],
            "tokens": entry["tokens"],
            "kinds": dict(entry["kinds"]),
            "extensions": dict(sorted(entry["exts"].items(), key=lambda kv: -kv[1])),
        })
    return out


def _group_items(rows: list[dict]) -> list[dict]:
    """Collapse an item kind across documents by name (case-insensitively)."""
    groups: dict[str, dict] = {}
    for row in rows:
        key = row["name"].strip().lower()
        group = groups.setdefault(key, {
            "name": row["name"].strip(), "documents": [], "details": [], "extra": []
        })
        group["documents"].append({"id": row["document_id"], "name": row["display_name"]})
        if row.get("detail"):
            group["details"].append(row["detail"])
        if row.get("extra"):
            try:
                group["extra"].append(json.loads(row["extra"]))
            except (TypeError, ValueError):
                pass
    ordered = sorted(groups.values(), key=lambda g: (-len(g["documents"]), g["name"].lower()))
    for group in ordered:
        group["count"] = len(group["documents"])
    return ordered


# A trigger or cadence is a phrase ("month-end close", "form submission").
# The model occasionally answers one with an essay instead, and a single
# runaway value can be larger than the rest of the section put together, so
# every free-text value is clipped before it reaches the page.
MAX_FIELD_CHARS = 400


def _clip(value, limit: int = MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\u2026"


def _merge_process(group: dict) -> dict:
    actors, systems, outputs, cadences, triggers = set(), set(), set(), set(), set()
    for extra in group["extra"]:
        actors.update(_clip(a, 120) for a in (extra.get("actors") or []) if a)
        systems.update(_clip(s, 120) for s in (extra.get("systems") or []) if s)
        outputs.update(_clip(o, 120) for o in (extra.get("outputs") or []) if o)
        if extra.get("cadence"):
            cadences.add(_clip(extra["cadence"], 120))
        if extra.get("trigger"):
            triggers.add(_clip(extra["trigger"]))
    return {
        "name": group["name"],
        "count": group["count"],
        "actors": sorted(actors),
        "systems": sorted(systems),
        "outputs": sorted(outputs),
        "cadences": sorted(cadences),
        "triggers": sorted(triggers),
        "documents": group["documents"],
    }


def _coverage(documents: list[dict]) -> dict:
    dates = [d["period_start"] for d in documents if d["period_start"]]
    dates += [d["period_end"] for d in documents if d["period_end"]]
    dates += [d["doc_date"] for d in documents if d["doc_date"]]
    kinds = defaultdict(int)
    domains = defaultdict(int)
    for doc in documents:
        kinds[doc["kind"]] += 1
        if doc["domain"]:
            domains[doc["domain"]] += 1
    return {
        "documents": len(documents),
        "representatives": sum(1 for d in documents if d["representative"]),
        "variants": sum(1 for d in documents if not d["representative"]),
        "mapped": sum(1 for d in documents if d["purpose"]),
        "unmapped": sum(1 for d in documents if not d["purpose"]),
        "needs_ocr": sum(1 for d in documents if d["needs_ocr"]),
        "total_tokens": sum(d["token_estimate"] for d in documents),
        "kinds": dict(kinds),
        "domains": dict(sorted(domains.items(), key=lambda kv: -kv[1])),
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
        "tabular_rows": sum(d["profile_data"].get("total_rows") or 0
                            for d in documents if d["kind"] == "tabular"),
    }


# --------------------------------------------------------------------------
# content selection


def select_content(documents: list[dict], token_budget: int) -> tuple[list[dict], list[dict]]:
    """Pick which documents get their full text into the pack.

    Highest analytical value first, representatives only, until the budget is
    spent. Everything else is still fully described by its profile and its
    entry in the index — it is left out of `content/`, not out of the pack."""
    candidates = [d for d in documents if d["representative"] and d["markdown"].strip()]
    candidates.sort(key=lambda d: (-(d["value_score"] or 0), -d["token_estimate"], d["id"]))
    included, excluded, spent = [], [], 0
    for doc in candidates:
        text = doc["markdown"][:MAX_DOC_CHARS]
        cost = _tokens(text)
        if spent + cost > token_budget and included:
            excluded.append(doc)
            continue
        included.append({**doc, "packed_text": text,
                         "packed_tokens": cost,
                         "truncated": len(doc["markdown"]) > MAX_DOC_CHARS})
        spent += cost
    return included, excluded


# --------------------------------------------------------------------------
# rendering


def _brief(cfg: Config, data: dict, coverage: dict,
           included: list[dict], excluded: list[dict], budget: int) -> str:
    counts = {kind: len(_group_items(data["items"][kind])) for kind in db.ITEM_KINDS}
    lines = [
        "# Analysis pack — brief",
        "",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M')} from {cfg.source_dir}_",
        "",
        "## What you are looking at",
        "",
        "This pack is a whole folder tree of business files — spreadsheets,",
        "documents and presentations — reduced to what can be reasoned over.",
        "Every file in the source corpus was hashed and deduplicated, converted",
        "to text and structurally profiled"
        + (", then read once by a model that recorded what it is and what it"
           " evidences." if coverage["mapped"] else
           ". The per-document model pass has not been run, so this pack carries"
           " structure and metadata but no recorded purpose, process or entity"
           " signals."),
        "Nothing here was analysed or interpreted: that is your job.",
        "",
        "## The goal this corpus was assembled for",
        "",
        cfg.prompt.strip() or "(no intention prompt set)",
        "",
        "## What is in the corpus",
        "",
        f"- **{coverage['documents']:,} unique documents** "
        f"({coverage['representatives']:,} distinct, {coverage['variants']:,} near-duplicate "
        "versions of those)",
        "- Formats: " + ", ".join(f"{n:,} {k}" for k, n in sorted(coverage["kinds"].items())),
        f"- Tabular data: **~{coverage['tabular_rows']:,} data rows** across all spreadsheets",
        f"- Time span of the content: {coverage['earliest'] or '?'} to {coverage['latest'] or '?'}",
        "- Business areas seen: "
        + (", ".join(f"{d} ({n})" for d, n in list(coverage["domains"].items())[:12])
           or "not classified"),
        f"- Signals recorded: {counts['process']} distinct processes, "
        f"{counts['system']} systems, {counts['entity']} entities, "
        f"{counts['metric']} metrics, {counts['issue']} data-quality issues",
        f"- Raw text of the whole corpus: ~{coverage['total_tokens']:,} tokens "
        f"— which is why this pack exists",
        "",
        "## How to read this pack",
        "",
        "| File | Use it for |",
        "|---|---|",
        "| `01_CORPUS_MAP.md` | Where things live: folder structure, volumes, formats |",
        "| `02_DATASETS.md` | The data dictionary — every sheet, column, type, range, join key |",
        "| `03_PROCESSES.md` | How the business runs: processes, actors, systems, cadence |",
        "| `04_ENTITIES_AND_SYSTEMS.md` | Who and what appears, and in which documents |",
        "| `05_TIMELINE.md` | What happened when |",
        "| `06_QUESTIONS_AND_GAPS.md` | What this corpus can answer, and what it cannot |",
        "| `07_DOCUMENT_INDEX.md` | Every document: id, path, purpose, analytical value |",
        "| `content/` | Full text of the highest-value documents |",
        "| `pack.json` | All of the above, machine-readable |",
        "",
        "Cite everything as `[doc <id>]`; the id resolves in `07_DOCUMENT_INDEX.md`.",
        "",
        "## What is NOT here — read this before concluding anything",
        "",
        f"- **`content/` holds {len(included):,} of {coverage['representatives']:,} documents** "
        f"(~{sum(d['packed_tokens'] for d in included):,} of a {budget:,}-token budget). "
        f"The other {len(excluded):,} are described by profile and index entry only; "
        "their full text is in the project database (`docpipe search`).",
        "- Spreadsheets are represented by their **structure and a sample**, not their "
        "rows. Column ranges and row counts are real; individual values are not all here. "
        "Never state a total or an average from this pack — say which sheet to compute it from.",
        f"- {coverage['needs_ocr']:,} documents had no extractable local text (scans or "
        "images) and were read by the model directly from the original file.",
        f"- {coverage['unmapped']:,} documents are profiled but not yet mapped by the model.",
        "- Near-duplicate versions were collapsed into families. Where a version history "
        "matters (which budget was final), check the family members in the index.",
        "- Relevance triage already removed documents judged unrelated to the goal above. "
        "Absence of evidence here is not evidence of absence in the source drive.",
        "",
        "## Suggested first pass",
        "",
        "1. Read `01_CORPUS_MAP.md` and `03_PROCESSES.md` together — the folder",
        "   structure and the observed processes usually describe the same",
        "   operating model from two directions, and where they disagree is",
        "   itself a finding.",
        "2. Read `02_DATASETS.md` and decide which sheets can actually carry",
        "   quantitative claims, and which joins are available between them.",
        "3. Read `06_QUESTIONS_AND_GAPS.md`, then pick the questions worth",
        "   answering and pull those documents from `content/`.",
        "",
    ]
    return "\n".join(lines)


def _corpus_map(data: dict, coverage: dict) -> str:
    lines = ["# Corpus map", "",
             "Folder structure of the source drive, with volumes. Folder names",
             "are usually the company's own taxonomy — treat them as evidence.",
             "", "| Folder | Docs | Formats | Est. tokens |", "|---|---:|---|---:|"]
    for entry in _folder_tree(data["documents"]):
        formats = ", ".join(f"{ext}×{n}" for ext, n in list(entry["extensions"].items())[:6])
        lines.append(f"| `{entry['folder']}` | {entry['documents']:,} | {formats} "
                     f"| {entry['tokens']:,} |")
    lines += ["", "## Near-duplicate families", "",
              f"{coverage['variants']:,} documents are near-identical versions of another "
              "document (same normalised name and structure). Only the newest of each",
              "family was sent to the model and packed; the rest are listed in the",
              "document index with their family id.", ""]
    families: dict[int, list] = defaultdict(list)
    for doc in data["documents"]:
        if doc["family_id"]:
            families[doc["family_id"]].append(doc)
    multi = [(fid, docs) for fid, docs in families.items() if len(docs) > 1]
    multi.sort(key=lambda pair: -len(pair[1]))
    if multi:
        lines += ["| Family | Versions | Newest (representative) |", "|---|---:|---|"]
        for fid, docs in multi[:50]:
            newest = next((d for d in docs if d["representative"]), docs[0])
            lines.append(f"| {fid} | {len(docs)} | doc {newest['id']} `{newest['name']}` |")
        lines.append("")
    else:
        lines += ["No multi-version families found.", ""]
    return "\n".join(lines)


def _datasets(data: dict) -> str:
    lines = ["# Datasets — data dictionary", "",
             "Every tabular file, sheet by sheet. Row counts are real counts;",
             "column types, ranges and examples come from a sample of up to",
             f"{profile_mod.SAMPLE_ROWS:,} rows per sheet. Use this to decide what can be",
             "computed and where, before asking for any file's contents.", "",
             "One entry per distinct workbook: near-identical versions share a",
             "profile and are listed under their representative.", ""]
    tabular = [d for d in data["documents"] if d["kind"] == "tabular"]
    if not tabular:
        return "\n".join(lines + ["No spreadsheets in this corpus.", ""])

    variants: dict[int, list] = defaultdict(list)
    for doc in tabular:
        if not doc["representative"] and doc["family_id"]:
            variants[doc["family_id"]].append(doc)

    for doc in sorted([d for d in tabular if d["representative"]],
                      key=lambda d: (-(d["value_score"] or 0), d["id"])):
        lines += [f"## doc {doc['id']} — `{doc['name']}`", "",
                  f"- Path: `{doc['rel_path']}`",
                  f"- Purpose: {doc['purpose'] or doc['summary'] or '(not mapped)'}"]
        if doc["domain"]:
            lines.append(f"- Domain: {doc['domain']}")
        if doc["period_start"] or doc["period_end"]:
            lines.append(f"- Period covered: {doc['period_start'] or '?'} to "
                         f"{doc['period_end'] or '?'}")
        if doc["joins"]:
            lines.append(f"- Candidate join keys: {', '.join(doc['joins'])}")
        siblings = variants.get(doc["family_id"] or -1, [])
        if siblings:
            lines.append("- Other versions of this file: "
                         + ", ".join(f"doc {s['id']} `{s['name']}`" for s in siblings))
        lines += ["", "```", doc["profile"], "```", ""]
    return "\n".join(lines)


def _processes(data: dict) -> str:
    groups = [_merge_process(g) for g in _group_items(data["items"]["process"])]
    lines = ["# Processes observed", "",
             "Business processes visible in the files themselves — an approval",
             "column, a status field, a reconciliation tab, a written procedure.",
             "Each is listed with how many documents evidence it. A process",
             "evidenced by one document is a claim; one evidenced by twenty is a",
             "pattern.", ""]
    if not groups:
        return "\n".join(lines + ["No processes were recorded.", ""])
    for group in groups:
        lines.append(f"## {group['name']}  ({group['count']} document"
                     f"{'s' if group['count'] != 1 else ''})")
        lines.append("")
        if group["cadences"]:
            lines.append(f"- **Cadence:** {', '.join(group['cadences'][:6])}")
        if group["triggers"]:
            lines.append(f"- **Triggered by:** {'; '.join(group['triggers'][:4])}")
        if group["actors"]:
            lines.append(f"- **Actors:** {', '.join(group['actors'][:15])}")
        if group["systems"]:
            lines.append(f"- **Systems:** {', '.join(group['systems'][:15])}")
        if group["outputs"]:
            lines.append(f"- **Outputs:** {', '.join(group['outputs'][:10])}")
        evidence = ", ".join(f"doc {d['id']}" for d in group["documents"][:15])
        more = f" (+{group['count'] - 15} more)" if group["count"] > 15 else ""
        lines += [f"- **Evidence:** {evidence}{more}", ""]
    return "\n".join(lines)


def _entities_and_systems(data: dict) -> str:
    lines = ["# Entities and systems", ""]
    for kind, title, note in (
        ("system", "Systems and tools",
         "Software the business runs on, as referenced in the files."),
        ("entity", "Entities",
         "Organisations, people, products and projects named in the files."),
        ("metric", "Metrics reported",
         "Quantities the corpus measures — and therefore what can be trended."),
    ):
        groups = _group_items(data["items"][kind])
        lines += [f"## {title}", "", note, ""]
        if not groups:
            lines += ["(none recorded)", ""]
            continue
        lines += ["| Name | Docs | Notes | Where |", "|---|---:|---|---|"]
        for group in groups[:300]:
            detail = "; ".join(dict.fromkeys(group["details"]))[:200]
            where = ", ".join(f"doc {d['id']}" for d in group["documents"][:8])
            if group["count"] > 8:
                where += f" (+{group['count'] - 8})"
            lines.append(f"| {group['name']} | {group['count']} | {detail} | {where} |")
        if len(groups) > 300:
            lines.append(f"| _... {len(groups) - 300} more in pack.json_ | | | |")
        lines.append("")
    return "\n".join(lines)


def _timeline(data: dict) -> str:
    lines = ["# Timeline", "",
             "Documents by the period their content covers, then the dated facts",
             "the extraction pass recorded. Use it to see what the corpus covers",
             "in time — and where it goes quiet.", "",
             "| Date | Document | What it is |", "|---|---|---|"]
    dated = [d for d in data["documents"]
             if d["period_start"] or d["doc_date"]]
    dated.sort(key=lambda d: (d["period_start"] or d["doc_date"] or "", d["id"]))
    for doc in dated[:MAX_TIMELINE_ROWS]:
        when = doc["period_start"] or doc["doc_date"]
        if doc["period_end"] and doc["period_end"] != when:
            when = f"{when} → {doc['period_end']}"
        lines.append(f"| {when} | doc {doc['id']} `{doc['name']}` "
                     f"| {(doc['purpose'] or doc['summary'] or '')[:160]} |")
    if len(dated) > MAX_TIMELINE_ROWS:
        lines.append(f"| _... {len(dated) - MAX_TIMELINE_ROWS} more_ | | |")
    lines.append("")

    if data["facts"]:
        lines += ["## Dated facts", "",
                  "From the core extraction pass (`facts` table).", "",
                  "| As of | Entity | Attribute | Value | Source |", "|---|---|---|---|---|"]
        for fact in data["facts"][:MAX_FACTS]:
            lines.append(f"| {fact['as_of_date'] or '?'} | {fact['entity']} "
                         f"| {fact['attribute']} | {str(fact['value'])[:120]} "
                         f"| doc {fact['document_id']} |")
        if len(data["facts"]) > MAX_FACTS:
            lines.append(f"| _... {len(data['facts']) - MAX_FACTS} more_ | | | | |")
        lines.append("")
    return "\n".join(lines)


def _questions_and_gaps(data: dict, coverage: dict) -> str:
    questions = _group_items(data["items"]["question"])
    issues = _group_items(data["items"]["issue"])
    lines = ["# Questions and gaps", "",
             "## Questions this corpus can answer", "",
             "Recorded per document while reading it, then grouped. Questions",
             "backed by several documents are the ones with enough evidence to",
             "answer properly.", ""]
    if questions:
        for group in questions[:200]:
            where = ", ".join(f"doc {d['id']}" for d in group["documents"][:6])
            lines.append(f"- {group['name']} _({group['count']}× — {where})_")
        if len(questions) > 200:
            lines.append(f"- _... {len(questions) - 200} more in pack.json_")
    else:
        lines.append("(none recorded)")

    lines += ["", "## Data-quality and process issues found", "",
              "Problems visible in the files themselves. These constrain what can",
              "be concluded, and several are findings in their own right.", ""]
    if issues:
        lines += ["| Issue | Docs | Detail |", "|---|---:|---|"]
        for group in issues[:150]:
            detail = "; ".join(dict.fromkeys(group["details"]))[:240]
            lines.append(f"| {group['name']} | {group['count']} | {detail} |")
    else:
        lines.append("(none recorded)")

    lines += ["", "## Structural gaps in the pack itself", "",
              f"- {coverage['unmapped']:,} of {coverage['documents']:,} documents are not "
              "mapped (no model read yet) — run `docpipe analysis map`.",
              f"- {coverage['needs_ocr']:,} documents have no local text (scans/images).",
              f"- Content coverage of the corpus in time: {coverage['earliest'] or '?'} to "
              f"{coverage['latest'] or '?'}; gaps inside that range are visible in "
              "`05_TIMELINE.md` and are worth asking about.",
              ""]
    return "\n".join(lines)


def _document_index(data: dict, included_ids: set[int]) -> str:
    lines = ["# Document index", "",
             "`in pack` = full text is in `content/`. Everything else is present",
             "as profile and metadata only; its text is in the project database.",
             "", "| id | value | in pack | file | folder | kind | purpose |",
             "|---:|---:|:-:|---|---|---|---|"]
    documents = sorted(data["documents"],
                       key=lambda d: (-(d["value_score"] or 0), d["id"]))
    for doc in documents:
        folder = doc["rel_path"].rsplit("/", 1)[0] if "/" in doc["rel_path"] else "(root)"
        value = f"{doc['value_score']:.2f}" if doc["value_score"] is not None else "—"
        marker = "✓" if doc["id"] in included_ids else ""
        family = f" _(family {doc['family_id']}, variant)_" if not doc["representative"] else ""
        purpose = (doc["purpose"] or doc["summary"] or "")[:180]
        lines.append(f"| {doc['id']} | {value} | {marker} | `{doc['name']}`{family} "
                     f"| `{folder}` | {doc['kind']} | {purpose} |")
    lines.append("")
    return "\n".join(lines)


def _content_file(doc: dict) -> str:
    header = [
        f"# doc {doc['id']} — {doc['name']}",
        "",
        f"- Path: `{doc['rel_path']}`",
        f"- Kind: {doc['kind']} ({doc['ext']})",
        f"- Purpose: {doc['purpose'] or doc['summary'] or '(not mapped)'}",
        f"- Period: {doc['period_start'] or '?'} to {doc['period_end'] or '?'}",
        f"- Analytical value: {doc['value_score'] if doc['value_score'] is not None else '—'}",
        "",
        "## Structure",
        "",
        "```",
        doc["profile"],
        "```",
        "",
        "## Content" + (" (truncated)" if doc["truncated"] else ""),
        "",
    ]
    return "\n".join(header) + doc["packed_text"] + "\n"


# --------------------------------------------------------------------------
# entry point


def build(project: Project, cfg: Config, token_budget: int = DEFAULT_TOKEN_BUDGET,
          relevant_only: bool = True) -> dict:
    """Build the analysis pack. Fully regenerable; no API key needed."""
    conn = project.connect()
    try:
        data = gather(conn, relevant_only=relevant_only)
    finally:
        conn.close()
    if not data["documents"]:
        raise FileNotFoundError(
            "No profiled documents. Run `docpipe analysis profile` first "
            "(and the core pipeline before that)."
        )

    coverage = _coverage(data["documents"])
    included, excluded = select_content(data["documents"], token_budget)
    included_ids = {d["id"] for d in included}

    root = pack_dir(project)
    content_dir = root / "content"
    if content_dir.exists():
        shutil.rmtree(content_dir)
    content_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "00_BRIEF.md": _brief(cfg, data, coverage, included, excluded, token_budget),
        "01_CORPUS_MAP.md": _corpus_map(data, coverage),
        "02_DATASETS.md": _datasets(data),
        "03_PROCESSES.md": _processes(data),
        "04_ENTITIES_AND_SYSTEMS.md": _entities_and_systems(data),
        "05_TIMELINE.md": _timeline(data),
        "06_QUESTIONS_AND_GAPS.md": _questions_and_gaps(data, coverage),
        "07_DOCUMENT_INDEX.md": _document_index(data, included_ids),
    }
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")

    for doc in included:
        (content_dir / f"{doc['id']:05d}_{_safe_name(doc['name'])}.md").write_text(
            _content_file(doc), encoding="utf-8"
        )

    machine = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_dir": cfg.source_dir,
        "goal": cfg.prompt,
        "coverage": coverage,
        "token_budget": token_budget,
        "packed_tokens": sum(d["packed_tokens"] for d in included),
        "folders": _folder_tree(data["documents"]),
        "processes": [_merge_process(g) for g in _group_items(data["items"]["process"])],
        "systems": _group_items(data["items"]["system"]),
        "entities": _group_items(data["items"]["entity"]),
        "metrics": _group_items(data["items"]["metric"]),
        "issues": _group_items(data["items"]["issue"]),
        "questions": _group_items(data["items"]["question"]),
        "documents": [
            {k: v for k, v in doc.items() if k not in ("markdown", "profile_data")}
            | {"in_pack": doc["id"] in included_ids}
            for doc in data["documents"]
        ],
    }
    (root / "pack.json").write_text(
        json.dumps(machine, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "pack": root,
        "files": [root / name for name in files],
        "content_dir": content_dir,
        "included": len(included),
        "excluded": len(excluded),
        "packed_tokens": machine["packed_tokens"],
        "coverage": coverage,
    }


def status_lines(project: Project) -> list[str]:
    conn = project.connect()
    try:
        counts = db.counts(conn)
    finally:
        conn.close()
    return [
        f"Documents in scope:      {counts['relevant']:,} (not triaged out)",
        f"Profiled:                {counts['profiled']:,}",
        f"Families (distinct):     {counts['families']:,} "
        f"({counts['representatives']:,} representatives to map)",
        f"Mapped by the model:     {counts['mapped']:,}",
        f"Signals recorded:        {counts['items']:,}",
    ]
