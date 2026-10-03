"""The Corpus Ingest Contract, encoded as a producer-side validator.

This is a local mirror of the corpus loader's `corpus/detoil_corpus/schema.py` (the
consuming schema), transcribed from CORPUSINGESTCONTRACT.md. It exists so the
producer can validate its own output *before* emitting — the contract loads
fail-closed (one bad value aborts the whole file), so we validate here and
refuse to write anything non-conforming.

It covers all 13 registers, not just the ones docpipe emits, so
``docpipe corpus validate <file>`` is a genuine conformance checker for any
external producer targeting the same contract.

Natural keys (§2):
- §2a registers require an explicit key; omitting it rejects the row.
- §2b registers derive the key (see ``derive_uid``) if it is omitted.

Coercion (§4): only ``title``/``rich_text`` fields may carry nested
dict/list values (they get JSON-stringified); a nested value in any other
field aborts the load, as does a non-numeric ``number`` or a non-ISO
``date``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime

# --- Types ------------------------------------------------------------------

TITLE = "title"
RICH_TEXT = "rich_text"
NUMBER = "number"
SELECT = "select"
DATE = "date"
EMAIL = "email"
PHONE = "phone_number"
URL = "url"
CHECKBOX = "checkbox"

TEXT_TYPES = {TITLE, RICH_TEXT}  # the only types that tolerate nested values

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CorpusError(Exception):
    """A conformance violation that would abort the whole load (fail-closed)."""


class SchemaError(CorpusError):
    """A row-level violation (e.g. a missing required natural key)."""


@dataclass(frozen=True)
class Field:
    type: str
    options: tuple = ()  # select option set


@dataclass(frozen=True)
class Register:
    name: str
    key: str  # the natural-key (title) field
    key_required: bool  # §2a (True) vs §2b derivable (False)
    fields: dict
    derive_from: tuple = ()  # source fields for §2b hash derivation


def _bbbee_options() -> tuple:
    return tuple([str(n) for n in range(1, 9)] + ["exempt"])


# --- The 13 registers -------------------------------------------------------

REGISTERS: dict = {}


def _reg(name, key, key_required, fields, derive_from=()):
    REGISTERS[name] = Register(name, key, key_required, fields, tuple(derive_from))


_reg("company_identity", "registration_number", True, {
    "registration_number": Field(TITLE),
    "legal_name": Field(RICH_TEXT),
    "incorporation_date": Field(DATE),
    "shareholders": Field(RICH_TEXT),
    "cipc_status": Field(SELECT, ("in_business", "deregistration_process", "deregistered")),
    "bbbee_level": Field(SELECT, _bbbee_options()),
    "briefing_time": Field(RICH_TEXT),
    "briefing_channel": Field(SELECT, ("telegram", "email", "whatsapp")),
})

_reg("financial_profile", "registration_number", True, {
    "registration_number": Field(TITLE),
    "income_tax_number": Field(RICH_TEXT),
    "vat_number": Field(RICH_TEXT),
    "paye_number": Field(RICH_TEXT),
    "uif_number": Field(RICH_TEXT),
    "bank_details": Field(RICH_TEXT),
    "accounting_backend": Field(SELECT, ("xero", "erpnext")),
    "financial_year_start": Field(DATE),
    "financial_year_end": Field(DATE),
})

_reg("financial_snapshot", "snapshot_date", True, {
    "snapshot_date": Field(TITLE),
    "bank_balance": Field(NUMBER),
    "accounts_receivable": Field(NUMBER),
    "accounts_payable": Field(NUMBER),
    "pl_ytd": Field(NUMBER),
    "outstanding_invoices": Field(RICH_TEXT),
    "outstanding_bills": Field(RICH_TEXT),
})

_reg("staff_register", "staff_uid", False, {
    "staff_uid": Field(TITLE),
    "id_number": Field(RICH_TEXT),
    "name": Field(RICH_TEXT),
    "role": Field(RICH_TEXT),
    "salary": Field(NUMBER),
    "notice_period": Field(RICH_TEXT),
    "status": Field(SELECT, ("active", "resigned", "terminated")),
    "hire_date": Field(DATE),
    "termination_date": Field(DATE),
}, derive_from=("name", "role"))

_reg("contract_register", "contract_uid", False, {
    "contract_uid": Field(TITLE),
    "contract_type": Field(SELECT, ("employment", "lease", "nda", "supplier",
                                    "customer", "sla", "other")),
    "counterparty": Field(RICH_TEXT),
    "address": Field(RICH_TEXT),
    "expiry_date": Field(DATE),
    "monthly_amount": Field(NUMBER),
    "notice_period": Field(RICH_TEXT),
    "key_obligations": Field(RICH_TEXT),
    "status": Field(SELECT, ("active", "expired", "terminated")),
}, derive_from=("contract_type", "counterparty", "address"))

_reg("compliance_calendar", "obligation_uid", False, {
    "obligation_uid": Field(TITLE),
    "category": Field(SELECT, ("SARS", "CIPC", "board", "renewal", "insurance", "B-BBEE")),
    "name": Field(RICH_TEXT),
    "due_date": Field(DATE),
    "alert_windows": Field(RICH_TEXT),
    "fired_windows": Field(RICH_TEXT),
    "status": Field(SELECT, ("open", "closed")),
}, derive_from=("category", "due_date", "name"))

_reg("task_queue", "task_uid", False, {
    "task_uid": Field(TITLE),
    "title": Field(RICH_TEXT),
    "task_type": Field(SELECT, ("meet_transcript", "hmcp_brief", "human_backlog", "corpus_write")),
    "detail": Field(RICH_TEXT),
    "due_date": Field(DATE),
    "status": Field(SELECT, ("pending", "in_progress", "done", "cancelled", "pending_review")),
    "payload": Field(RICH_TEXT),
}, derive_from=("task_type", "title", "due_date"))

_reg("document_index", "file_id", True, {
    "file_id": Field(TITLE),
    "filename": Field(RICH_TEXT),
    "doc_type": Field(SELECT, ("contract", "invoice", "statement", "receipt", "other")),
    "folder_path": Field(RICH_TEXT),
    "last_updated": Field(DATE),
    "confidence": Field(NUMBER),
})

_reg("supplier_register", "supplier_uid", True, {
    "supplier_uid": Field(TITLE),
    "name": Field(RICH_TEXT),
    "contact": Field(RICH_TEXT),
    "payment_terms": Field(RICH_TEXT),
    "contract_ref": Field(RICH_TEXT),
    "status": Field(SELECT, ("active", "inactive")),
})

_reg("customer_register", "customer_uid", True, {
    "customer_uid": Field(TITLE),
    "name": Field(RICH_TEXT),
    "contact": Field(RICH_TEXT),
    "contract_ref": Field(RICH_TEXT),
    "payment_status": Field(SELECT, ("current", "overdue", "suspended")),
    "status": Field(SELECT, ("active", "inactive")),
})

_reg("tool_access_register", "access_uid", False, {
    "access_uid": Field(TITLE),
    "tool": Field(RICH_TEXT),
    "person": Field(RICH_TEXT),
    "login": Field(EMAIL),
    "access_level": Field(RICH_TEXT),
    "granted_date": Field(DATE),
    "revoked_date": Field(DATE),
    "status": Field(SELECT, ("active", "revoked")),
}, derive_from=("tool", "person", "login"))

_reg("hmcp_provider_register", "slug", True, {
    "slug": Field(TITLE),
    "name": Field(RICH_TEXT),
    "provider_type": Field(SELECT, ("payroll", "accounting", "legal", "other")),
    "scope": Field(RICH_TEXT),
    "rate_card": Field(RICH_TEXT),
    "sla": Field(RICH_TEXT),
    "threshold": Field(RICH_TEXT),
    "contact": Field(RICH_TEXT),
})

_reg("audit_log", "event_uid", False, {
    "event_uid": Field(TITLE),
    "ts": Field(DATE),
    "actor": Field(RICH_TEXT),
    "workflow": Field(RICH_TEXT),
    "action": Field(RICH_TEXT),
    "status": Field(SELECT, ("ok", "error", "skipped")),
    "detail": Field(RICH_TEXT),
    "via": Field(SELECT, ("agent", "job", "manual")),
}, derive_from=("action", "ts", "detail"))


# --- Natural-key derivation (§2b) -------------------------------------------

def _hash_parts(parts) -> str:
    """SHA-256 of the parts joined by ``|`` (None -> sentinel), first 16 hex."""
    joined = "|".join("\x00" if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _sha256_hex(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def derive_uid(register: str, row: dict) -> str:
    """The natural key the loader would derive for a §2b row missing its key.

    Mirrors the documented v2 hashing so a producer can precompute keys that
    match exactly. Raises for §2a registers (they require an explicit key).
    """
    spec = REGISTERS.get(register)
    if spec is None:
        raise CorpusError(f"unknown register {register!r}")
    if spec.key_required:
        raise SchemaError(f"{register}.{spec.key} must be supplied explicitly (no derivation)")

    if register == "staff_register":
        id_number = row.get("id_number")
        if id_number not in (None, ""):
            return str(id_number).strip()
        return _hash_parts([row.get("name"), row.get("role")])
    if register == "audit_log":
        return _hash_parts([row.get("action"), row.get("ts"), _sha256_hex(row.get("detail"))])
    return _hash_parts([row.get(f) for f in spec.derive_from])


# --- Value validation (§4) --------------------------------------------------

def _is_iso8601(value) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        pass
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def _is_numeric(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def check_value(register: str, field_name: str, spec: Field, value) -> None:
    """Raise CorpusError/SchemaError if ``value`` would not coerce (§4)."""
    where = f"{register}.{field_name}"
    nested = isinstance(value, (dict, list))
    if nested and spec.type not in TEXT_TYPES:
        raise CorpusError(
            f"{where}: nested {type(value).__name__} is only allowed in "
            f"title/rich_text fields (would abort the load)"
        )
    if spec.type in TEXT_TYPES:
        return  # any scalar, dict or list is fine (JSON-stringified)
    if spec.type == NUMBER:
        if not _is_numeric(value):
            raise CorpusError(f"{where}: {value!r} is not a number or numeric string")
    elif spec.type == SELECT:
        if str(value) not in spec.options:
            raise CorpusError(
                f"{where}: {value!r} is not one of the declared options "
                f"{list(spec.options)}"
            )
    elif spec.type == DATE:
        if not _is_iso8601(value):
            raise CorpusError(f"{where}: {value!r} is not an ISO 8601 date/datetime")
    elif spec.type == EMAIL:
        if not (isinstance(value, str) and _EMAIL_RE.match(value)):
            raise CorpusError(f"{where}: {value!r} is not a valid email address")
    elif spec.type in (PHONE, URL):
        if not isinstance(value, str):
            raise CorpusError(f"{where}: {spec.type} must be a string")
    # checkbox accepts anything truthy/falsy — nothing to check.


@dataclass
class ValidationResult:
    registers: dict = field(default_factory=dict)  # register -> row count
    unknown_fields: list = field(default_factory=list)  # (register, field) skipped

    @property
    def rows(self) -> int:
        return sum(self.registers.values())


def validate_row(register: str, spec: Register, row: dict, result: ValidationResult) -> None:
    if not isinstance(row, dict):
        raise CorpusError(f"{register}: each row must be an object, got {type(row).__name__}")

    key_value = row.get(spec.key)
    if key_value in (None, ""):
        if spec.key_required:
            raise SchemaError(
                f"{register}: required natural key {spec.key!r} is missing "
                f"(this rejects the row)"
            )
        # §2b: derivable — must be able to derive from the source fields.
        derive_uid(register, row)  # raises if the sources are unusable

    for field_name, value in row.items():
        fspec = spec.fields.get(field_name)
        if fspec is None:
            result.unknown_fields.append((register, field_name))  # skipped, not fatal
            continue
        if value is None:
            continue  # a null field is skipped (property left unset)
        check_value(register, field_name, fspec, value)


def validate_document(document) -> ValidationResult:
    """Validate a whole corpus document against the contract.

    Raises CorpusError on the first fatal violation (an unknown register, a
    bad value, a missing required key). Unknown *fields* are collected as
    warnings on the result rather than raised.
    """
    if not isinstance(document, dict):
        raise CorpusError("the corpus document must be a single JSON object")

    result = ValidationResult()
    for register, rows in document.items():
        spec = REGISTERS.get(register)
        if spec is None:
            raise CorpusError(
                f"unknown register {register!r} — an unknown register key aborts "
                f"the entire load. Known registers: {sorted(REGISTERS)}"
            )
        if not isinstance(rows, list):
            raise CorpusError(f"{register}: value must be an array of rows")
        for row in rows:
            validate_row(register, spec, row, result)
        result.registers[register] = len(rows)
    return result


def loads_and_validate(text: str) -> ValidationResult:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"not valid JSON: {exc}") from exc
    return validate_document(document)


# --- Producer-side coercion & schema generation -----------------------------
#
# These support the targeted-extraction pass (``corpus extract``): they turn
# the register specs into a Gemini response schema, and sanitize the model's
# output field-by-field so we only ever emit values that will actually load.

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text) -> str:
    """Deterministic slug for minting §2a uids (e.g. 'Engen Fleet' -> 'engen-fleet')."""
    return _SLUG_RE.sub("-", str(text).strip().lower()).strip("-")


def coerce_or_none(register: str, field_name: str, value):
    """Return ``value`` if it would load for this field, else ``None``.

    This is the producer-side guard behind the contract's "validate types
    before emitting" (§4): a value that would abort the load downstream is
    dropped here instead, so our output stays conforming and fail-closed.
    """
    spec = REGISTERS.get(register)
    if spec is None:
        return None
    fspec = spec.fields.get(field_name)
    if fspec is None or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        check_value(register, field_name, fspec, value)
    except CorpusError:
        return None
    return value


def gemini_field_schema(fspec: Field) -> dict:
    """One field's schema in the google-genai (uppercase) dialect."""
    if fspec.type == NUMBER:
        node: dict = {"type": "NUMBER"}
    elif fspec.type == SELECT:
        node = {"type": "STRING", "enum": list(fspec.options)}
    else:  # title, rich_text, date, email, phone_number, url -> STRING
        node = {"type": "STRING"}
    node["nullable"] = True
    return node


def gemini_object_schema(register: str, skip_fields=()) -> dict:
    """An OBJECT schema for one register, omitting model-computed key fields."""
    spec = REGISTERS[register]
    skip = set(skip_fields)
    props = {
        name: gemini_field_schema(fspec)
        for name, fspec in spec.fields.items()
        if name not in skip
    }
    # A model-supplied extraction confidence (0-1), stripped before emit.
    props["confidence"] = {"type": "NUMBER", "nullable": True}
    return {"type": "OBJECT", "properties": props}
