import csv
import json
import re

import pytest
from conftest import make_response_line

from docpipe import convert, pipeline, scan, triage
from docpipe.config import Config, Project
from docpipe.modules import analysis
from docpipe.modules.analysis import db as adb
from docpipe.modules.analysis import mapping
from docpipe.modules.analysis import pack as pack_mod
from docpipe.modules.analysis import profile as profile_mod


@pytest.fixture
def data_source(tmp_path):
    """A miniature company drive: sheets, a deck, documents, and
    three saved versions of the same workbook."""
    src = tmp_path / "drive"
    (src / "finance").mkdir(parents=True)
    (src / "finance" / "archive").mkdir()
    (src / "ops").mkdir()

    header = ["invoice_id", "customer", "amount", "issued_on", "status"]
    rows = [
        [f"INV-{n:04d}", f"Customer {n % 7}", f"{100 + n * 3}.50",
         f"2023-{(n % 12) + 1:02d}-05", "paid" if n % 3 else "overdue"]
        for n in range(1, 301)
    ]

    def write_csv(path, extra_rows=()):
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Debtors extract"])  # title row before the header
            writer.writerow([])
            writer.writerow(header)
            writer.writerows(rows)
            writer.writerows(extra_rows)

    write_csv(src / "finance" / "debtors_v1.csv")
    write_csv(src / "finance" / "debtors_v2_FINAL.csv",
              extra_rows=[["INV-0301", "Customer 3", "999.00", "2023-12-01", "paid"]])
    write_csv(src / "finance" / "archive" / "debtors (copy).csv",
              extra_rows=[["INV-0302", "Customer 4", "12.00", "2023-12-09", "paid"]])

    (src / "ops" / "process_notes.md").write_text(
        "# Order to cash\n\n"
        "## Step 1 — capture\nOrders are captured in Salesforce by the sales team.\n\n"
        "## Step 2 — invoice\nFinance raises the invoice in Xero every Friday.\n"
    )
    (src / "ops" / "handbook.txt").write_text(
        "Operations handbook 2023. Approvals above R50 000 go to the COO."
    )
    return src


@pytest.fixture
def data_project(tmp_path, data_source):
    proj = Project(tmp_path / "proj")
    proj.create(Config(source_dir=str(data_source),
                       prompt="Find operational insights across this drive."))
    scan.run(proj, proj.config)
    convert.run(proj, proj.config)
    return proj


def _reviewed(project):
    """Triage everything as relevant and mark it reviewed."""
    conn = project.connect()
    lines = [
        make_response_line(f"doc-{doc['id']}", {
            "relevant": True, "reason": "test", "category": "record",
            "doc_date": "2023-06-01", "summary": "A test document.",
        })
        for doc in triage.pending_docs(conn)
    ]
    triage.ingest_lines(conn, lines)
    conn.execute("UPDATE documents SET reviewed = 1")
    conn.commit()
    conn.close()
    return project


# --------------------------------------------------------------------------
# profiling


def test_profiles_columns_and_types_of_a_spreadsheet(data_project):
    profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    row = conn.execute(
        "SELECT p.* FROM analysis_profiles p JOIN documents d ON d.id = p.document_id"
        " WHERE d.display_name = 'debtors_v1.csv'"
    ).fetchone()
    conn.close()

    assert row["kind"] == "tabular"
    sheet = adb.load_profile(row)["sheets"][0]
    # The title/blank rows above the real header are skipped.
    names = [c["name"] for c in sheet["column_profiles"]]
    assert names == ["invoice_id", "customer", "amount", "issued_on", "status"]
    assert sheet["rows"] == 300, "data rows, excluding the title and header rows"
    by_name = {c["name"]: c for c in sheet["column_profiles"]}
    assert by_name["amount"]["type"] == "number"
    assert by_name["issued_on"]["type"] == "date"
    assert by_name["status"]["distinct"] == 2
    assert by_name["invoice_id"]["filled"] == 300


