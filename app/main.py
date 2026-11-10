"""OpenOnboard API.

Flow:  applicant form -> POST /api/applications -> Middesk "create business"
       -> Middesk verifies (async) -> webhook or "Refresh" pulls results
       -> rules engine recommends -> human reviewer approves/declines.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config, core_banking, db, decision, disclosures, kyc, maintenance, products, staff
from .middesk import MiddeskError, get_client, redact_tins

app = FastAPI(title="OpenOnboard", version="0.2.0")
STATIC = Path(__file__).parent / "static"

MAX_PRODUCTS = 4
MAX_PRODUCTS_BOTH = 6  # a combined application can choose more accounts
MAX_DOC_BYTES = 10 * 1024 * 1024
MAX_DOCS_PER_APP = 10
DOC_TYPES = ("formation_document", "ein_letter", "ownership_document", "other")
# The file type is decided by the first bytes, never by the client's content type or extension.
MAGIC = ((b"%PDF-", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    # Default body echoes the submitted input, which can include a full TIN.
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


# Everything an applicant needs is public. Every other /api route is for reviewers, so a new route is
# protected by default. Webhooks are checked with their own HMAC signature instead.
_PUBLIC = (
    ("GET", re.compile(r"/api/(health|disclosures|products|maintenance/kinds)")),
    ("POST", re.compile(r"/api/applications")),
    ("POST", re.compile(r"/api/applications/[0-9a-fA-F-]{36}/documents")),
)


def reviewer_login_on() -> bool:
    return bool(config.REVIEWER_PASSWORD) or not config.MOCK_MODE


def _is_public(method: str, path: str) -> bool:
    if not path.startswith("/api/"):
        return True  # the page itself, API docs and the signed webhook
    return any(method == m and rx.fullmatch(path) for m, rx in _PUBLIC)


def _password_ok(header: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        _user, _, password = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(password.encode(), config.REVIEWER_PASSWORD.encode())


@app.middleware("http")
async def require_reviewer(request: Request, call_next):
    if request.method == "OPTIONS" or _is_public(request.method, request.url.path) or not reviewer_login_on():
        return await call_next(request)
    if not config.REVIEWER_PASSWORD:  # connected to Middesk but no password set: never serve reviewer data openly
        return JSONResponse({"detail": "Set REVIEWER_PASSWORD in .env to open the reviewer pages."}, status_code=503)
    if not _password_ok(request.headers.get("authorization", "")):
        return JSONResponse(
            {"detail": "Reviewer password required."},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OpenOnboard reviewers", charset="UTF-8"'},
        )
    return await call_next(request)


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    # Starlette buffers the whole multipart body before the handler runs, so refuse oversized ones up front.
    if request.url.path.endswith("/documents") and request.method == "POST":
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_DOC_BYTES + 64 * 1024:
            return JSONResponse(
                {"detail": f"The file is larger than {MAX_DOC_BYTES // (1024 * 1024)} MB."}, status_code=413
            )
    return await call_next(request)


# ---------- request models ----------
class Owner(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    title: str | None = Field(default=None, max_length=80)
    ownership_pct: float | None = Field(default=None, ge=0, le=100)
    control_person: bool = False

    @field_validator("name")
    @classmethod
    def name_given(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Enter the owner's full name.")
        return v


def _clean_state(v):
    if v is None:
        return v
    v = v.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", v):
        raise ValueError("Use the 2-letter state code, like NY")
    return v


def _clean_zip(v):
    if v is None:
        return v
    v = v.strip()
    if not re.fullmatch(r"\d{5}(-\d{4})?", v):
        raise ValueError("ZIP code must look like 10013 or 10013-1234")
    return v


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # Feb 29
        return today.replace(year=today.year - years, day=28)


class Individual(BaseModel):
    """A person on a personal account. Only the LAST 4 digits of the SSN are ever accepted or stored."""

    model_config = {"extra": "forbid"}  # a stray full "ssn" field is refused, not silently ignored

    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    ssn_last4: str
    email: str = Field(max_length=200)
    phone: str
    address_line1: str = Field(min_length=3, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=100)
    state: str
    postal_code: str

    @field_validator("full_name", "address_line1", "address_line2", "city")
    @classmethod
    def mask_tins(cls, v):
        return redact_tins(v.strip()) if v else v

    @field_validator("full_name")
    @classmethod
    def name_given(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Enter the person's full name.")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def adult(cls, v):
        today = datetime.now(UTC).date()
        if v >= today:
            raise ValueError("Date of birth must be in the past.")
        if v > _years_ago(today, 18):
            raise ValueError("Each applicant must be at least 18 years old.")
        return v

    @field_validator("ssn_last4")
    @classmethod
    def last4_only(cls, v):
        v = v.strip()
        if re.fullmatch(r"\d{4}", v):
            return v
        if len(re.sub(r"\D", "", v)) > 4:
            raise ValueError("Only the last 4 digits of the SSN are needed. Enter just those 4 digits.")
        raise ValueError("Enter exactly 4 digits.")

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        v = v.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", v):
            raise ValueError("Enter an email address like name@example.com")
        return v

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v):
        v = v.strip()
        if not re.fullmatch(r"[\d\s()+\-]{7,20}", v):
            raise ValueError("Enter a phone number with 7 to 20 digits, spaces or ( ) + -")
        return v

    @field_validator("state")
    @classmethod
    def two_letter_state(cls, v):
        return _clean_state(v)

    @field_validator("postal_code")
    @classmethod
    def zip_code(cls, v):
        return _clean_zip(v)


class ProductChoice(BaseModel):
    """One account the applicant wants, with its opening deposit and options. Checked against the SAMPLE catalog."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(max_length=80)
    initial_deposit_cents: int = Field(ge=0, le=products.MAX_DEPOSIT_CENTS)
    term_months: int | None = None
    overdraft_protection: bool = False
    debit_card: bool = False

    @model_validator(mode="after")
    def matches_product(self):
        p = products.get_product(self.product_id)
        if p is None:
            raise ValueError("That account is not available.")
        if self.initial_deposit_cents < p["min_opening_deposit_cents"]:
            raise ValueError(f"{p['name']} needs at least ${p['min_opening_deposit_cents'] / 100:,.2f} to open.")
        if p["kind"] == "certificate":
            if self.term_months not in p["terms_months"]:
                raise ValueError(f"Choose a term for {p['name']}.")
        elif self.term_months is not None:
            raise ValueError(f"{p['name']} does not have a term.")
        if p["kind"] != "checking" and (self.overdraft_protection or self.debit_card):
            raise ValueError(f"Overdraft protection and a debit card are only for checking accounts ({p['name']}).")
        return self


