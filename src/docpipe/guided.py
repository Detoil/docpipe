"""Guided mode: the zero-command-line way to use docpipe.

Starting `docpipe` with no subcommand (which is what the double-click
launchers do) lands here. The first start writes a plain-text settings
template next to the tool; once the user fills it in and starts again,
this module creates the project and walks the whole pipeline with plain
language messages, pausing at the human review gate.

All text here avoids jargon on purpose: the audience has never used a
terminal before.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import convert, db, extract, pipeline, report, review, scan, triage
from .config import Config, Project

SETTINGS_FILENAME = "docpipe_settings.txt"

SETTINGS_TEMPLATE = """\
=================================================================
 DOCPIPE SETTINGS

 Fill in the three sections below, save this file, then start
 the tool again (double-click docpipe.bat on Windows or
 docpipe.command on Mac).

 The explanation lines that came with this file are ignored:
 just write your answers on the empty lines in each section.
=================================================================

==== 1. YOUR DOCUMENTS FOLDER ====

Paste the full path of the folder that contains your documents
on the empty line below this text.
  Windows: right-click the folder in Explorer and choose
           "Copy as path", then paste here.
  Mac:     right-click the folder, hold the Option key, and
           choose "Copy ... as Pathname", then paste here.


==== 2. YOUR GEMINI API KEY ====

The tool uses Google's Gemini AI to read your documents.
Get a free key at https://aistudio.google.com/apikey
(sign in with a Google account, click "Create API key"),
then paste the key on the empty line below this text.
Please note: your documents will be sent to Google for processing.


==== 3. WHAT DO YOU WANT FROM THESE DOCUMENTS? ====

In your own words: what are you trying to find out, and which
kinds of documents count as relevant or irrelevant? Write as
much as you like below this line.