def test_profile_is_a_fraction_of_the_source_file(data_source, data_project):
    profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    row = conn.execute(
        "SELECT p.* FROM analysis_profiles p JOIN documents d ON d.id = p.document_id"
        " WHERE d.display_name = 'debtors_v1.csv'"
    ).fetchone()
    conn.close()

    rendered = profile_mod.render_profile(row["kind"], adb.load_profile(row))
    source_chars = (data_source / "finance" / "debtors_v1.csv").stat().st_size
    assert len(rendered) < source_chars / 10
    assert "invoice_id" in rendered and "300 rows" in rendered


def test_profile_recovers_columns_the_markdown_conversion_lost(data_project):
    """MarkItDown flattens a CSV whose first row is a one-cell title into a
    single column. The native reader is why the profile still has all five."""
    profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    row = conn.execute(
        "SELECT p.*, d.markdown FROM analysis_profiles p"
        " JOIN documents d ON d.id = p.document_id"
        " WHERE d.display_name = 'debtors_v1.csv'"
    ).fetchone()
    conn.close()

    profile = adb.load_profile(row)
    assert profile["reader"] == "csv"
    assert len(profile["sheets"][0]["column_profiles"]) == 5
    assert "amount" not in row["markdown"], "the converted text really did lose them"


def test_text_profile_captures_headings(data_project):
    profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    row = conn.execute(
        "SELECT p.* FROM analysis_profiles p JOIN documents d ON d.id = p.document_id"
        " WHERE d.display_name = 'process_notes.md'"
    ).fetchone()
    conn.close()
    titles = [h["title"] for h in adb.load_profile(row)["headings"]]
    assert titles == ["Order to cash", "Step 1 — capture", "Step 2 — invoice"]


def test_versions_collapse_into_one_family(data_project):
    stats = profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    rows = conn.execute(
        "SELECT d.display_name, p.family_id, p.representative FROM analysis_profiles p"
        " JOIN documents d ON d.id = p.document_id WHERE d.ext = '.csv'"
        " ORDER BY d.display_name"
    ).fetchall()
    conn.close()

    families = {r["family_id"] for r in rows}
    assert len(rows) == 3 and len(families) == 1, "the three debtor exports are one family"
    assert sum(r["representative"] for r in rows) == 1, "exactly one representative"
    assert stats["variants"] == 2


def test_different_documents_stay_in_separate_families(data_project):
    profile_mod.run(data_project, data_project.config)
    conn = data_project.connect()
    rows = conn.execute(
        "SELECT d.display_name, p.family_id FROM analysis_profiles p"
        " JOIN documents d ON d.id = p.document_id"
        " WHERE d.display_name IN ('process_notes.md', 'handbook.txt')"
    ).fetchall()
    conn.close()
    assert len({r["family_id"] for r in rows}) == 2


def test_profiling_is_incremental(data_project):
    first = profile_mod.run(data_project, data_project.config)
    assert first["profiled"] == 5
    again = profile_mod.run(data_project, data_project.config)
    assert again["profiled"] == 0
    assert again["families"] == first["families"]


def test_signature_ignores_version_noise():
    profile = {"sheets": [{"name": "s", "column_profiles": [{"name": "a"}]}]}
    base = profile_mod.signature("tabular", "Budget.xlsx", profile)
    assert profile_mod.signature("tabular", "Budget v2 FINAL.xlsx", profile) == base
    assert profile_mod.signature("tabular", "Budget (1) copy.xlsx", profile) == base
    # A different shape is a different family, same name or not.
    other = {"sheets": [{"name": "s", "column_profiles": [{"name": "z"}]}]}
    assert profile_mod.signature("tabular", "Budget.xlsx", other) != base


# --------------------------------------------------------------------------
# the batch pass