class ApplicationIn(BaseModel):
    account_type: Literal["business", "personal", "both"] = "business"
    channel: Literal["online", "in_branch"] = "online"
    created_by: str | None = Field(default=None, max_length=80)  # staff id of the banker, in-branch only
    business_name: str | None = Field(default=None, max_length=200)
    tin: str | None = None
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = None
    postal_code: str | None = None
    website: str | None = Field(default=None, max_length=300)
    owners: list[Owner] = Field(default_factory=list, max_length=10)
    individuals: list[Individual] = Field(default_factory=list, max_length=4)
    products: list[ProductChoice] = Field(default_factory=list, max_length=MAX_PRODUCTS_BOTH)
    disclosures_accepted: bool = False
    disclosure_version: str | None = None
    signature_name: str | None = Field(default=None, max_length=120)  # SIMULATED e-signature (typed name)

    @model_validator(mode="after")
    def account_fields(self):
        if self.account_type == "personal":
            if not self.individuals:
                raise ValueError("Add at least one applicant.")
            # Business-only fields are not used. The display name is the primary applicant.
            self.business_name = self.individuals[0].full_name
            self.tin = self.website = None
            self.owners = []
        else:
            if self.account_type == "both":
                if not self.individuals:
                    raise ValueError("Add at least one personal applicant.")
            else:
                self.individuals = []
            name = (self.business_name or "").strip()
            if len(name) < 2:
                raise ValueError("Enter the business name.")
            self.business_name = name
            if not (self.address_line1 and len(self.address_line1) >= 3):
                raise ValueError("Enter the business street address.")
            if not (self.city and len(self.city) >= 2):
                raise ValueError("Enter the city.")
            if not self.state:
                raise ValueError("Use the 2-letter state code, like NY")
            if not self.postal_code:
                raise ValueError("ZIP code must look like 10013 or 10013-1234")
        return self

    @model_validator(mode="after")
    def channel_ok(self):
        if self.channel == "in_branch":
            if not self.created_by:
                raise ValueError("Choose the banker helping the customer.")
            if staff.get_staff(self.created_by) is None:
                raise ValueError("That staff member was not found.")
        else:
            self.created_by = None
        return self

    @model_validator(mode="after")
    def products_ok(self):
        if not self.products:
            raise ValueError("Choose at least one account.")
        ids = [c.product_id for c in self.products]
        if len(set(ids)) != len(ids):
            raise ValueError("Each account can only be chosen once.")
        kinds = {products.get_product(i)["account_type"] for i in ids}
        if self.account_type == "both":
            if kinds != {"business", "personal"}:
                raise ValueError(
                    "Choose at least one business account and one personal account, or pick just one kind."
                )
        else:
            if len(ids) > MAX_PRODUCTS:
                raise ValueError(f"Choose up to {MAX_PRODUCTS} accounts.")
            if kinds != {self.account_type}:
                raise ValueError(f"Choose accounts that are for a {self.account_type} account.")
        return self

    @model_validator(mode="after")
    def disclosures_ok(self):
        # Checked here, not in a field validator, because those are skipped when the field is omitted.
        self.signature_name = (self.signature_name or "").strip()
        if not 2 <= len(self.signature_name) <= 120:
            raise ValueError("Type your full legal name to sign.")
        if not self.disclosures_accepted:
            raise ValueError("Please read and accept the account disclosures.")
        if self.disclosure_version != disclosures.DISCLOSURE_VERSION:
            raise ValueError("The disclosures changed. Please review them again.")
        return self

    @field_validator("owners")
    @classmethod
    def ownership_total(cls, v):
        if sum(o.ownership_pct or 0 for o in v) > 100:
            raise ValueError("Ownership adds up to more than 100%. Check the percentages.")
        return v

    @field_validator("tin")
    @classmethod
    def clean_tin(cls, v):
        if v in (None, ""):
            return None
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("TIN/EIN must have 9 digits, like 12-3456789")
        return digits

    @field_validator("state")
    @classmethod
    def two_letter_state(cls, v):
        return _clean_state(v)

    @field_validator("postal_code")
    @classmethod
    def zip_code(cls, v):
        return _clean_zip(v)


