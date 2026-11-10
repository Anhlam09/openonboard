"""Small Middesk client.

Real mode: talks to the Middesk REST API (sandbox or live).
Mock mode: no API key needed. Returns realistic fake results so anyone can
run the project. Business-name triggers (similar in spirit to Middesk's sandbox):
  "Unregistered ..."  -> no Secretary of State registration
  "... Watchlist ..." -> sanctions/watchlist hit (the real sandbox needs "watchlist hit" in the name)
  "... Badtin ..."    -> IRS TIN mismatch
  address "423 ..."   -> approximate address match
Anything else       -> clean pass.
Mock results survive a server restart: the mock rebuilds a business from the
application database (name, address and whether a TIN was sent; never the TIN).
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import httpx

from . import config

# A 9-digit TIN in EIN (12-3456789), SSN (123-45-6789) or plain form, not part of a longer number.
_TIN_RE = re.compile(r"(?<!\d)(?:\d{2}[-\s]?\d{3}|\d{3}(?:[-\s]\d{2}[-\s]|\d{2}))(?P<last4>\d{4})(?!\d)")


def redact_tins(text: str) -> str:
    """Mask anything that looks like a full TIN/EIN/SSN to its last 4 digits."""
    return _TIN_RE.sub(lambda m: f"*****{m.group('last4')}", text)


class MiddeskError(Exception):
    """Errors from Middesk. The message is redacted, because Middesk may echo
    the submitted TIN back and we store and show these messages."""

    def __init__(self, message: str):
        super().__init__(redact_tins(message))


class MiddeskClient:
    def __init__(self, api_key: str, base_url: str, auth_style: str = "bearer"):
        headers = {"Accept": "application/json"}
        auth = None
        if auth_style == "basic":
            auth = (api_key, "")  # older docs: API key as username, empty password
        else:
            headers["Authorization"] = f"Bearer {api_key}"  # current API reference
        self._http = httpx.Client(base_url=base_url, auth=auth, timeout=20.0, headers=headers)

    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        # Only GET is safe to retry after a timeout or a 5xx: a retried POST could
        # create (and bill) a second business. A 429 means "not processed", so any
        # method can retry it.
        safe_to_retry = method.upper() == "GET"
        attempts = 3
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = self._http.request(method, path, **kw)
            except httpx.HTTPError as e:
                if safe_to_retry and not last:
                    time.sleep(2**attempt)
                    continue
                raise MiddeskError(f"Could not connect to Middesk ({type(e).__name__})") from e
            if r.status_code == 429 or (r.status_code >= 500 and safe_to_retry):
                if not last:
                    time.sleep(2**attempt)  # simple backoff for rate limits / outages
                    continue
                raise MiddeskError(f"Middesk unavailable after retries ({r.status_code})")
            if r.status_code >= 400:
                raise MiddeskError(f"Middesk {r.status_code}: {r.text[:500]}")
            try:
                return r.json()
            except ValueError as e:
                raise MiddeskError(f"Middesk sent a response that is not JSON ({r.status_code})") from e
        raise AssertionError("unreachable")

    def create_business(self, payload: dict) -> dict:
        return self._request("POST", "/businesses", json=payload)

    def get_business(self, business_id: str) -> dict:
        return self._request("GET", f"/businesses/{business_id}")


def _task(category, key, label, status, message, sub_label):
    return {
        "category": category,
        "key": key,
        "label": label,
        "status": status,
        "message": message,
        "sub_label": sub_label,
        "sources": [],
    }


class MockMiddeskClient:
    """Pretends to be Middesk. Creation returns 'pending'; the first fetch completes it.
    Businesses are kept in memory and rebuilt from the application database after a restart."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    def create_business(self, payload: dict) -> dict:
        bid = str(uuid.uuid4())
        biz = {
            "object": "business",
            "id": bid,
            "name": payload["name"],
            "status": "pending",
            "review": None,
            "external_id": payload.get("external_id"),
            "_payload": payload,
        }
        self._store[bid] = biz
        return {k: v for k, v in biz.items() if not k.startswith("_")}

    def _rebuild(self, business_id: str) -> dict:
        """Recreate a business lost on restart from the stored application."""
        from . import db  # lazy import: avoids an import cycle

        row = db.get_by_middesk_id(business_id)
        if not row:
            raise MiddeskError("Mock: business not found (no application in the database uses this ID)")
        applicant = row.get("applicant") or {}
        payload: dict[str, Any] = {
            "name": row["business_name"],
            "addresses": [{"address_line1": applicant.get("address_line1", "")}],
        }
        if row["tin_submitted"]:
            payload["tin"] = {"tin": "on-file"}  # placeholder only: the real TIN is never stored
        biz = {
            "object": "business",
            "id": business_id,
            "name": row["business_name"],
            "status": "pending",
            "review": None,
            "external_id": row["id"],
            "_payload": payload,
        }
        self._store[business_id] = biz
        return biz

    def get_business(self, business_id: str) -> dict:
        biz = self._store.get(business_id) or self._rebuild(business_id)
        p = biz["_payload"]
        name = p["name"].lower()
        addr = (p.get("addresses") or [{}])[0].get("address_line1", "")
        tasks = [
            _task(
                "name",
                "name",
                "Business Name",
                "success",
                "Match identified to the submitted Business Name",
                "Verified",
            ),
            _task(
                "address",
                "address_verification",
                "Office Address",
                "success",
                "Match identified to the submitted Office Address",
                "Verified",
            ),
            _task(
                "sos",
                "sos_active",
                "SOS Active",
                "success",
                "The business is active in its state of formation",
                "Active",
            ),
            _task("watchlist", "watchlist", "Watchlist", "success", "No Watchlist hits were identified", "No Hits"),
        ]
        if "unregistered" in name:
            tasks[0] = _task(
                "name",
                "name",
                "Business Name",
                "warning",
                "Could not find a registration matching this name",
                "Unverified",
            )
            tasks[2] = _task(
                "sos",
                "sos_not_found",
                "SOS Filings",
                "failure",
                "The business has no Secretary of State filings",
                "Not Registered",
            )
        if "watchlist" in name:
            tasks[3] = _task(
                "watchlist",
                "watchlist",
                "Watchlist",
                "failure",
                "Potential match on a sanctions list (OFAC SDN)",
                "Hits",
            )
        if addr.strip().startswith("423"):
            tasks[1] = _task(
                "address",
                "address_verification",
                "Office Address",
                "warning",
                "Approximate match to the submitted Office Address",
                "Similar Match",
            )
        if p.get("tin"):
            if "badtin" in name:
                tasks.append(
                    _task(
                        "tin",
                        "tin",
                        "TIN Match",
                        "failure",
                        "The IRS does not have a record for the submitted TIN and Business Name",
                        "Not Found",
                    )
                )
            else:
                tasks.append(
                    _task(
                        "tin",
                        "tin",
                        "TIN Match",
                        "success",
                        "The IRS has a record for the submitted TIN and Business Name",
                        "Found",
                    )
                )
        biz["status"] = "in_review"
        biz["review"] = {"tasks": tasks}
        return {k: v for k, v in biz.items() if not k.startswith("_")}


_client = None


def get_client():
    global _client
    if _client is None:
        _client = (
            MockMiddeskClient()
            if config.MOCK_MODE
            else MiddeskClient(config.MIDDESK_API_KEY, config.MIDDESK_BASE_URL, config.MIDDESK_AUTH)
        )
    return _client
