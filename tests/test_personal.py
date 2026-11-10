"""Personal accounts: no Middesk, last-4-only SSN, SIMULATED identity and watchlist checks."""

from datetime import UTC, datetime
from pathlib import Path

from app import config, decision, middesk
from tests.signing import PERSONAL_PRODUCTS, SIGNED, without_random_parts


def person(name="Ava Stone", **kw):
    return {
        "full_name": name,
        "date_of_birth": "1990-04-12",
        "ssn_last4": "6789",
        "email": "ava@example.com",
        "phone": "(212) 555-0100",
        "address_line1": "12 Main St",
        "city": "Albany",
        "state": "ny",
        "postal_code": "12207",
        **kw,
    }


def body(*people, **kw):
    return {
        "account_type": "personal",
        "individuals": list(people or [person()]),
        **SIGNED,
        "products": PERSONAL_PRODUCTS,
        **kw,
    }


def test_personal_happy_path_skips_middesk(client, monkeypatch):
    calls = []
    real = middesk.get_client

    def spy():
        calls.append(1)
        return real()

    monkeypatch.setattr("app.main.get_client", spy)
    r = client.post("/api/applications", json=body())
    assert r.status_code == 201
    a = r.json()
    assert a["recommendation"] == "recommend_approve"
    assert a["middesk_status"] == "not_applicable"
    assert a["business_name"] == "Ava Stone"
    assert a["applicant"]["individuals"][0]["state"] == "NY"
    refreshed = client.get(f"/api/applications/{a['id']}?refresh=true").json()
    assert refreshed["recommendation"] == "recommend_approve"
    assert calls == []


def test_joint_applicants_and_limit(client):
    a = client.post("/api/applications", json=body(person(), person("Ben Stone"))).json()
    assert len(a["applicant"]["individuals"]) == 2
    assert len(a["kyc"]) == 2
    assert client.post("/api/applications", json=body(*[person()] * 5)).status_code == 422
    assert client.post("/api/applications", json={**body(), "individuals": []}).status_code == 422


def test_under_18_and_future_dob_rejected(client):
    this_year = datetime.now(UTC).year
    for dob in (f"{this_year - 10}-01-01", f"{this_year + 1}-01-01"):
        assert client.post("/api/applications", json=body(person(date_of_birth=dob))).status_code == 422


def test_nine_digit_ssn_rejected_and_never_stored(client):
    r = client.post("/api/applications", json=body(person(ssn_last4="123-45-6789")))
    assert r.status_code == 422
    assert "last 4" in str(r.json())
    assert "123-45-6789" not in r.text and "123456789" not in r.text
    assert client.post("/api/applications", json=body(person(ssn_last4="123456789"))).status_code == 422
    assert client.post("/api/applications", json=body(person(ssn_last4="12"))).status_code == 422
    assert client.post("/api/applications", json=body({**person(), "ssn": "123456789"})).status_code == 422
    client.post("/api/applications", json=body())  # a valid one so the DB file exists
    raw = Path(config.DATABASE_PATH).read_bytes()
    assert b"123456789" not in raw and b"123-45-6789" not in raw


def test_j_doe_needs_review(client):
    a = client.post("/api/applications", json=body(person("J Doe"))).json()
    assert a["recommendation"] == "needs_review"
    assert a["priority"] == "normal"


def test_watchlist_name_is_high_priority(client):
    a = client.post("/api/applications", json=body(person(), person("Watchlist Hit"))).json()
    assert a["recommendation"] == "needs_review"
    assert a["priority"] == "high"
    assert any("Watchlist" in r for r in a["reasons"])


def test_no_auto_decline_and_human_decides(client):
    a = client.post("/api/applications", json=body(person("Fraud Person"))).json()
    assert a["recommendation"] == "needs_review"
    assert a["final_decision"] is None
    d = client.post(f"/api/applications/{a['id']}/decision", json={"decision": "approved"})
    assert d.status_code == 200


def test_business_without_account_type_still_works(client):
    r = client.post(
        "/api/applications",
        json={
            "business_name": "Corporation",
            "tin": "12-3410000",
            "address_line1": "223 Grand St.",
            "city": "New York",
            "state": "NY",
            "postal_code": "10013",
            **SIGNED,
        },
    )
    assert r.status_code == 201
    assert r.json()["applicant"]["account_type"] == "business"
    bad = client.post("/api/applications", json={"account_type": "business", **SIGNED})
    assert bad.status_code == 422


def test_csv_and_events_have_no_dob_or_ssn(client):
    a = client.post("/api/applications", json=body(person(), person("Ben Stone", ssn_last4="4321"))).json()
    csv_text = client.get("/api/audit.csv").text
    events = str(client.get(f"/api/applications/{a['id']}/events").json())
    assert "Ava Stone" in csv_text
    for text in (csv_text, events):
        text = without_random_parts(text)
        assert "1990-04-12" not in text
        assert "6789" not in text and "4321" not in text


def test_evaluate_personal_rules():
    assert decision.evaluate_personal([])["recommendation"] == "needs_review"
    ok = {"name": "A B", "status": "verified", "message": "ok"}
    assert decision.evaluate_personal([ok])["recommendation"] == "recommend_approve"