class DecisionIn(BaseModel):
    decision: Literal["approved", "declined"]
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("note")
    @classmethod
    def mask_tins(cls, v):
        return redact_tins(v) if v else v


class AssigneeIn(BaseModel):
    staff_id: str = Field(max_length=80)


class OfficerIn(BaseModel):
    staff_id: str | None = Field(default=None, max_length=80)


class MaintenanceIn(BaseModel):
    kind: Literal[maintenance.KIND_IDS]
    details: dict = Field(default_factory=dict)
    requested_by: str | None = Field(default=None, max_length=80)


class MaintenanceDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    decided_by: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("note")
    @classmethod
    def mask_tins(cls, v):
        return redact_tins(v) if v else v


class DepositIn(BaseModel):
    product_id: str | None = Field(default=None, max_length=80)  # default: the first account
    amount_cents: int = Field(ge=100, le=100_000_000)  # $1 to $1,000,000


# ---------- helpers ----------
def to_middesk_payload(a: ApplicationIn, app_id: str) -> dict:
    addr = {"address_line1": a.address_line1, "city": a.city, "state": a.state, "postal_code": a.postal_code}
    if a.address_line2:
        addr["address_line2"] = a.address_line2
    payload = {"name": a.business_name, "addresses": [addr], "external_id": app_id}
    if config.MIDDESK_ORDER_PRODUCT:
        payload["orders"] = [{"product": config.MIDDESK_ORDER_PRODUCT}]
    if a.tin:
        payload["tin"] = {"tin": a.tin}
    if a.website:
        payload["website"] = {"url": a.website}
    if a.owners:
        payload["people"] = [{"name": o.name} for o in a.owners]
    return payload


