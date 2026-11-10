"""Shared reviewer password: applicants never need it; every reviewer route does."""

import base64

import pytest

from tests.signing import SIGNED

APP = {
    "business_name": "Login Test LLC",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "NY",
    "postal_code": "10013",
    **SIGNED,
}


def basic(password: str, user: str = "reviewer") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture()
def locked(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "REVIEWER_PASSWORD", "s3cret pass")
    return client


def test_demo_mode_without_password_stays_open(client):
    assert client.get("/api/applications").status_code == 200
    assert client.get("/api/health").json()["reviewer_login"] is False


def test_reviewer_routes_need_the_password(locked):
    a = locked.post("/api/applications", json=APP)
    assert a.status_code == 201  # applying is public
    app_id = a.json()["id"]
    reviewer_routes = [
        ("GET", "/api/applications"),
        ("GET", f"/api/applications/{app_id}"),
        ("GET", f"/api/applications/{app_id}/events"),
        ("GET", f"/api/applications/{app_id}/export"),
        ("GET", f"/api/applications/{app_id}/documents"),
        ("GET", f"/api/applications/{app_id}/maintenance"),
        ("GET", "/api/audit.csv"),
        ("GET", "/api/staff"),
        ("POST", f"/api/applications/{app_id}/decision"),
        ("POST", f"/api/applications/{app_id}/assignees"),
        ("POST", "/api/maintenance/00000000-0000-0000-0000-000000000000/decision"),
    ]
    for method, path in reviewer_routes:
        r = locked.request(method, path, json={})
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.headers["www-authenticate"].startswith("Basic")
        assert "Login Test" not in r.text


def test_wrong_or_malformed_credentials_are_refused(locked):
    for headers in (basic("wrong"), basic(""), {"Authorization": "Basic !!!notbase64"}, {"Authorization": "Bearer x"}):
        assert locked.get("/api/applications", headers=headers).status_code == 401


def test_right_password_works_with_any_username(locked):
    assert locked.get("/api/applications", headers=basic("s3cret pass")).status_code == 200
    assert locked.get("/api/applications", headers=basic("s3cret pass", user="maria")).status_code == 200


def test_applicant_routes_stay_public(locked):
    for path in (
        "/api/health",
        "/api/disclosures",
        "/api/products?account_type=business",
        "/api/maintenance/kinds",
        "/",
    ):
        assert locked.get(path).status_code == 200, path
    app_id = locked.post("/api/applications", json=APP).json()["id"]
    pdf = ("a.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")
    up = locked.post(f"/api/applications/{app_id}/documents", data={"doc_type": "other"}, files={"file": pdf})
    assert up.status_code == 201
    assert locked.get("/api/health").json()["reviewer_login"] is True


def test_public_paths_cannot_be_used_to_reach_reviewer_data(locked):
    app_id = locked.post("/api/applications", json=APP).json()["id"]
    # same paths as public ones but a different method, or a longer path, are still protected
    assert locked.get(f"/api/applications/{app_id}/documents").status_code == 401
    assert locked.get("/api/applications").status_code == 401
    assert locked.get("/api/health/../applications").status_code in (401, 404)


def test_connected_to_middesk_without_password_refuses_reviewer_routes(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "REVIEWER_PASSWORD", "")
    r = client.get("/api/applications")
    assert r.status_code == 503 and "REVIEWER_PASSWORD" in r.text
    assert client.get("/api/health").status_code == 200


def test_webhook_is_not_behind_the_password(locked):
    # It has its own HMAC check: a bad signature is a 400, not a 401.
    r = locked.post("/webhooks/middesk", content=b"{}", headers={"X-Middesk-Signature-256": "nope"})
    assert r.status_code == 400