SIGNAL = {
    "purpose": "Accounts-receivable extract listing open and paid invoices.",
    "domain": "Finance",
    "record_type": "transaction export",
    "period_start": "2023-01-05",
    "period_end": "2023-12-05",
    "processes": [{
        "name": "Order to cash", "trigger": "Order captured", "cadence": "weekly",
        "actors": ["Finance"], "systems": ["Xero"], "outputs": ["Invoice"],
        "evidence": "status column",
    }],
    "systems": [{"name": "Xero", "used_for": "invoicing"}],
    "entities": [{"name": "Customer 3", "type": "customer", "role": "debtor"}],
    "metrics": [{"name": "Debtor days", "unit": "days", "period": "FY2023",
                 "location": "amount column"}],
    "issues": [{"name": "Title rows above the header", "detail": "Breaks naive imports."}],
    "questions": ["Which customers are persistently overdue?"],
    "analysis_value": 0.9,
    "joins": ["invoice_id", "customer"],
}


def _mapped(project):
    profile_mod.run(project, project.config)
    conn = project.connect()
    docs = mapping.pending_docs(conn)
    mapping.ingest_lines(conn, [make_response_line(f"doc-{d['id']}", SIGNAL) for d in docs])
    conn.close()
    return project


def test_map_sends_only_family_representatives(data_project):
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    conn = proj.connect()
    representatives = mapping.pending_docs(conn)
    everything = mapping.pending_docs(conn, include_variants=True)
    conn.close()
    assert len(everything) == 5
    assert len(representatives) == 3, "the two extra debtor versions are not re-read"


def test_map_request_carries_the_profile_not_the_data(data_project):
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    conn = proj.connect()
    doc = next(d for d in mapping.pending_docs(conn) if d["kind"] == "tabular")
    system = mapping.prompts.MAP_SYSTEM.format(user_prompt=proj.config.prompt)
    request = mapping.build_request(conn, proj.config, doc, system)
    conn.close()

    text = request["request"]["contents"][0]["parts"][0]["text"]
    assert request["key"] == f"doc-{doc['id']}"
    assert "invoice_id: type=text" in text, "the column profile is in the prompt"
    assert re.search(r"\d{3} rows x 5 columns", text), "so is the real shape of the sheet"
    assert "2 — " in text, "the sibling versions are named"
    # The data itself is capped at the excerpt budget, however long the file is.
    excerpt = text.split("CONTENT EXCERPT", 1)[1]
    assert "(truncated)" in excerpt
    assert len(excerpt) < mapping.EXCERPT_CHARS["tabular"] + 200
    assert "INV-0300" not in text, "the tail of the data never reaches the model"
    assert request["request"]["generation_config"]["response_mime_type"] == "application/json"
    json.dumps(request)  # must be JSONL-serialisable


def test_ingest_records_signals_and_retries_failures(data_project):
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    conn = proj.connect()
    d1, d2, d3 = mapping.pending_docs(conn)
    stats = mapping.ingest_lines(conn, [
        make_response_line(f"doc-{d1['id']}", SIGNAL),
        make_response_line(f"doc-{d2['id']}", SIGNAL),
        {"key": f"doc-{d3['id']}", "status": {"code": 13, "message": "internal"}},
    ])
    assert stats["ok"] == 2 and stats["errors"] == 1
    assert stats["items"] == 12  # 6 signal items per document

    # The failed document stays pending for the next submit.
    assert [d["id"] for d in mapping.pending_docs(conn)] == [d3["id"]]
    row = conn.execute("SELECT * FROM analysis_signals WHERE document_id = ?",
                       (d1["id"],)).fetchone()
    assert row["domain"] == "finance" and row["value_score"] == 0.9
    process = conn.execute(
        "SELECT * FROM analysis_items WHERE document_id = ? AND kind = 'process'",
        (d1["id"],)).fetchone()
    assert process["name"] == "Order to cash"
    assert json.loads(process["extra"])["systems"] == ["Xero"]
    conn.close()


def test_ingest_is_idempotent(data_project):
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    conn = proj.connect()
    doc = mapping.pending_docs(conn)[0]
    line = make_response_line(f"doc-{doc['id']}", SIGNAL)
    mapping.ingest_lines(conn, [line])
    mapping.ingest_lines(conn, [line])
    n = conn.execute("SELECT count(*) AS n FROM analysis_items WHERE document_id = ?",
                     (doc["id"],)).fetchone()["n"]
    conn.close()
    assert n == 6


