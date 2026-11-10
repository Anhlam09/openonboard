"""Fields every valid application body needs: disclosures and a default product."""

import re

from app.disclosures import DISCLOSURE_VERSION

BUSINESS_PRODUCTS = [{"product_id": "business-checking", "initial_deposit_cents": 10000}]
PERSONAL_PRODUCTS = [{"product_id": "personal-everyday-checking", "initial_deposit_cents": 2500}]

SIGNED = {
    "disclosures_accepted": True,
    "disclosure_version": DISCLOSURE_VERSION,
    "signature_name": "Jane Doe",
    "products": BUSINESS_PRODUCTS,  # business default; personal bodies override it
}

_RANDOM_PARTS = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # application, request, document ids
    r"|KYB-[0-9A-F]{6}|SIM-[0-9A-F]{8}"  # reference numbers and simulated account refs
    r"|[0-9a-f]{32,}"  # file fingerprints (sha256)
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\"',]*"  # timestamps
    r"|account_last4[\"']?\s*[:=]\s*[\"']?\d{4}"  # simulated account numbers (last 4)
)


def without_random_parts(text: str) -> str:
    """Remove random ids, references, fingerprints, timestamps and simulated account digits, so a check for
    leaked SSN or tax ID digits can't fail just because a random value happens to contain the same digits."""
    return _RANDOM_PARTS.sub("", text)
