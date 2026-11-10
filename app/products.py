"""SAMPLE product catalog.

Every entry here is invented sample data (sample=True). The names, fees, rates and minimums are not real
offers. The bank replaces this list with its own current terms before going live.
"""

from __future__ import annotations

MAX_DEPOSIT_CENTS = 100_000_000  # $1,000,000

CATALOG: list[dict] = [
    {
        "id": "personal-everyday-checking",
        "name": "Everyday Checking",
        "account_type": "personal",
        "kind": "checking",
        "min_opening_deposit_cents": 2500,
        "monthly_fee_cents": 500,
        "fee_waiver": "No fee with direct deposit or a $500 daily balance.",
        "apy": "None",
        "notes": "A simple account for paying bills and everyday spending.",
    },
    {
        "id": "personal-interest-checking",
        "name": "Interest Checking",
        "account_type": "personal",
        "kind": "checking",
        "min_opening_deposit_cents": 10000,
        "monthly_fee_cents": 1000,
        "fee_waiver": "No fee with a $2,500 daily balance.",
        "apy": "0.05%",
        "notes": "Earns a small amount of interest on your balance.",
    },
    {
        "id": "personal-savings",
        "name": "Personal Savings",
        "account_type": "personal",
        "kind": "savings",
        "min_opening_deposit_cents": 5000,
        "monthly_fee_cents": 300,
        "fee_waiver": "No fee with a $300 daily balance.",
        "apy": "1.50%",
        "notes": "Set money aside and earn interest.",
    },
    {
        "id": "personal-certificate",
        "name": "Certificate of Deposit",
        "account_type": "personal",
        "kind": "certificate",
        "min_opening_deposit_cents": 100000,
        "monthly_fee_cents": 0,
        "fee_waiver": "No monthly fee.",
        "apy": "4.00%",
        "notes": "Your money stays put for the full term. Taking it out early costs a penalty.",
        "terms_months": [6, 12, 24],
    },
    {
        "id": "business-checking",
        "name": "Business Checking",
        "account_type": "business",
        "kind": "checking",
        "min_opening_deposit_cents": 10000,
        "monthly_fee_cents": 1500,
        "fee_waiver": "No fee with a $2,000 daily balance.",
        "apy": "None",
        "notes": "Day-to-day banking for a small business.",
    },
    {
        "id": "business-interest-checking",
        "name": "Business Interest Checking",
        "account_type": "business",
        "kind": "checking",
        "min_opening_deposit_cents": 50000,
        "monthly_fee_cents": 2500,
        "fee_waiver": "No fee with a $10,000 daily balance.",
        "apy": "0.10%",
        "notes": "Checking that earns interest on larger balances.",
    },
    {
        "id": "business-savings",
        "name": "Business Savings",
        "account_type": "business",
        "kind": "savings",
        "min_opening_deposit_cents": 25000,
        "monthly_fee_cents": 1000,
        "fee_waiver": "No fee with a $1,000 daily balance.",
        "apy": "1.25%",
        "notes": "A place for your business reserves.",
    },
    {
        "id": "business-certificate",
        "name": "Business Certificate",
        "account_type": "business",
        "kind": "certificate",
        "min_opening_deposit_cents": 250000,
        "monthly_fee_cents": 0,
        "fee_waiver": "No monthly fee.",
        "apy": "3.90%",
        "notes": "Your money stays put for the full term. Taking it out early costs a penalty.",
        "terms_months": [6, 12, 24],
    },
]
for _p in CATALOG:
    _p["sample"] = True

_BY_ID = {p["id"]: p for p in CATALOG}


def list_products(account_type: str | None = None) -> list[dict]:
    return [dict(p) for p in CATALOG if account_type in (None, p["account_type"])]


def get_product(product_id: str) -> dict | None:
    return _BY_ID.get(product_id)