def test_ingest_coerces_bad_values(data_project):
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    conn = proj.connect()
    doc = mapping.pending_docs(conn)[0]
    mapping.ingest_lines(conn, [make_response_line(f"doc-{doc['id']}", {
        **SIGNAL, "analysis_value": "very high", "entities": [{"name": ""}],
    })])
    row = conn.execute("SELECT * FROM analysis_signals WHERE document_id = ?",
                       (doc["id"],)).fetchone()
    entities = conn.execute(
        "SELECT count(*) AS n FROM analysis_items WHERE document_id = ? AND kind = 'entity'",
        (doc["id"],)).fetchone()["n"]
    conn.close()
    assert row["value_score"] is None  # unparseable score dropped, row still stored
    assert entities == 0  # nameless entity dropped


# --------------------------------------------------------------------------
# the pack


def test_pack_builds_every_section(data_project):
    proj = _mapped(_reviewed(data_project))
    result = pack_mod.build(proj, proj.config)
    root = result["pack"]

    assert {p.name for p in root.glob("*.md")} == {
        "00_BRIEF.md", "01_CORPUS_MAP.md", "02_DATASETS.md", "03_PROCESSES.md",
        "04_ENTITIES_AND_SYSTEMS.md", "05_TIMELINE.md", "06_QUESTIONS_AND_GAPS.md",
        "07_DOCUMENT_INDEX.md",
    }

    brief = (root / "00_BRIEF.md").read_text()
    assert "Find operational insights across this drive." in brief
    assert "near-duplicate" in brief

    datasets = (root / "02_DATASETS.md").read_text()
    assert "invoice_id" in datasets and "issued_on" in datasets
    assert "Candidate join keys: invoice_id, customer" in datasets

    processes = (root / "03_PROCESSES.md").read_text()
    assert "Order to cash" in processes and "Xero" in processes

    entities = (root / "04_ENTITIES_AND_SYSTEMS.md").read_text()
    assert "Customer 3" in entities and "Debtor days" in entities

    questions = (root / "06_QUESTIONS_AND_GAPS.md").read_text()
    assert "persistently overdue" in questions
    assert "Title rows above the header" in questions

    index = (root / "07_DOCUMENT_INDEX.md").read_text()
    assert "debtors_v1.csv" in index and "variant" in index


def test_pack_json_is_machine_readable(data_project):
    proj = _mapped(_reviewed(data_project))
    result = pack_mod.build(proj, proj.config)
    data = json.loads((result["pack"] / "pack.json").read_text())

    assert data["goal"] == "Find operational insights across this drive."
    assert data["coverage"]["documents"] == 5
    assert data["coverage"]["variants"] == 2
    assert data["coverage"]["tabular_rows"] > 100
    process = data["processes"][0]
    assert process["name"] == "Order to cash" and process["systems"] == ["Xero"]
    assert all("markdown" not in doc for doc in data["documents"])
    assert any(doc["in_pack"] for doc in data["documents"])


def test_pack_respects_the_token_budget(data_project):
    proj = _mapped(_reviewed(data_project))
    small = pack_mod.build(proj, proj.config, token_budget=200)
    assert small["included"] == 1, "the budget always admits at least one document"
    assert small["excluded"] >= 1
    assert len(list(small["content_dir"].iterdir())) == 1
    assert small["packed_tokens"] > 0

    large = pack_mod.build(proj, proj.config, token_budget=1_000_000)
    assert large["included"] == 3, "representatives only, variants never packed"
    assert large["excluded"] == 0
    # Rebuilding clears the previous content/ rather than accumulating.
    assert len(list(large["content_dir"].iterdir())) == 3


def test_pack_content_files_carry_provenance(data_project):
    proj = _mapped(_reviewed(data_project))
    result = pack_mod.build(proj, proj.config)
    text = next(iter(result["content_dir"].iterdir())).read_text()
    assert text.startswith("# doc ")
    assert "- Path: `" in text and "## Structure" in text and "## Content" in text


