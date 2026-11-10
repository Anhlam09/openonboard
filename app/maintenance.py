"""Account maintenance requests for customers whose accounts are open.

Each kind has its own details model. A request is validated against the application (account type, account,
product), then a human approves or rejects it. Approval "sends the change to the core", which is SIMULATED:
the change is only written to this app's own data. Notes need no approval.

Tax ID changes keep the last 4 digits only: a new SSN is accepted as 4 digits, a new EIN is typed in full
but reduced to its last 4 before anything is stored, logged or exported.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import products
from .middesk import redact_tins

KINDS: list[dict] = [
    {
        "id": "contact_change",
        "label": "Update contact details",
        "description": "Change a name, email, phone number or address.",
        "applies_to": "both",
        "needs_account": False,
        "needs_target": True,
        "needs_approval": True,
    },
    {
        "id": "product_change",
        "label": "Switch account type",
        "description": "Move an account over to a different sample product.",
        "applies_to": "both",
        "needs_account": True,
        "needs_approval": True,
    },
    {
        "id": "beneficiary_change",
        "label": "Update beneficiaries",
        "description": "Choose who receives the balance and in what shares.",
        "applies_to": "personal",
        "needs_account": True,
        "needs_approval": True,
    },
    {
        "id": "signer_change",
        "label": "Add or remove a signer",
        "description": "Change who can sign for a business account.",
        "applies_to": "business",
        "needs_account": True,
        "needs_approval": True,
    },
    {
        "id": "debit_card_order",
        "label": "Order a debit card",
        "description": "Ask for a new, replacement or lost-card debit card.",
        "applies_to": "both",
        "needs_account": True,
        "needs_approval": True,
    },
    {
        "id": "check_order",
        "label": "Order checks",
        "description": "Ask for a box of printed checks.",
        "applies_to": "both",
        "needs_account": True,
        "needs_approval": True,
    },
    {
        "id": "safe_deposit_box",
        "label": "Rent a safe deposit box",
        "description": "Reserve a box of a chosen size at a branch.",
        "applies_to": "both",
        "needs_account": False,
        "needs_approval": True,
    },
    {
        "id": "tax_id_change",
        "label": "Correct a tax ID",
        "description": "Replace the tax ID on file. Only the last 4 digits are kept.",
        "applies_to": "both",
        "needs_account": False,
        "needs_target": True,
        "needs_approval": True,
    },
    {
        "id": "note",
        "label": "Add a note",
        "description": "Record a conversation or reminder. No approval needed.",
        "applies_to": "both",
        "needs_account": False,
        "needs_approval": False,
    },
]
KIND_IDS = tuple(k["id"] for k in KINDS)


class DetailsError(Exception):
    """Bad request details. `errors` is a list of (field, message)."""

    def __init__(self, errors: list[tuple[str, str]]):
        super().__init__("; ".join(m for _, m in errors))
        self.errors = errors


class ApplyError(Exception):
    """The change can no longer be applied to the application as it is now."""


class _M(BaseModel):
    model_config = {"extra": "forbid"}


def _blank_to_none(v):
    return None if isinstance(v, str) and not v.strip() else v


class ContactChange(_M):
    target: Literal["business"] | None = None  # combined customers: "business", or leave out and give person_index
    person_index: int | None = Field(default=None, ge=0, le=3)
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = None
    postal_code: str | None = None

    _blank = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("name", "address_line1", "address_line2", "city")
    @classmethod
    def trimmed(cls, v, info):
        if v is None:
            return v
        v = redact_tins(v.strip())
        floor = {"name": 2, "address_line1": 3, "city": 2}.get(info.field_name, 0)
        if len(v) < floor:
            raise ValueError(f"Enter at least {floor} characters.")
        return v

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        if v is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", v.strip()):
            raise ValueError("Enter an email address like name@example.com")
        return v and v.strip()

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v):
        if v is not None and not re.fullmatch(r"[\d\s()+\-]{7,20}", v.strip()):
            raise ValueError("Enter a phone number with 7 to 20 digits, spaces or ( ) + -")
        return v and v.strip()

    @field_validator("state")
    @classmethod
    def state_ok(cls, v):
        if v is None:
            return v
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("Use the 2-letter state code, like NY")
        return v

    @field_validator("postal_code")
    @classmethod
    def zip_ok(cls, v):
        if v is not None and not re.fullmatch(r"\d{5}(-\d{4})?", v.strip()):
            raise ValueError("ZIP code must look like 10013 or 10013-1234")
        return v and v.strip()

    @model_validator(mode="after")
    def something_changed(self):
        if not any(v is not None for k, v in self.model_dump().items() if k not in ("person_index", "target")):
            raise ValueError("Enter at least one detail to change.")
        return self


class ProductChange(_M):
    account_ref: str = Field(max_length=80)
    new_product_id: str = Field(max_length=80)
    term_months: int | None = None


class Beneficiary(_M):
    name: str = Field(min_length=2, max_length=120)
    relationship: str = Field(min_length=2, max_length=60)
    share_pct: float = Field(gt=0, le=100)

    @field_validator("name", "relationship")
    @classmethod
    def mask_tins(cls, v):
        return redact_tins(v.strip())


class BeneficiaryChange(_M):
    account_ref: str = Field(max_length=80)
    beneficiaries: list[Beneficiary] = Field(min_length=1, max_length=5)

    @field_validator("beneficiaries")
    @classmethod
    def shares_total_100(cls, v):
        if round(sum(b.share_pct for b in v), 2) != 100:
            raise ValueError("The shares must add up to 100%.")
        return v


class SignerChange(_M):
    account_ref: str = Field(max_length=80)
    action: Literal["add", "remove"]
    name: str = Field(min_length=2, max_length=120)
    title: str | None = Field(default=None, max_length=80)

    @field_validator("name", "title")
    @classmethod
    def mask_tins(cls, v):
        return redact_tins(v.strip()) if v else v


class DebitCardOrder(_M):
    account_ref: str = Field(max_length=80)
    reason: Literal["new", "replacement", "lost_stolen"]


class CheckOrder(_M):
    account_ref: str = Field(max_length=80)
    quantity: Literal[50, 100, 200]
    style: Literal["standard"] = "standard"


class SafeDepositBox(_M):
    box_size: Literal["small", "medium", "large"]
    branch: str = Field(min_length=2, max_length=100)

    @field_validator("branch")
    @classmethod
    def branch_text(cls, v):
        v = redact_tins(v.strip())
        if len(v) < 2:
            raise ValueError("Enter the branch.")
        return v


class TaxIdChange(_M):
    target: Literal["business"] | None = None
    person_index: int | None = Field(default=None, ge=0, le=3)
    ssn_last4: str | None = None
    ein: str | None = None

    @field_validator("ssn_last4")
    @classmethod
    def last4_only(cls, v):
        if v is None:
            return v
        v = v.strip()
        if re.fullmatch(r"\d{4}", v):
            return v
        if len(re.sub(r"\D", "", v)) > 4:
            raise ValueError("Only the last 4 digits of the SSN are needed. Enter just those 4 digits.")
        raise ValueError("Enter exactly 4 digits.")

    @field_validator("ein")
    @classmethod
    def ein_digits(cls, v):
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("EIN must have 9 digits, like 12-3456789")
        return digits


class Note(_M):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def clean(cls, v):
        v = redact_tins(v.strip())
        if not v:
            raise ValueError("Write a note.")
        return v


MODELS = {
    "contact_change": ContactChange,
    "product_change": ProductChange,
    "beneficiary_change": BeneficiaryChange,
    "signer_change": SignerChange,
    "debit_card_order": DebitCardOrder,
    "check_order": CheckOrder,
    "safe_deposit_box": SafeDepositBox,
    "tax_id_change": TaxIdChange,
    "note": Note,
}


def account_type(app_row: dict) -> str:
    t = (app_row.get("applicant") or {}).get("account_type")
    return t if t in ("personal", "both") else "business"


def is_personal(app_row: dict) -> bool:
    return account_type(app_row) == "personal"


def is_both(app_row: dict) -> bool:
    return account_type(app_row) == "both"


def find_account(app_row: dict, ref: str) -> dict:
    acct = next((a for a in app_row.get("accounts") or [] if a["account_ref"] == ref), None)
    if acct is None:
        raise DetailsError([("account_ref", "That account was not found on this application.")])
    return acct


def _kind_of(acct: dict) -> str | None:
    p = products.get_product(acct.get("product_id") or "")
    return p["kind"] if p else None


def current_signers(app_row: dict, acct: dict) -> list[str]:
    if acct.get("signers") is not None:
        return list(acct["signers"])
    return [o["name"] for o in (app_row.get("applicant") or {}).get("owners") or []]


def _acct_account_type(acct: dict) -> str | None:
    p = products.get_product(acct.get("product_id") or "")
    return p["account_type"] if p else None


def _check_target(app_row: dict, target: str | None, idx: int | None) -> int | None:
    """Combined customers must say who a change is for: the business (target "business") or one applicant
    (person_index). Returns the person index, or None for the business."""
    if target == "business":
        if idx is not None:
            raise DetailsError([("person_index", "Choose the business or an applicant, not both.")])
        return None
    if idx is None:
        raise DetailsError([("target", "Choose who this is for: the business or an applicant.")])
    n = len((app_row.get("applicant") or {}).get("individuals") or [])
    if idx >= n:
        raise DetailsError([("person_index", "That applicant was not found.")])
    return idx


def _check_person_index(app_row: dict, idx: int | None) -> int | None:
    if is_personal(app_row):
        n = len((app_row.get("applicant") or {}).get("individuals") or [])
        i = idx or 0
        if i >= n:
            raise DetailsError([("person_index", "That applicant was not found.")])
        return i
    if idx is not None:
        raise DetailsError([("person_index", "Only personal accounts have more than one person.")])
    return None


def validate(kind: str, raw: dict, app_row: dict) -> dict:
    """Check `raw` for this kind and application. Returns the details to store: sanitized (a full EIN or any
    SSN digits beyond the last 4 are never in the result). Raises DetailsError."""
    try:
        m = MODELS[kind].model_validate(raw)
    except ValidationError as e:
        raise DetailsError(
            [
                (str(err["loc"][-1]) if err.get("loc") else "details", str(err["msg"]).removeprefix("Value error, "))
                for err in e.errors()
            ]
        ) from None
    d = m.model_dump(exclude_none=True)
    personal = is_personal(app_row)
    both = is_both(app_row)
    applies = next(k["applies_to"] for k in KINDS if k["id"] == kind)
    if both:  # decided per account (or per target) below
        pass
    elif "target" in d:
        raise DetailsError([("target", "Only customers with business and personal accounts choose a target.")])
    elif applies == "personal" and not personal:
        raise DetailsError([("kind", "This request is only for personal accounts.")])
    if applies == "business" and personal:
        raise DetailsError([("kind", "This request is only for business accounts.")])

    if kind == "contact_change":
        i = _check_target(app_row, m.target, m.person_index) if both else _check_person_index(app_row, m.person_index)
        if i is not None:
            d["person_index"] = i
    elif kind == "product_change":
        acct = find_account(app_row, m.account_ref)
        new = products.get_product(m.new_product_id)
        if new is None:
            raise DetailsError([("new_product_id", "That account is not available.")])
        wanted_type = _acct_account_type(acct) if both else ("personal" if personal else "business")
        if new["account_type"] != wanted_type:
            raise DetailsError([("new_product_id", "Choose an account that is for the same kind of customer.")])
        wanted = [c.get("product_id") for c in (app_row.get("applicant") or {}).get("products") or []]
        if new["id"] in wanted or any(a.get("product_id") == new["id"] for a in app_row["accounts"]):
            raise DetailsError([("new_product_id", "This customer already has that account.")])
        if new["kind"] == "certificate":
            if m.term_months not in new["terms_months"]:
                raise DetailsError([("term_months", f"Choose a term for {new['name']}.")])
        elif m.term_months is not None:
            raise DetailsError([("term_months", f"{new['name']} does not have a term.")])
        d["from_product_id"] = acct.get("product_id")
    elif kind == "beneficiary_change":
        acct = find_account(app_row, m.account_ref)
        if both and _acct_account_type(acct) != "personal":
            raise DetailsError([("account_ref", "Beneficiaries are only for personal accounts.")])
        if _kind_of(acct) not in ("savings", "certificate"):
            raise DetailsError([("account_ref", "Beneficiaries are only for savings and certificate accounts.")])
    elif kind == "signer_change":
        acct = find_account(app_row, m.account_ref)
        if both and _acct_account_type(acct) != "business":
            raise DetailsError([("account_ref", "Signers are only for business accounts.")])
        names = {s.lower() for s in current_signers(app_row, acct)}
        if m.action == "add" and m.name.strip().lower() in names:
            raise DetailsError([("name", "That person is already a signer.")])
        if m.action == "remove" and m.name.strip().lower() not in names:
            raise DetailsError([("name", "That person is not a signer on this account.")])
        d["name"] = m.name.strip()
    elif kind in ("debit_card_order", "check_order"):
        acct = find_account(app_row, m.account_ref)
        if _kind_of(acct) != "checking":
            raise DetailsError([("account_ref", "This is only for checking accounts.")])
    elif kind == "tax_id_change":
        if both:
            i = _check_target(app_row, m.target, m.person_index)
            if i is None:
                if m.ssn_last4 is not None:
                    raise DetailsError([("ssn_last4", "The business changes the EIN.")])
                if m.ein is None:
                    raise DetailsError([("ein", "Enter the new EIN.")])
                d = {"ein_last4": m.ein[-4:]}  # the full EIN is dropped here, before anything is stored
            else:
                if m.ein is not None:
                    raise DetailsError([("ein", "An applicant changes the last 4 of the SSN only.")])
                if m.ssn_last4 is None:
                    raise DetailsError([("ssn_last4", "Enter the last 4 digits of the SSN.")])
                d = {"person_index": i, "ssn_last4": m.ssn_last4}
        elif personal:
            if m.ein is not None:
                raise DetailsError([("ein", "Personal accounts change the last 4 of the SSN only.")])
            if m.ssn_last4 is None:
                raise DetailsError([("ssn_last4", "Enter the last 4 digits of the SSN.")])
            d = {"person_index": _check_person_index(app_row, m.person_index), "ssn_last4": m.ssn_last4}
        else:
            if m.ssn_last4 is not None or m.person_index is not None:
                raise DetailsError([("ssn_last4", "Business accounts change the EIN.")])
            if m.ein is None:
                raise DetailsError([("ein", "Enter the new EIN.")])
            d = {"ein_last4": m.ein[-4:]}  # the full EIN is dropped here, before anything is stored
    return d


def summary(kind: str, details: dict) -> str:
    """A short description with no sensitive digits (no tax ID digits at all)."""
    if kind == "contact_change":
        return "Changed " + ", ".join(k.replace("_", " ") for k in details if k not in ("person_index", "target"))
    if kind == "product_change":
        old = (products.get_product(details.get("from_product_id") or "") or {}).get("name", "an account")
        return f"{old} to {products.get_product(details['new_product_id'])['name']}"
    if kind == "beneficiary_change":
        n = len(details["beneficiaries"])
        return f"{n} {'beneficiary' if n == 1 else 'beneficiaries'}"
    if kind == "signer_change":
        return f"{details['action'].capitalize()} signer {redact_tins(details['name'])}"
    if kind == "debit_card_order":
        return "Debit card: " + details["reason"].replace("_", " ")
    if kind == "check_order":
        return f"{details['quantity']} {details['style']} checks"
    if kind == "safe_deposit_box":
        return f"{details['box_size'].capitalize()} box at {details['branch']}"
    if kind == "tax_id_change":
        return "Tax ID changed"
    return "Note added"


def apply(kind: str, details: dict, app_row: dict, at: str) -> dict:
    """SIMULATED core update. Returns the new stored values: any of applicant, accounts, business_name,
    tin_submitted. Raises ApplyError if the application no longer fits the request."""
    applicant = {**(app_row.get("applicant") or {})}
    accounts = [dict(a) for a in app_row.get("accounts") or []]
    out: dict = {}

    def account():
        a = next((x for x in accounts if x["account_ref"] == details.get("account_ref")), None)
        if a is None:
            raise ApplyError("The account is no longer on this application.")
        return a

    if kind == "contact_change":
        fields = {k: v for k, v in details.items() if k not in ("person_index", "target")}
        if is_personal(app_row) or "person_index" in details:
            people = [dict(p) for p in applicant.get("individuals") or []]
            i = details.get("person_index", 0)
            if i >= len(people):
                raise ApplyError("That applicant is no longer on this application.")
            if "name" in fields:
                people[i]["full_name"] = fields.pop("name")
            people[i].update(fields)
            applicant["individuals"] = people
            if i == 0 and is_personal(app_row):
                out["business_name"] = people[0]["full_name"]
                applicant["business_name"] = people[0]["full_name"]
        else:
            if "name" in fields:
                out["business_name"] = applicant["business_name"] = fields.pop("name")
            if "email" in fields:
                applicant["contact_email"] = fields.pop("email")
            if "phone" in fields:
                applicant["contact_phone"] = fields.pop("phone")
            applicant.update(fields)
        out["applicant"] = applicant
    elif kind == "product_change":
        acct = account()
        new = products.get_product(details["new_product_id"])
        wanted = [c.get("product_id") for c in applicant.get("products") or []]
        if new["id"] in wanted or any(a is not acct and a.get("product_id") == new["id"] for a in accounts):
            raise ApplyError("This customer already has that account.")
        old_id = acct.get("product_id")
        acct["product_id"], acct["product_name"] = new["id"], new["name"]
        choices = [dict(c) for c in applicant.get("products") or []]
        for c in choices:
            if c["product_id"] == old_id:
                c["product_id"] = new["id"]
                c["term_months"] = details.get("term_months")
                if new["kind"] != "checking":
                    c["overdraft_protection"] = c["debit_card"] = False
        applicant["products"] = choices
        out["applicant"], out["accounts"] = applicant, accounts
    elif kind == "beneficiary_change":
        account()["beneficiaries"] = details["beneficiaries"]
        out["accounts"] = accounts
    elif kind == "signer_change":
        acct = account()
        signers = current_signers(app_row, acct)
        name = details["name"]
        if details["action"] == "add":
            signers.append(name)
        else:
            signers = [s for s in signers if s.lower() != name.lower()]
        acct["signers"] = signers
        out["accounts"] = accounts
    elif kind in ("debit_card_order", "check_order"):
        acct = account()
        order = {k: v for k, v in details.items() if k != "account_ref"}
        acct.setdefault("orders", []).append({"kind": kind, **order, "ordered_at": at})
        out["accounts"] = accounts
    elif kind == "safe_deposit_box":
        applicant["safe_deposit_boxes"] = [*(applicant.get("safe_deposit_boxes") or []), {**details, "at": at}]
        out["applicant"] = applicant
    elif kind == "tax_id_change":
        if "ssn_last4" in details:
            people = [dict(p) for p in applicant.get("individuals") or []]
            if details["person_index"] >= len(people):
                raise ApplyError("That applicant is no longer on this application.")
            people[details["person_index"]]["ssn_last4"] = details["ssn_last4"]
            applicant["individuals"] = people
        else:
            applicant["tin"] = f"*****{details['ein_last4']}"
            out["tin_submitted"] = 1
        out["applicant"] = applicant
    return out