def masked(a: ApplicationIn) -> dict:
    d = a.model_dump(mode="json")
    d["tin"] = f"*****{a.tin[-4:]}" if a.tin else None
    d["signed_at"] = datetime.now(UTC).isoformat()
    return d


def clean_filename(name: str | None) -> str:
    """Display-only name: basename, no control characters or path separators, at most 120 characters."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:120] or "document"


def sniff(head: bytes) -> str | None:
    return next((ctype for magic, ctype in MAGIC if head.startswith(magic)), None)


def ref_id(app_id: str) -> str:
    return "KYB-" + app_id.replace("-", "")[:6].upper()


def csv_safe(value) -> str:
    """Stop spreadsheets from running user text as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip()[:1] in ("=", "+", "-", "@") or text[:1] in ("\t", "\r") else text


def account_type(app_row: dict) -> str:
    """ "business" (also for applications saved before account types existed), "personal" or "both"."""
    t = (app_row.get("applicant") or {}).get("account_type")
    return t if t in ("personal", "both") else "business"


def chosen_products(app_row: dict) -> list[dict]:
    """The accounts the applicant chose. Applications saved before product choices existed have none, so they
    get one default account: the first sample product for their account type."""
    chosen = (app_row.get("applicant") or {}).get("products") or []
    if chosen:
        return chosen
    kind = "personal" if account_type(app_row) == "personal" else "business"
    default = products.list_products(kind)[0]
    return [{"product_id": default["id"], "initial_deposit_cents": default["min_opening_deposit_cents"]}]


def try_open_account(app_row: dict) -> None:
    """SIMULATED. Ask the core to open one account per selected product that has none yet.
    A failure never undoes the human decision; it is recorded so it can be retried."""
    have = {a["product_id"] for a in app_row.get("accounts") or []}
    for choice in chosen_products(app_row):
        pid = choice["product_id"]
        if pid in have:
            continue
        try:
            account = core_banking.open_account(app_row, pid)
        except core_banking.CoreError as e:
            db.add_event(app_row["id"], "account_open_failed", {"product_id": pid, "message": str(e)})
            continue
        db.add_account(
            app_row["id"],
            {
                **account,
                "product_id": pid,
                "product_name": products.get_product(pid)["name"],
                "expected_deposit_cents": choice["initial_deposit_cents"],
            },
        )


def missing_accounts(app_row: dict) -> list[str]:
    have = {a["product_id"] for a in app_row.get("accounts") or []}
    return [c["product_id"] for c in chosen_products(app_row) if c["product_id"] not in have]


def apply_business(app_row: dict, business: dict, source: str = "refresh") -> dict:
    """Store Middesk's latest view of the business and re-run the rules."""
    # Applications saved before the owners step have no "owners" key: skip the owner rules for them.
    owners = (app_row.get("applicant") or {}).get("owners")
    kyc_all = app_row.get("kyc") or []
    if account_type(app_row) == "both":
        ev = decision.evaluate_combined(
            decision.evaluate(
                business,
                app_row["tin_submitted"],
                people=owners,
                kyc=[c for c in kyc_all if c.get("role") != "applicant"],
            ),
            decision.evaluate_personal([c for c in kyc_all if c.get("role") == "applicant"]),
        )
    else:
        ev = decision.evaluate(business, app_row["tin_submitted"], people=owners, kyc=kyc_all)
    tasks = ((business.get("review") or {}).get("tasks")) or []
    return db.update_with_results_event(
        app_row["id"],
        source,
        middesk_status=business.get("status"),
        recommendation=ev["recommendation"],
        priority=ev["priority"],
        reasons=ev["reasons"],
        tasks=tasks,
    )


