"""Identity checks (KYC) on each person listed on an application.

SIMULATED. MockKYC never looks anyone up. It decides from the name alone so demos and
tests are predictable. A real provider would need each person's date of birth and SSN,
which this app does not collect (personal accounts keep only the last 4 of the SSN). Collecting them would also mean
protecting them (encryption, masking, access control). Never use the mock for real customers.

Results only feed the RECOMMENDATION. A person still makes every final decision.
"""

from __future__ import annotations

from typing import Protocol

VERIFIED, NEEDS_REVIEW, FAILED = "verified", "needs_review", "failed"


class KYCProvider(Protocol):
    def check_person(self, name: str) -> dict: ...


class MockKYC:
    """Simulated provider. Name rules (case-insensitive): "fraud" fails; "unverified" or "J Doe" needs review."""

    def check_person(self, name: str) -> dict:
        n = " ".join(name.lower().split())
        if "fraud" in n:
            status, message = FAILED, "Identity check failed: details do not match a real person"
        elif "unverified" in n or n == "j doe":
            status, message = NEEDS_REVIEW, "Identity could not be verified from public records"
        else:
            status, message = VERIFIED, "Identity matched public records (simulated)"
        return {"name": name, "status": status, "message": message, "simulated": True}


def screen_watchlist(name: str) -> bool:
    """SIMULATED watchlist screen for a person: a name containing "watchlist" is a possible match."""
    return "watchlist" in name.lower()


def get_provider() -> KYCProvider:
    return MockKYC()


def check_people(names: list[str]) -> list[dict]:
    provider = get_provider()
    return [provider.check_person(n) for n in names]


def check_individuals(names: list[str]) -> list[dict]:
    """Identity check plus a SIMULATED watchlist screen for each person on a personal account."""
    out = []
    for check in check_people(names):
        if screen_watchlist(check["name"]):
            check = {**check, "status": FAILED, "watchlist": True, "message": "Possible watchlist match (simulated)"}
        out.append(check)
    return out