def test_pack_works_without_the_llm_pass(data_project):
    """`profile` + `pack` alone must produce a usable pack — no API key."""
    proj = _reviewed(data_project)
    profile_mod.run(proj, proj.config)
    result = pack_mod.build(proj, proj.config)
    assert result["coverage"]["mapped"] == 0
    datasets = (result["pack"] / "02_DATASETS.md").read_text()
    assert "invoice_id" in datasets
    brief = (result["pack"] / "00_BRIEF.md").read_text()
    assert "not yet mapped" in brief


def test_pack_needs_profiles_first(data_project):
    with pytest.raises(FileNotFoundError):
        pack_mod.build(data_project, data_project.config)


def test_status_lines(data_project):
    proj = _mapped(_reviewed(data_project))
    lines = "\n".join(pack_mod.status_lines(proj))
    assert "Profiled:" in lines and "Mapped by the model:" in lines


def test_pipeline_dispatches_the_analysis_stage():
    ingestors = pipeline.stage_ingestors()
    assert ingestors["analysis_map"] is analysis.INGESTORS["analysis_map"]
    assert set(ingestors) >= {"triage", "extract", "dataroom_map", "analysis_map"}


def test_a_runaway_trigger_cannot_dominate_the_processes_page():
    """The model sometimes answers 'trigger' with an essay. One such value used
    to be copied into the page whole -- 155k characters in a real run, larger
    than every other process put together."""
    from docpipe.modules.analysis import pack as pack_mod

    group = {
        "name": "Month-end close",
        "count": 2,
        "documents": [],
        "extra": [
            {"trigger": "ledger " * 40_000, "cadence": "monthly",
             "actors": ["Finance"], "systems": ["Xero"], "outputs": ["Report"]},
            {"trigger": "Month-end close begins.", "cadence": "monthly",
             "actors": [], "systems": [], "outputs": []},
        ],
    }
    merged = pack_mod._merge_process(group)

    # asserted against a literal, not the module constant, so this test fails
    # on the unclipped code rather than erroring on a missing attribute
    assert all(len(t) <= 1_000 for t in merged["triggers"])
    assert any(t.endswith("…") for t in merged["triggers"])
    # the short, useful trigger survives alongside the clipped one
    assert "Month-end close begins." in merged["triggers"]


def test_clipping_leaves_ordinary_values_untouched():
    from docpipe.modules.analysis import pack as pack_mod

    assert pack_mod._clip("Month-end close begins.") == "Month-end close begins."
    assert pack_mod._clip("  spaced   out\ntext ") == "spaced out text"


def test_header_row_survives_a_narrow_title_row_above_it():
    """A board-tool export opens with a board title, then a subtitle,
    then the real header. The title row is two cells wide and all text, so
    picking the first 'mostly text' row picks the title and every real column
    name is read as data instead."""
    rows = [
        ["AB | Example campaign", "Updates", None, None, None, None],
        ["Instructions for the board", None, None, None, None, None],
        ["Item ID", "Item Name", "Content Type", "User", "Created At", "Asset IDs"],
        [1000000001, "Example item one", "Update", "Test User",
         "10/September/2025", "2000000001"],
        [1000000002, "Example item two", "Update", "Test User",
         "10/September/2025", "2000000002"],
    ]

    header, body, index = profile_mod._pick_header(rows)

    assert index == 2, "the board title row was mistaken for the header"
    assert header[0] == "Item ID"
    assert header[5] == "Asset IDs"
    assert len(body) == 2, "rows above the header must not be counted as data"
    assert body[0][1] == "Example item one"


def test_header_row_is_still_the_first_when_there_is_no_title():
    rows = [
        ["invoice_id", "customer", "amount"],
        ["INV-0001", "Customer 1", "103.50"],
        ["INV-0002", "Customer 2", "106.50"],
    ]

    header, body, index = profile_mod._pick_header(rows)

    assert index == 0
    assert header == ["invoice_id", "customer", "amount"]
    assert len(body) == 2