"""

_SECTION_RE = re.compile(r"^====\s*(\d)")
_TEMPLATE_LINES = {ln.strip() for ln in SETTINGS_TEMPLATE.splitlines() if ln.strip()}


def parse_settings(text: str) -> tuple[dict, list[str]]:
    """Extract the user's answers: any line inside a numbered section that
    did not come with the template. Returns (values, errors)."""
    sections: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        m = _SECTION_RE.match(raw.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        line = raw.strip()
        if not line or line in _TEMPLATE_LINES:
            continue
        sections[current].append(line)

    values = {"folder": "", "api_key": "", "goal": ""}
    errors: list[str] = []

    folder_lines = sections.get("1", [])
    if folder_lines:
        values["folder"] = folder_lines[0].strip().strip('"').strip("'")
    else:
        errors.append("Section 1 is empty: paste the path of your documents folder.")

    key_lines = sections.get("2", [])
    if key_lines:
        key = key_lines[0].strip()
        if len(key) < 10 or " " in key:
            errors.append("Section 2: that does not look like an API key "
                          "(get one at https://aistudio.google.com/apikey).")
        else:
            values["api_key"] = key
    else:
        errors.append("Section 2 is empty: paste your Gemini API key.")

    goal = "\n".join(sections.get("3", [])).strip()
    if goal:
        values["goal"] = goal
    else:
        errors.append("Section 3 is empty: describe what you want from the documents.")

    return values, errors


def run(project_root: Path, echo=print, ask=input) -> None:
    settings_path = Path.cwd() / SETTINGS_FILENAME

    if not settings_path.exists():
        settings_path.write_text(SETTINGS_TEMPLATE, encoding="utf-8")
        echo("")
        echo("Welcome to docpipe!")
        echo("")
        echo("A settings file has been created here:")
        echo(f"    {settings_path}")
        echo("")
        echo("What to do now:")
        echo("  1. Open that file (double-click it; Notepad or TextEdit is fine).")
        echo("  2. Fill in the three sections: where your documents are, your")
        echo("     Gemini API key, and what you want from the documents.")
        echo("  3. Save the file, then start this tool again.")
        return

    values, errors = parse_settings(settings_path.read_text(encoding="utf-8"))
    source = None
    if not errors:
        source = Path(values["folder"]).expanduser()
        if not source.is_dir():
            errors.append(f"The documents folder was not found: {source}")
    if errors:
        echo("")
        echo("The settings file needs attention:")
        for e in errors:
            echo(f"  - {e}")
        echo("")
        echo(f"Please edit {settings_path}")
        echo("save it, and start the tool again.")
        return

    # Guided users never touch environment variables; the key lives in the
    # settings file and is injected here for the API client.
    os.environ["GEMINI_API_KEY"] = values["api_key"]

    proj = Project(project_root)
    if not proj.exists():
        proj.create(Config(source_dir=str(source.resolve()), prompt=values["goal"]))
        echo(f"Created your project folder: {proj.root}")
    else:
        cfg = proj.config
        changed = False
        if cfg.prompt != values["goal"]:
            cfg.prompt = values["goal"]
            changed = True
        if Path(cfg.source_dir) != source.resolve():
            cfg.source_dir = str(source.resolve())
            changed = True
        if changed:
            proj.save_config()
            echo("Settings file changed: project updated.")

    _run_pipeline(proj, echo, ask)


def _explain_api_failure(exc: Exception, echo=print) -> None:
    """Translate API/network errors into something a first-time user can act on.

    Nothing is lost on failure: every completed step is saved, so starting
    the tool again resumes from where it stopped."""
    echo("")
    echo("  Something went wrong while talking to Gemini:")
    echo(f"    {exc}")
    msg = str(exc).lower()
    if "api key" in msg or "api_key" in msg or "permission" in msg or "401" in msg or "403" in msg:
        echo("  This usually means the API key in docpipe_settings.txt is wrong.")
        echo("  Check it against https://aistudio.google.com/apikey and try again.")
    elif "resolve" in msg or "connect" in msg or "network" in msg or "timed out" in msg:
        echo("  This looks like an internet connection problem.")
        echo("  Check your connection and start the tool again - it will resume.")
    else:
        echo("  Your progress so far is saved; start the tool again to retry.")


def _count(conn, where: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM documents WHERE {where}").fetchone()["n"]


def _run_pipeline(proj: Project, echo=print, ask=input) -> None:
    cfg = proj.config

    # Step 1+2: local work, free, always safe to re-run.
    conn = proj.connect()
    try:
        total = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
    finally:
        conn.close()
    if total == 0:
        echo("")
        echo("Step 1 of 6: looking through your folder (and all folders inside it)...")
        stats = scan.run(proj, cfg)
        echo(f"  Found {stats['seen']} files: {stats['new']} unique documents "
             f"({stats['duplicates']} duplicate copies merged, {stats['skipped']} "
             f"skipped as not processable).")

    conn = proj.connect()
    try:
        discovered = _count(conn, "status = 'discovered'")
    finally:
        conn.close()
    if discovered:
        echo("")
        echo(f"Step 2 of 6: converting {discovered} documents to text "
             "(this stays on your computer).")
        echo(f"  Big PDFs can be slow. Any file that takes more than "
             f"{cfg.convert_timeout_seconds} seconds is skipped; PDFs that "
             "cannot be read locally are sent to Gemini as files instead.")
        stats = convert.run(proj, cfg, progress=lambda m: echo("  " + m))
        msg = f"  Converted {stats['converted']} documents."
        if stats["needs_ocr"]:
            msg += (f" {stats['needs_ocr']} look like scans/images; the AI will "
                    "read those files directly.")
        if stats["failed"]:
            msg += f" {stats['failed']} could not be converted (listed in the final report)."
        echo(msg)

    # Step 3: triage batch.
    conn = proj.connect()
    try:
        to_triage = len(triage.pending_docs(conn))
        triage_pending = bool(db.pending_jobs(conn, "triage"))
    finally:
        conn.close()
    if to_triage and not triage_pending:
        echo("")
        echo(f"Step 3 of 6: sending {to_triage} documents to Gemini to sort "
             "relevant from irrelevant.")
        for line in pipeline.estimate_lines(proj):
            echo("  " + line)
        try:
            triage.submit(proj, cfg)
        except Exception as exc:
            _explain_api_failure(exc, echo)
            return
        triage_pending = True
    if triage_pending:
        echo("  Waiting for Gemini to finish. This can take minutes to hours;")
        echo("  leave this window open, or close it and start the tool again later.")
        try:
            pipeline.wait_for_jobs(proj, echo=lambda m: echo("  " + str(m)))
        except Exception as exc:
            _explain_api_failure(exc, echo)
            return

    # Step 4: the human review gate.
    conn = proj.connect()
    try:
        unreviewed = _count(conn, "status = 'triaged' AND reviewed = 0")
    finally:
        conn.close()
    if unreviewed:
        echo("")
        echo("Step 4 of 6: your turn! Gemini has sorted the documents.")
        csv_path, html_path = review.export(proj, cfg)
        echo("  Please check its verdicts:")
        echo(f"    Easy to read:  {html_path}")
        echo(f"    To make edits: {csv_path}  (opens in Excel or Numbers)")
        echo("  In the csv file, the 'relevant' column says 1 (keep) or 0 (drop).")
        echo("  Change any verdicts you disagree with and SAVE the file.")
        echo("")
        while True:
            answer = ask(
                "  Press Enter when you have saved your changes (or type A to "
                "accept all verdicts as-is, Q to quit and continue later): "
            ).strip().lower()
            if answer in ("q", "quit"):
                echo("  No problem. Start the tool again any time to continue.")
                return
            if answer in ("a", "accept", ""):
                stats = review.apply(proj, cfg, accept_all=(answer.startswith("a")))
                echo(f"  Applied. {stats['irrelevant']} irrelevant documents are "
                     f"linked in: {proj.irrelevant_dir}")
                break

    # Step 5: extraction batch.
    conn = proj.connect()
    try:
        to_extract = len(extract.pending_docs(conn))
        extract_pending = bool(db.pending_jobs(conn, "extract"))
    finally:
        conn.close()
    if to_extract and not extract_pending:
        echo("")
        echo(f"Step 5 of 6: sending {to_extract} relevant documents to Gemini "
             "to extract the facts and dates...")
        try:
            extract.submit(proj, cfg)
        except Exception as exc:
            _explain_api_failure(exc, echo)
            return
        extract_pending = True
    if extract_pending:
        echo("  Waiting for Gemini to finish (same as before: you can close "
             "this window and come back).")
        try:
            pipeline.wait_for_jobs(proj, echo=lambda m: echo("  " + str(m)))
        except Exception as exc:
            _explain_api_failure(exc, echo)
            return

    # Step 6: reports.
    echo("")
    echo("Step 6 of 6: writing your reports...")
    report_path = report.build(proj, cfg)
    briefing_path = None
    try:
        briefing_path = report.synthesise_briefing(proj, cfg)
    except Exception as exc:  # the deterministic report is still valid
        echo(f"  (Could not write the narrative briefing: {exc})")

    echo("")
    echo("All done! Your results:")
    echo(f"  Overview report:        {report_path}")
    if briefing_path:
        echo(f"  Narrative briefing:     {briefing_path}")
    echo(f"  Irrelevant documents:   {proj.irrelevant_dir}")
    echo(f"  Full database (all document text and every fact with its source):")
    echo(f"                          {proj.db_path}")
    echo("")
    echo("Your original documents folder was not modified in any way.")
