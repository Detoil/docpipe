"""The due-diligence checklist that drives the dataroom module.

The baseline checklist is jurisdiction-neutral: the document set investors
ask for almost everywhere, with notes where names differ by country. It is
written to dataroom_checklist.json in the project directory so users can
edit it freely (add/remove items, change importance), and can optionally be
localised for a specific country with one LLM call at
`docpipe dataroom init --country ...` — the model proposes local additions
(statutory registrations, local compliance certificates) and local names
for existing items, which are merged into the JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path

from ... import gemini
from ...config import Config, Project
from . import prompts

FILE_NAME = "dataroom_checklist.json"

IMPORTANCE_LEVELS = ("required", "recommended", "conditional")

CATEGORIES = [
    {"code": "COR", "folder": "01_Corporate_and_Governance", "title": "Corporate & Governance"},
    {"code": "EQT", "folder": "02_Ownership_and_Equity", "title": "Ownership & Equity"},
    {"code": "FIN", "folder": "03_Financial", "title": "Financial"},
    {"code": "TAX", "folder": "04_Tax", "title": "Tax"},
    {"code": "CON", "folder": "05_Material_Contracts", "title": "Material Contracts"},
    {"code": "IPT", "folder": "06_Intellectual_Property_and_IT", "title": "Intellectual Property & IT"},
    {"code": "HRE", "folder": "07_Human_Resources", "title": "Human Resources"},
    {"code": "PRO", "folder": "08_Property_and_Assets", "title": "Property & Assets"},
    {"code": "REG", "folder": "09_Regulatory_and_Compliance", "title": "Regulatory & Compliance"},
    {"code": "INS", "folder": "10_Insurance", "title": "Insurance"},
    {"code": "LIT", "folder": "11_Litigation_and_Disputes", "title": "Litigation & Disputes"},
]

UNASSIGNED_FOLDER = "99_Unassigned"

# (code, category, importance, title, description, note)
_BASE_ITEMS = [
    ("COR-01", "COR", "required", "Certificate of incorporation / company registration",
     "Proof the company legally exists, issued by the company registry.",
     "Name varies by country (e.g. certificate of incorporation, CIPC registration documents, extract from the commercial register)."),
    ("COR-02", "COR", "required", "Constitutional documents",
     "The company's current constitution: memorandum of incorporation, articles of association, bylaws or equivalent, including all amendments.", None),
    ("COR-03", "COR", "required", "Statutory registers",
     "Registers of directors, members/shareholders and beneficial owners as required by local company law.", None),
    ("COR-04", "COR", "required", "Board minutes and resolutions (last 3 years)",
     "Minutes and written resolutions of the board of directors.", None),
    ("COR-05", "COR", "required", "Shareholder meeting minutes and resolutions",
     "AGM/EGM minutes and shareholder written resolutions.", None),
    ("COR-06", "COR", "required", "Group structure / organisation chart",
     "Diagram of the legal group structure (subsidiaries, holding companies) and management organogram.", None),
    ("COR-07", "COR", "recommended", "Company registry filings and annual returns",
     "Annual returns and other statutory filings made with the company registry, showing the company is in good standing.", None),
    ("EQT-01", "EQT", "required", "Capitalisation table / share register",
     "Current cap table: every holder of shares or other securities, class, number and percentage, fully diluted.", None),
    ("EQT-02", "EQT", "recommended", "Share certificates and transfer records",
     "Issued share certificates and the history of share issues and transfers.", None),
    ("EQT-03", "EQT", "required", "Shareholders' agreement",
     "Any agreement between shareholders governing the company (voting, pre-emption, tag/drag, vesting).", None),
    ("EQT-04", "EQT", "conditional", "Share option / employee incentive plans",
     "Option plan rules, grant letters and the option ledger, if any equity incentives exist.", None),
    ("EQT-05", "EQT", "conditional", "Previous investment and funding agreements",
     "Subscription agreements, convertible notes, SAFEs and side letters from prior funding rounds.", None),
    ("FIN-01", "FIN", "required", "Annual financial statements (last 3 years)",
     "Audited or independently reviewed annual financial statements; signed versions.", None),
    ("FIN-02", "FIN", "required", "Management accounts (current year)",
     "Recent management accounts: income statement, balance sheet, cash flow.", None),
    ("FIN-03", "FIN", "required", "Budget / financial forecast",
     "Current-year budget and forward-looking forecast or financial model.", None),
    ("FIN-04", "FIN", "conditional", "Loan and debt facility agreements",
     "All borrowing: bank facilities, loans, overdrafts, shareholder loans, plus security/collateral given.", None),
    ("FIN-05", "FIN", "recommended", "Fixed asset register",
     "Register of material assets owned by the company.", None),
    ("FIN-06", "FIN", "recommended", "Debtors and creditors ageing",
     "Aged accounts receivable and payable listings.", None),
    ("TAX-01", "TAX", "required", "Tax registration documents",
     "Registration with the tax authority for income/corporate tax, VAT/GST/sales tax and payroll taxes as applicable.",
     "Issuing authority varies by country (e.g. SARS, HMRC, IRS)."),
    ("TAX-02", "TAX", "required", "Tax clearance / good standing certificate",
     "Current confirmation from the tax authority that the company's tax affairs are in order, where the jurisdiction issues one.", None),
    ("TAX-03", "TAX", "required", "Filed tax returns (last 3 years)",
     "Corporate income tax returns and assessments; VAT/GST returns if applicable.", None),
    ("TAX-04", "TAX", "conditional", "Tax disputes, audits and rulings",
     "Correspondence on any tax audit, dispute, objection or advance ruling.", None),
    ("CON-01", "CON", "required", "Key customer contracts",
     "Contracts with the largest customers (typically the top 5-10 by revenue) and any framework agreements.", None),
    ("CON-02", "CON", "required", "Key supplier contracts",
     "Contracts with material suppliers and service providers the business depends on.", None),
    ("CON-03", "CON", "conditional", "Partnership, JV and distribution agreements",
     "Joint venture, partnership, agency, franchise or distribution agreements.", None),
    ("CON-04", "CON", "recommended", "Standard terms of business",
     "Standard customer terms and conditions / template contracts used in the ordinary course.", None),
    ("CON-05", "CON", "conditional", "Guarantees, indemnities and sureties",
     "Guarantees or indemnities given or received by the company.", None),
    ("IPT-01", "IPT", "conditional", "Registered intellectual property",
     "Trademark, patent and design registrations and applications, with renewal status.", None),
    ("IPT-02", "IPT", "required", "IP assignments to the company",
     "Assignments confirming the company owns IP created by founders, employees and contractors.", None),
    ("IPT-03", "IPT", "recommended", "Domain names and key software licences",
     "Domain registrations and licences for software the business depends on.", None),
    ("IPT-04", "IPT", "required", "Data protection / privacy compliance",
     "Privacy policy, data protection registrations and compliance documentation under the applicable regime.",
     "Regime varies by country (e.g. POPIA in South Africa, GDPR in the EU/UK, CCPA in California)."),
    ("HRE-01", "HRE", "required", "Employment contracts for key personnel",
     "Signed employment contracts for directors, executives and key staff.", None),
    ("HRE-02", "HRE", "required", "Employee census",
     "List of employees with role, start date, location and remuneration (payroll summary).", None),
    ("HRE-03", "HRE", "recommended", "HR policies / employee handbook",
     "Disciplinary, leave, remote-work and other workplace policies.", None),
    ("HRE-04", "HRE", "conditional", "Bonus, commission and incentive schemes",
     "Non-equity incentive arrangements for staff.", None),
    ("HRE-05", "HRE", "conditional", "Labour disputes and union agreements",
     "Any current or recent employment disputes, and collective/union agreements.", None),
    ("PRO-01", "PRO", "conditional", "Title deeds for owned property",
     "Title documents and any mortgages/encumbrances, if the company owns real property.", None),
    ("PRO-02", "PRO", "required", "Lease agreements",
     "Leases for premises and material equipment, with current terms and renewal dates.", None),
    ("REG-01", "REG", "required", "Operating licences and permits",
     "Licences, permits and registrations required to operate the business.", None),
    ("REG-02", "REG", "conditional", "Industry-specific certificates and accreditations",
     "Certificates investors or customers in the industry expect.",
     "Examples vary by country and sector (e.g. B-BBEE certificate in South Africa, ISO certifications)."),
    ("REG-03", "REG", "conditional", "AML / KYC and other financial-regulatory compliance",
     "Compliance policies and registrations if the company operates in a regulated financial sector.", None),
    ("INS-01", "INS", "required", "Insurance policy schedule",
     "Schedule of current insurance cover: insurer, risk, sums insured, renewal dates.", None),
    ("INS-02", "INS", "recommended", "Insurance claims history",
     "Claims made in the last 3 years and their outcomes.", None),
    ("LIT-01", "LIT", "required", "Litigation and disputes summary",
     "Current, pending or threatened litigation, arbitration or regulatory action — or a statement that there is none.", None),
    ("LIT-02", "LIT", "conditional", "Settlement agreements",
     "Settlements of past disputes with ongoing obligations.", None),
]


def base_checklist() -> dict:
    """The shipped, jurisdiction-neutral checklist as a JSON-able dict."""
    return {
        "version": 1,
        "jurisdiction": None,
        "categories": [dict(c) for c in CATEGORIES],
        "items": [
            {"code": code, "category": cat, "importance": importance,
             "title": title, "description": description, "note": note}
            for code, cat, importance, title, description, note in _BASE_ITEMS
        ],
    }


def path_for(project: Project) -> Path:
    return project.root / FILE_NAME


def load(project: Project) -> dict:
    p = path_for(project)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found — run `docpipe dataroom init` first "
            "(use --country to localise the checklist)."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def save(project: Project, checklist: dict) -> Path:
    p = path_for(project)
    p.write_text(json.dumps(checklist, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def render(checklist: dict) -> str:
    """Human/LLM-readable rendering of the checklist, grouped by category."""
    by_cat: dict[str, list[dict]] = {}
    for item in checklist["items"]:
        by_cat.setdefault(item["category"], []).append(item)
    lines = []
    for cat in checklist["categories"]:
        items = by_cat.get(cat["code"])
        if not items:
            continue
        lines.append(f"{cat['title']}:")
        for item in items:
            note = f" [{item['note']}]" if item.get("note") else ""
            lines.append(
                f"- {item['code']} ({item['importance']}): {item['title']} — "
                f"{item.get('description') or ''}{note}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def next_code(checklist: dict, category: str) -> str:
    """Next free item code within a category (e.g. REG-04)."""
    highest = 0
    for item in checklist["items"]:
        prefix, _, num = item["code"].partition("-")
        if prefix == category and num.isdigit():
            highest = max(highest, int(num))
    return f"{category}-{highest + 1:02d}"


def merge_localization(checklist: dict, data: dict, country: str) -> dict:
    """Merge an LLM localisation proposal into the checklist (pure function).

    Returns stats: items added, notes updated, entries skipped."""
    stats = {"added": 0, "notes": 0, "skipped": 0}
    valid_categories = {c["code"] for c in checklist["categories"]}
    by_code = {item["code"]: item for item in checklist["items"]}

    for add in data.get("additions") or []:
        cat = (add.get("category") or "").strip().upper()
        title = (add.get("title") or "").strip()
        if cat not in valid_categories or not title:
            stats["skipped"] += 1
            continue
        importance = (add.get("importance") or "recommended").strip().lower()
        if importance not in IMPORTANCE_LEVELS:
            importance = "recommended"
        checklist["items"].append({
            "code": next_code(checklist, cat),
            "category": cat,
            "importance": importance,
            "title": title,
            "description": (add.get("description") or "").strip(),
            "note": (add.get("note") or "").strip() or None,
        })
        stats["added"] += 1

    for upd in data.get("local_notes") or []:
        item = by_code.get((upd.get("code") or "").strip())
        note = (upd.get("note") or "").strip()
        if not item or not note:
            stats["skipped"] += 1
            continue
        item["note"] = note
        stats["notes"] += 1

    checklist["jurisdiction"] = country
    return stats


def localize(checklist: dict, country: str, cfg: Config) -> dict:
    """One non-batch LLM call proposing jurisdiction-specific adjustments."""
    client = gemini.get_client()
    text = gemini.generate(
        client,
        cfg.synthesis_model,
        prompts.LOCALIZE_SYSTEM.format(country=country),
        f"Baseline checklist:\n\n{render(checklist)}",
        schema=prompts.LOCALIZE_SCHEMA,
    )
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise gemini.GeminiError(f"Could not parse localisation response: {exc}")
    return merge_localization(checklist, data, country)
