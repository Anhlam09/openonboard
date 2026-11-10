"""Opening the account in the bank's core system.

SIMULATED. MockCore never talks to a core banking system. It makes up an account
reference and last 4 digits from a hash of the application id and product id, so the same
application and product always get the same result. A real bank core integration replaces this module.
Never use the mock for real customers.

Only the last 4 digits of an account number are ever kept. This only runs after a
person approves; it never decides anything.
"""

from __future__ import annotations

import hashlib


class CoreError(Exception):
    """The core system could not open the account."""


class MockCore:
    """Simulated core. A business name containing "core down" fails, for testing."""

    def open_account(self, app_row: dict, product_id: str) -> dict:
        if "core down" in app_row["business_name"].lower():
            raise CoreError("The core banking system did not respond (simulated)")
        digest = hashlib.sha256(f"{app_row['id']}|{product_id}".encode()).hexdigest()
        return {
            "account_ref": "SIM-" + digest[:8].upper(),
            "account_last4": f"{int(digest[8:16], 16) % 10000:04d}",
            "simulated": True,
        }


def get_core() -> MockCore:
    return MockCore()


def open_account(app_row: dict, product_id: str) -> dict:
    return get_core().open_account(app_row, product_id)