def apply_personal(app_row: dict, source: str = "refresh") -> dict:
    """Personal accounts never go to Middesk. Re-run the rules on the SIMULATED identity checks."""
    ev = decision.evaluate_personal(app_row.get("kyc") or [])
    return db.update_with_results_event(
        app_row["id"],
        source,
        middesk_status="not_applicable",
        recommendation=ev["recommendation"],
        priority=ev["priority"],
        reasons=ev["reasons"],
        tasks=[],
    )


def is_personal(app_row: dict) -> bool:
    return account_type(app_row) == "personal"


# ---------- routes ----------
@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mode": "mock" if config.MOCK_MODE else "middesk",
        "middesk_base_url": None if config.MOCK_MODE else config.MIDDESK_BASE_URL,
        # Components that are always fake today, whatever the Middesk mode is.
        "simulated": ["kyc", "core_banking", "esign", "disclosures"] + (["middesk"] if config.MOCK_MODE else []),
        "reviewer_login": reviewer_login_on(),
        "in_browser": config.IN_BROWSER,
    }


@app.get("/api/products")
def get_products(account_type: Literal["business", "personal"] | None = None):
    """SAMPLE product catalog. Every entry is flagged sample=True."""
    return products.list_products(account_type)


@app.get("/api/staff")
def get_staff():
    """SAMPLE staff directory: names to assign work to, not logins."""
    return staff.list_staff()


@app.get("/api/disclosures")
def get_disclosures():
    return disclosures.current()


@app.post("/api/applications", status_code=201)
def create_application(a: ApplicationIn):
    app_id = str(uuid.uuid4())
    personal = a.account_type == "personal"
    both = a.account_type == "both"
    if personal:  # SIMULATED identity and watchlist checks on every person
        checks = kyc.check_individuals([p.full_name for p in a.individuals])
    else:
        checks = kyc.check_people([o.name for o in a.owners])  # SIMULATED, instant
        if both:  # the role tells owners and personal applicants apart when the rules run
            checks = [{**c, "role": "owner"} for c in checks] + [
                {**c, "role": "applicant"} for c in kyc.check_individuals([p.full_name for p in a.individuals])
            ]
    counts = {s: sum(c["status"] == s for c in checks) for s in (kyc.VERIFIED, kyc.NEEDS_REVIEW, kyc.FAILED)}
    db.insert(
        {
            "id": app_id,
            "business_name": a.business_name,
            "applicant": masked(a),
            "channel": a.channel,
            "created_by": a.created_by,
            "tin_submitted": bool(a.tin),
            "kyc": checks,
        },
        event={
            **(
                {"account_type": "personal", "individuals": len(a.individuals)}
                if personal
                else {"tin_submitted": bool(a.tin), "owners": len(a.owners)}
                | ({"account_type": "both", "individuals": len(a.individuals)} if both else {})
            ),
            **({"channel": "in_branch", "created_by": a.created_by} if a.channel == "in_branch" else {}),
        },
        kyc_event={**counts, "simulated": True},
        extra_events=[
            ("disclosures_accepted", {"version": a.disclosure_version, "signature_name": a.signature_name}),
            ("products_selected", {"products": [c.model_dump() for c in a.products]}),
        ],
    )
    if personal:  # Middesk verifies businesses, not people
        return apply_personal(db.get(app_id), source="create")
    try:
        business = get_client().create_business(to_middesk_payload(a, app_id))
    except MiddeskError as e:
        db.add_event(app_id, "middesk_error", {"message": str(e)})
        db.update(app_id, middesk_status="error", recommendation="needs_review", reasons=[str(e)])
        raise HTTPException(502, f"Could not reach Middesk: {e}") from e
    db.update(app_id, middesk_id=business["id"])
    row = db.get(app_id)
    if row["middesk_status"]:
        return row  # a webhook already stored newer results; don't overwrite them with this older snapshot
    return apply_business(row, business, source="create")


