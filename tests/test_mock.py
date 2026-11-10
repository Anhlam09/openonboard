"""Mock Middesk results survive a server restart."""

import pytest

from app import middesk
from app.middesk import MiddeskError, MockMiddeskClient
from tests.signing import SIGNED

GOOD = {
    "business_name": "Corporation",
    "tin": "12-3410000",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "ny",
    "postal_code": "10013",
    **SIGNED,
    "owners": [{"name": "Jane Doe", "title": "CEO", "ownership_pct": 100, "control_person": True}],
}


def _refresh_after_restart(client, monkeypatch, body):
    app = client.post("/api/applications", json=body).json()
    monkeypatch.setattr(middesk, "_client", None)  # simulate a restart
    r = client.get(f"/api/applications/{app['id']}?refresh=true")
    assert r.status_code == 200
    return r.json()


def test_clean_sample_after_restart(client, monkeypatch):
    assert _refresh_after_restart(client, monkeypatch, GOOD)["recommendation"] == "recommend_approve"


def test_watchlist_sample_after_restart(client, monkeypatch):
    app = _refresh_after_restart(client, monkeypatch, {**GOOD, "business_name": "Watchlist Trading LLC"})
    assert app["recommendation"] == "needs_review" and app["priority"] == "high"


def test_no_tin_sample_after_restart(client, monkeypatch):
    body = {k: v for k, v in GOOD.items() if k != "tin"}
    assert _refresh_after_restart(client, monkeypatch, body)["recommendation"] == "needs_review"


def test_unknown_id_still_raises(client):
    with pytest.raises(MiddeskError):
        MockMiddeskClient().get_business("does-not-exist")
