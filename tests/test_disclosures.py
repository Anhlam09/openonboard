"""Disclosures and the SIMULATED typed signature."""

from app import disclosures
from tests.test_audit import BODY


def test_disclosures_shape(client):
    d = client.get("/api/disclosures").json()
    assert d["version"] == disclosures.DISCLOSURE_VERSION
    assert d["placeholder"] is True
    assert len(d["sections"]) == 4
    assert all("SAMPLE TEXT" in s["body"] and s["title"] for s in d["sections"])


def test_missing_acceptance_rejected(client):
    assert client.post("/api/applications", json={**BODY, "disclosures_accepted": False}).status_code == 422
    body = {k: v for k, v in BODY.items() if k != "disclosures_accepted"}
    assert client.post("/api/applications", json=body).status_code == 422


def test_missing_signature_rejected(client):
    for sig in (None, "", " ", "J"):
        assert client.post("/api/applications", json={**BODY, "signature_name": sig}).status_code == 422


def test_wrong_version_rejected(client):
    r = client.post("/api/applications", json={**BODY, "disclosure_version": "old"})
    assert r.status_code == 422
    assert "The disclosures changed. Please review them again." in r.text


def test_signature_stored_with_event(client):
    a = client.post("/api/applications", json=BODY).json()
    ap = a["applicant"]
    assert ap["disclosure_version"] == disclosures.DISCLOSURE_VERSION
    assert ap["signature_name"] == "Jane Doe"
    assert ap["signed_at"].endswith("+00:00")
    ev = [e for e in client.get(f"/api/applications/{a['id']}/events").json() if e["kind"] == "disclosures_accepted"]
    assert len(ev) == 1
    assert ev[0]["detail"] == {"version": disclosures.DISCLOSURE_VERSION, "signature_name": "Jane Doe"}