@app.get("/api/applications")
def list_applications():
    return db.list_all()


@app.get("/api/applications/{app_id}")
def get_application(app_id: str, refresh: bool = False):
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    if refresh and is_personal(row):
        row = apply_personal(row)
    elif refresh and row.get("middesk_id"):
        try:
            row = apply_business(row, get_client().get_business(row["middesk_id"]))
        except MiddeskError as e:
            raise HTTPException(502, str(e)) from e
    return row


@app.get("/api/applications/{app_id}/events")
def application_events(app_id: str):
    if not db.get(app_id):
        raise HTTPException(404, "Application not found")
    return db.list_events(app_id)


@app.get("/api/applications/{app_id}/export")
def export_application(app_id: str):
    """One application as a JSON download: details, history and document metadata. No file contents, no DOB or SSN."""
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    applicant = dict(row.get("applicant") or {})
    applicant["individuals"] = [
        {k: v for k, v in p.items() if k not in ("date_of_birth", "ssn_last4")}
        for p in applicant.get("individuals") or []
    ]
    docs = [{k: v for k, v in d.items() if k != "app_id"} for d in db.list_documents(app_id)]
    out = {
        "reference_id": ref_id(app_id),
        "application": {**row, "applicant": applicant},
        "events": db.list_events(app_id),
        "documents": docs,
    }
    text = redact_tins(json.dumps(out, indent=2))
    return Response(
        text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="openonboard-{ref_id(app_id)}.json"'},
    )


