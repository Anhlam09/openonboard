"""Owners, SIMULATED identity checks (KYC) and the owner rules."""

from app import db, decision, kyc
from tests.signing import SIGNED

OWNER = {"name": "Jane Doe", "title": "CEO", "ownership_pct": 100, "control_person": True}
BODY = {
    "business_name": "Corporation",
    "tin": "12-3410000",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "NY",
    "postal_code": "10013",
    **SIGNED,
    "owners": [OWNER],
}
OK_BUSINESS = {
    "status": "approved",
    "review": {"tasks": [{"key": "tin", "label": "TIN", "status": "success", "message": "ok"}]},
}


def _final(client, body):
    a = client.post("/api/applications", json=body).json()
    return client.get(f"/api/applications/{a['id']}?refresh=true").json()


def test_ownership_over_100_rejected(client):
    owners = [{**OWNER, "ownership_pct": 60}, {"name": "Sam Lee", "ownership_pct": 50}]
    r = client.post("/api/applications", json={**BODY, "owners": owners})
    assert r.status_code == 422
    assert "more than 100%" in str(r.json())


def test_ownership_range_and_owners_optional(client):
    assert (
        client.post("/api/applications", json={**BODY, "owners": [{**OWNER, "ownership_pct": 101}]}).status_code == 422
    )
    assert client.post("/api/applications", json={**BODY, "owners": []}).status_code == 201


def test_clean_owner_recommends_approve(client):
    assert _final(client, BODY)["recommendation"] == "recommend_approve"


def test_missing_control_person_needs_review(client):
    body = {**BODY, "owners": [{**OWNER, "control_person": False}]}
    a = _final(client, body)
    assert a["recommendation"] == "needs_review"
    assert "No control person was listed (someone who manages the business)." in a["reasons"]


def test_no_owners_needs_review(client):
    a = _final(client, {**BODY, "owners": []})
    assert a["recommendation"] == "needs_review"
    assert "Owners with 25% or more may be missing." in a["reasons"]


def test_missing_large_owner_reason():
    owners = [{"name": "A B", "ownership_pct": 10, "control_person": True}]
    ev = decision.evaluate(OK_BUSINESS, True, people=owners, kyc=[])
    assert ev["recommendation"] == decision.NEEDS_REVIEW
    assert ev["reasons"] == ["Owners with 25% or more may be missing."]


def test_kyc_needs_review_is_soft(client):
    a = _final(client, {**BODY, "owners": [{**OWNER, "name": "J Doe"}]})
    assert a["recommendation"] == "needs_review" and a["priority"] == "normal"
    assert a["kyc"][0]["status"] == "needs_review" and a["kyc"][0]["simulated"] is True


def test_kyc_failed_is_high_priority_never_decline(client):
    a = _final(client, {**BODY, "owners": [{**OWNER, "name": "Fraud Person"}]})
    assert a["recommendation"] == "needs_review" and a["priority"] == "high"
    assert a["final_decision"] is None


def test_pending_wins_over_people_rules():
    ev = decision.evaluate({"status": "pending"}, True, people=[], kyc=[])
    assert ev["recommendation"] == decision.PENDING


def test_evaluate_without_people_unchanged():
    assert decision.evaluate(OK_BUSINESS, True)["recommendation"] == decision.RECOMMEND_APPROVE


def test_mock_kyc_rules():
    m = kyc.MockKYC()
    assert m.check_person("Jane Doe")["status"] == "verified"
    assert m.check_person("UNVERIFIED Bob")["status"] == "needs_review"
    assert m.check_person("j doe")["status"] == "needs_review"
    assert m.check_person("Major Fraud")["status"] == "failed"
    assert m.check_person("Jane Doe")["simulated"] is True


def test_kyc_stored_and_event_recorded(client):
    owners = [OWNER, {"name": "Unverified Pat", "ownership_pct": 0}]
    a = client.post("/api/applications", json={**BODY, "owners": [{**OWNER, "ownership_pct": 50}, owners[1]]}).json()
    assert [c["status"] for c in db.get(a["id"])["kyc"]] == ["verified", "needs_review"]
    ev = [e for e in db.list_events(a["id"]) if e["kind"] == "kyc_completed"]
    assert ev and ev[0]["detail"] == {"verified": 1, "needs_review": 1, "failed": 0, "simulated": True}
    assert "Jane" not in str(ev[0]["detail"])
