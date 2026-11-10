"""SIMULATED account opening and first deposit after a human approves."""

import pytest

from app import core_banking, db
from tests.test_owners_kyc import BODY


def _ready(client, **over):
    a = client.post("/api/applications", json={**BODY, **over}).json()
    return client.get(f"/api/applications/{a['id']}?refresh=true").json()


def _decide(client, app_id, decision="approved"):
    return client.post(f"/api/applications/{app_id}/decision", json={"decision": decision})


def _kinds(app_id):
    return [e["kind"] for e in db.list_events(app_id)]


def test_approve_opens_account_with_last4_only(client):
    a = _ready(client)
    r = _decide(client, a["id"]).json()
    assert r["core_account_ref"].startswith("SIM-") and len(r["core_account_ref"]) == 12
    assert len(r["account_last4"]) == 4 and r["account_last4"].isdigit()
    assert r["funding_status"] == "awaiting_deposit" and r["account_opened_at"]
    ev = [e for e in db.list_events(a["id"]) if e["kind"] == "account_opened"]
    assert ev[0]["detail"] == {
        "product_id": "business-checking",
        "account_ref": r["core_account_ref"],
        "account_last4": r["account_last4"],
        "simulated": True,
    }


def test_decline_never_calls_core(client, monkeypatch):
    monkeypatch.setattr(core_banking, "open_account", lambda row, product_id: pytest.fail("core called"))
    a = _ready(client)
    r = _decide(client, a["id"], "declined").json()
    assert r["core_account_ref"] is None and r["funding_status"] is None
    assert "account_opened" not in _kinds(a["id"])


def test_account_ref_is_deterministic():
    row = {"id": "abc-123", "business_name": "Acme"}
    assert core_banking.open_account(row, "p1") == core_banking.open_account(row, "p1")
    assert core_banking.open_account(row, "p1") != core_banking.open_account({**row, "id": "other"}, "p1")
    assert core_banking.open_account(row, "p1") != core_banking.open_account(row, "p2")


def test_core_down_keeps_decision_and_retry_works(client, monkeypatch):
    a = _ready(client, business_name="Core down Trading LLC")
    r = _decide(client, a["id"])
    assert r.status_code == 200 and r.json()["final_decision"] == "approved"
    assert r.json()["core_account_ref"] is None
    failed = [e for e in db.list_events(a["id"]) if e["kind"] == "account_open_failed"]
    assert failed[0]["detail"]["message"] == "The core banking system did not respond (simulated)"
    monkeypatch.setattr(
        core_banking,
        "open_account",
        lambda row, product_id: {"account_ref": "SIM-ABCDEF12", "account_last4": "4321", "simulated": True},
    )
    r = client.post(f"/api/applications/{a['id']}/open-account")
    assert r.status_code == 200 and r.json()["account_last4"] == "4321"
    assert "account_opened" in _kinds(a["id"])


def test_open_account_409_and_404(client):
    assert client.post("/api/applications/nope/open-account").status_code == 404
    a = _ready(client)
    assert client.post(f"/api/applications/{a['id']}/open-account").status_code == 409  # not decided
    _decide(client, a["id"])
    assert client.post(f"/api/applications/{a['id']}/open-account").status_code == 409  # already opened
    d = _ready(client)
    _decide(client, d["id"], "declined")
    assert client.post(f"/api/applications/{d['id']}/open-account").status_code == 409


def test_deposit_flow_and_errors(client):
    assert client.post("/api/applications/nope/simulate-deposit", json={"amount_cents": 500}).status_code == 404
    a = _ready(client)
    url = f"/api/applications/{a['id']}/simulate-deposit"
    assert client.post(url, json={"amount_cents": 500}).status_code == 409  # no account yet
    _decide(client, a["id"])
    for bad in (99, 100_000_001, "x"):
        assert client.post(url, json={"amount_cents": bad}).status_code == 422
    r = client.post(url, json={"amount_cents": 25000})
    assert r.status_code == 200
    assert r.json()["funding_status"] == "funded" and r.json()["funded_amount_cents"] == 25000
    assert client.post(url, json={"amount_cents": 25000}).status_code == 409
    ev = [e for e in db.list_events(a["id"]) if e["kind"] == "deposit_received"]
    assert ev[0]["detail"] == {"product_id": "business-checking", "amount_cents": 25000, "simulated": True}


def test_full_flow_submit_to_funded(client):
    a = _ready(client)
    assert a["funding_status"] is None
    _decide(client, a["id"])
    client.post(f"/api/applications/{a['id']}/simulate-deposit", json={"amount_cents": 100000})
    row = client.get(f"/api/applications/{a['id']}").json()
    assert row["final_decision"] == "approved" and row["funding_status"] == "funded"
    kinds = _kinds(a["id"])
    assert kinds.index("decision_recorded") < kinds.index("account_opened") < kinds.index("deposit_received")
    assert "3410000" not in str(db.list_events(a["id"]))