@app.post("/api/applications/{app_id}/documents", status_code=201)
async def upload_document(app_id: str, doc_type: Annotated[Literal[DOC_TYPES], Form()], file: UploadFile):
    if not db.get(app_id):
        raise HTTPException(404, "Application not found")
    if db.count_documents(app_id) >= MAX_DOCS_PER_APP:
        raise HTTPException(409, f"This application already has {MAX_DOCS_PER_APP} documents.")
    doc_id = str(uuid.uuid4())
    root = Path(config.DOCUMENTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    final, tmp = root / doc_id, root / f".{doc_id}.tmp"
    digest, size, ctype = hashlib.sha256(), 0, None
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(64 * 1024):
                if ctype is None:
                    ctype = sniff(chunk)
                    if ctype is None:
                        raise HTTPException(415, "Only PDF, PNG and JPEG files are accepted.")
                size += len(chunk)
                if size > MAX_DOC_BYTES:  # stop reading as soon as the limit is passed
                    raise HTTPException(413, f"The file is larger than {MAX_DOC_BYTES // (1024 * 1024)} MB.")
                digest.update(chunk)
                out.write(chunk)
        if ctype is None:
            raise HTTPException(415, "Only PDF, PNG and JPEG files are accepted.")
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    meta = {
        "id": doc_id,
        "app_id": app_id,
        "created_at": db.now(),
        "doc_type": doc_type,
        "filename": clean_filename(file.filename),
        "content_type": ctype,
        "size": size,
        "sha256": digest.hexdigest(),
    }
    try:
        saved = db.add_document(meta, MAX_DOCS_PER_APP)
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    if not saved:
        final.unlink(missing_ok=True)
        raise HTTPException(409, f"This application already has {MAX_DOCS_PER_APP} documents.")
    return {k: v for k, v in meta.items() if k != "app_id"}


@app.get("/api/applications/{app_id}/documents")
def list_documents(app_id: str):
    if not db.get(app_id):
        raise HTTPException(404, "Application not found")
    return db.list_documents(app_id)


@app.get("/api/applications/{app_id}/documents/{doc_id}")
def download_document(app_id: str, doc_id: str):
    try:
        doc_id = str(uuid.UUID(doc_id))  # the only user input that ever reaches a file path
    except ValueError:
        raise HTTPException(404, "Document not found") from None
    doc = db.get_document(app_id, doc_id)
    path = Path(config.DOCUMENTS_DIR) / doc_id
    if not doc or not path.is_file():
        raise HTTPException(404, "Document not found")
    return FileResponse(
        path,
        media_type=doc["content_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(doc["filename"], safe=""),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/audit.csv")
def audit_csv():
    names = {a["id"]: a["business_name"] for a in db.list_all()}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time", "application_id", "reference_id", "business_name", "event", "details"])
    for e in db.list_events():
        details = redact_tins(json.dumps(e["detail"], separators=(",", ":"))) if e["detail"] is not None else ""
        row = [e["at"], e["app_id"], ref_id(e["app_id"]), redact_tins(names.get(e["app_id"], "")), e["kind"], details]
        w.writerow([csv_safe(v) for v in row])
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="openonboard-audit.csv"'},
    )


@app.post("/api/applications/{app_id}/decision")
def decide(app_id: str, d: DecisionIn):
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    if row["final_decision"]:
        raise HTTPException(409, f"This application was already {row['final_decision']}.")
    if row["recommendation"] in (None, decision.PENDING):
        raise HTTPException(409, "Verification is not finished yet. Refresh first.")
    if not db.set_final_decision(app_id, d.decision, d.note):
        raise HTTPException(409, "Another reviewer just decided this application. Reload to see it.")
    if d.decision == "approved":
        try_open_account(db.get(app_id))
    return db.get(app_id)


def _staff_or_404(staff_id: str) -> dict:
    person = staff.get_staff(staff_id)
    if person is None:
        raise HTTPException(404, "That staff member was not found.")
    return person


@app.post("/api/applications/{app_id}/assignees")
def add_assignee(app_id: str, body: AssigneeIn):
    """Assign a staff member (a name, not a login). Up to 5 per application."""
    person = _staff_or_404(body.staff_id)
    result = db.add_assignee(app_id, person)
    if result == "missing":
        raise HTTPException(404, "Application not found")
    if result == "duplicate":
        raise HTTPException(409, f"{person['name']} is already assigned.")
    if result == "full":
        raise HTTPException(409, f"An application can have at most {db.MAX_ASSIGNEES} assigned staff.")
    return db.get(app_id)


@app.delete("/api/applications/{app_id}/assignees/{staff_id}")
def remove_assignee(app_id: str, staff_id: str):
    person = _staff_or_404(staff_id)
    result = db.remove_assignee(app_id, staff_id, person["name"])
    if result == "missing":
        raise HTTPException(404, "Application not found")
    if result == "not_assigned":
        raise HTTPException(404, f"{person['name']} is not assigned.")
    return db.get(app_id)


@app.put("/api/applications/{app_id}/primary-officer")
def set_primary_officer(app_id: str, body: OfficerIn):
    """Set the primary officer, or clear it with {"staff_id": null}."""
    person = _staff_or_404(body.staff_id) if body.staff_id is not None else None
    if not db.set_primary_officer(app_id, person):
        raise HTTPException(404, "Application not found")
    return db.get(app_id)


@app.post("/api/applications/{app_id}/open-account")
def open_account(app_id: str):
    """SIMULATED. Retry opening the account for an approved application that has none."""
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    if row["final_decision"] != "approved":
        raise HTTPException(409, "Only approved applications can have an account.")
    if not missing_accounts(row):
        raise HTTPException(409, "This application already has all its accounts.")
    try_open_account(row)
    return db.get(app_id)


@app.post("/api/applications/{app_id}/simulate-deposit")
def simulate_deposit(app_id: str, d: DepositIn):
    """SIMULATED. Pretend the first deposit arrived. No money moves."""
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    accounts = row["accounts"]
    pid = d.product_id if d.product_id is not None else (accounts[0]["product_id"] if accounts else None)
    if accounts and d.product_id is not None and not any(a["product_id"] == pid for a in accounts):
        raise HTTPException(404, "That account was not found.")
    if not db.set_funded(app_id, pid, d.amount_cents):
        raise HTTPException(409, "This account is not waiting for a first deposit.")
    return db.get(app_id)


@app.get("/api/maintenance/kinds")
def maintenance_kinds():
    """The kinds of account maintenance request, with labels and descriptions for the UI."""
    return maintenance.KINDS


@app.post("/api/applications/{app_id}/maintenance", status_code=201)
def create_maintenance(app_id: str, body: MaintenanceIn):
    """Ask for a change to an open account. A human approves it later; notes complete at once."""
    row = db.get(app_id)
    if not row:
        raise HTTPException(404, "Application not found")
    who = _staff_or_404(body.requested_by) if body.requested_by is not None else None
    if body.kind != "note" and not (row["final_decision"] == "approved" and row["accounts"]):
        raise HTTPException(409, "Maintenance requests are for customers with an open account.")
    try:
        details = maintenance.validate(body.kind, body.details, row)
    except maintenance.DetailsError as e:
        raise HTTPException(422, [{"loc": ["details", f], "msg": m} for f, m in e.errors]) from None
    meta = next(k for k in maintenance.KINDS if k["id"] == body.kind)
    req_id = str(uuid.uuid4())
    base = {"request_id": req_id, "kind": body.kind}
    text = maintenance.summary(body.kind, details)
    events = [
        (
            "maintenance_requested",
            {**base, "summary": text, "requested_by": who and who["id"], "name": who and who["name"]},
        )
    ]
    if not meta["needs_approval"]:
        events.append(("maintenance_completed", {**base, "summary": text}))
    db.add_maintenance(
        {
            "id": req_id,
            "app_id": app_id,
            "kind": body.kind,
            "status": "pending" if meta["needs_approval"] else "completed",
            "details": details,
            "requested_by": who and who["id"],
            "account_ref": details.get("account_ref"),
        },
        events,
    )
    return db.get_maintenance(req_id)


@app.get("/api/applications/{app_id}/maintenance")
def list_maintenance(app_id: str):
    if not db.get(app_id):
        raise HTTPException(404, "Application not found")
    return db.list_maintenance(app_id)


@app.post("/api/maintenance/{request_id}/decision")
def decide_maintenance(request_id: str, body: MaintenanceDecisionIn):
    """A person approves or rejects a pending request. Approving sends the change to the core (SIMULATED)."""
    req = db.get_maintenance(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    who = _staff_or_404(body.decided_by) if body.decided_by is not None else None
    try:
        result, out = db.decide_maintenance(
            request_id,
            body.decision == "approve",
            who,
            body.note,
            lambda row, at: maintenance.apply(req["kind"], req["details"], row, at),
            maintenance.summary(req["kind"], req["details"]),
        )
    except maintenance.ApplyError as e:
        raise HTTPException(409, str(e)) from None
    if result == "decided":
        raise HTTPException(409, f"This request was already {out['status']}.")
    return out


@app.post("/webhooks/middesk")
async def middesk_webhook(request: Request):
    if not config.MIDDESK_WEBHOOK_SECRET:
        raise HTTPException(503, "Set MIDDESK_WEBHOOK_SECRET to accept webhooks")
    raw = await request.body()  # verify the RAW bytes before parsing JSON
    sent = request.headers.get("X-Middesk-Signature-256", "")
    expected = hmac.new(config.MIDDESK_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(raw)
    except ValueError as e:
        raise HTTPException(400, "Body is not valid JSON") from e
    if not isinstance(event, dict):
        raise HTTPException(400, "Expected a JSON object")

    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type.startswith("business.") and isinstance(obj, dict) and obj.get("id"):
        row = db.get_by_middesk_id(obj["id"])
        if not row and obj.get("external_id"):
            # The webhook beat our own save of middesk_id. external_id is our application ID.
            row = db.claim_middesk_id(str(obj["external_id"]), obj["id"])
        if row:
            apply_business(row, obj, source="webhook")
    return {"received": True}
