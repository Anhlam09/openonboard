import hashlib
import hmac
import json

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


def test_full_flow_clean_business(client):
    r = client.post("/api/applications", json=GOOD)
    assert r.status_code == 201
    app = r.json()
    assert app["recommendation"] == "pending"
    assert app["applicant"]["tin"] == "*****0000"  # TIN masked
    app = client.get(f"/api/applications/{app['id']}?refresh=true").json()
    assert app["recommendation"] == "recommend_approve"
    r = client.post(f"/api/applications/{app['id']}/decision", json={"decision": "approved"})
    assert r.json()["final_decision"] == "approved"


def test_full_tin_never_stored(client, tmp_path):
    from app import config

    client.post("/api/applications", json=GOOD)
    with open(config.DATABASE_PATH, "rb") as f:
        raw = f.read()
    assert b"123410000" not in raw


def test_validation_errors(client):
    bad = {**GOOD, "tin": "123", "state": "New York", "postal_code": "abc"}
    assert client.post("/api/applications", json=bad).status_code == 422


def test_cannot_decide_before_results(client):
    app = client.post("/api/applications", json=GOOD).json()
    assert client.post(f"/api/applications/{app['id']}/decision", json={"decision": "approved"}).status_code == 409


def test_watchlist_sample(client):
    app = client.post("/api/applications", json={**GOOD, "business_name": "Watchlist Trading LLC"}).json()
    app = client.get(f"/api/applications/{app['id']}?refresh=true").json()
    assert app["recommendation"] == "needs_review" and app["priority"] == "high"


def _sign(body: bytes) -> str:
    return hmac.new(b"sec_test", body, hashlib.sha256).hexdigest()


def test_webhook_rejects_bad_signature(client):
    body = json.dumps({"type": "business.updated", "data": {"object": {"id": "x"}}}).encode()
    r = client.post("/webhooks/middesk", content=body, headers={"X-Middesk-Signature-256": "nope"})
    assert r.status_code == 400


def test_webhook_updates_application(client):
    app = client.post("/api/applications", json=GOOD).json()
    obj = {
        "id": app["middesk_id"],
        "status": "in_review",
        "review": {
            "tasks": [
                {"category": "tin", "key": "tin", "label": "TIN Match", "status": "failure", "message": "No IRS match"}
            ]
        },
    }
    body = json.dumps({"type": "business.updated", "data": {"object": obj}}).encode()
    r = client.post("/webhooks/middesk", content=body, headers={"X-Middesk-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert client.get(f"/api/applications/{app['id']}").json()["recommendation"] == "recommend_decline"


def test_unregistered_sample_is_likely_decline(client):
    app = client.post("/api/applications", json={**GOOD, "business_name": "Unregistered Business"}).json()
    app = client.get(f"/api/applications/{app['id']}?refresh=true").json()
    assert app["recommendation"] == "recommend_decline"


def test_payload_matches_middesk_format():
    from app.main import ApplicationIn, to_middesk_payload

    p = to_middesk_payload(ApplicationIn(**{**GOOD, "website": "https://example.com"}), "app-1")
    assert p["tin"] == {"tin": "123410000"}
    assert p["addresses"][0] == {
        "address_line1": "223 Grand St.",
        "city": "New York",
        "state": "NY",
        "postal_code": "10013",
    }
    assert p["people"] == [{"name": "Jane Doe"}]
    assert p["website"] == {"url": "https://example.com"}
    assert p["external_id"] == "app-1"
    assert p["orders"] == [{"product": "business_verification_verify"}]


def test_real_client_uses_bearer_auth():
    from app.middesk import MiddeskClient

    c = MiddeskClient("mk_test_abc", "https://api-sandbox.middesk.com/v1")
    assert c._http.headers["Authorization"] == "Bearer mk_test_abc"


def test_check_script_refuses_mock_mode(capsys):
    import pytest

    from app import check_middesk

    with pytest.raises(SystemExit):
        check_middesk.main()
    assert "mock mode" in capsys.readouterr().out


def test_index_has_queue_filter(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'aria-label="Filter applications"' in r.text
