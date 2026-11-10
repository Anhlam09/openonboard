"""Error handling, TIN redaction, one-time decisions, webhook edge cases and DB migration."""

import hashlib
import hmac
import json
import sqlite3

import httpx
import pytest

from app.middesk import MiddeskClient, MiddeskError, redact_tins
from tests.signing import SIGNED

GOOD = {
    "business_name": "Corporation",
    "tin": "12-3410000",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "NY",
    "postal_code": "10013",
    **SIGNED,
    "owners": [{"name": "Jane Doe", "title": "CEO", "ownership_pct": 100, "control_person": True}],
}


def _sign(body: bytes) -> str:
    return hmac.new(b"sec_test", body, hashlib.sha256).hexdigest()


def _client_with(handler) -> MiddeskClient:
    c = MiddeskClient("mk_test_abc", "https://api-sandbox.middesk.com/v1")
    c._http = httpx.Client(base_url="https://api-sandbox.middesk.com/v1", transport=httpx.MockTransport(handler))
    return c


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.middesk.time.sleep", lambda _s: None)


# ---------- TIN redaction ----------
def test_redact_tins_masks_both_formats():
    assert redact_tins("tin 123410000 and 12-3410000") == "tin *****0000 and *****0000"


def test_redact_tins_masks_ssn_and_spaced_formats():
    assert redact_tins("123-45-6789 / 12 3410000 / 123 45 6789") == "*****6789 / *****0000 / *****6789"


def test_redact_tins_leaves_other_numbers_alone():
    assert redact_tins("ZIP 10013-1234, phone 2125551234, 12345") == "ZIP 10013-1234, phone 2125551234, 12345"


def test_middesk_error_message_is_redacted():
    assert "123410000" not in str(MiddeskError('Middesk 422: {"tin": "123410000"}'))


def test_middesk_error_echoing_tin_is_not_stored(client, monkeypatch):
    from app import config, middesk

    echo = _client_with(lambda req: httpx.Response(422, json={"errors": [{"tin": json.loads(req.content)["tin"]}]}))
    monkeypatch.setattr(middesk, "_client", echo)

    r = client.post("/api/applications", json=GOOD)
    assert r.status_code == 502
    assert "123410000" not in r.text and "*****0000" in r.text
    with open(config.DATABASE_PATH, "rb") as f:
        assert b"123410000" not in f.read()


# ---------- Middesk client errors and retries ----------
def test_network_error_becomes_middesk_error(no_sleep):
    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(MiddeskError, match="Could not connect"):
        _client_with(boom).get_business("b1")


def test_post_is_not_retried_after_server_error(no_sleep):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, text="down")

    with pytest.raises(MiddeskError, match="503"):
        _client_with(handler).create_business({"name": "x"})
    assert len(calls) == 1  # a retry could create a second, billed business


def test_get_is_retried_after_server_error(no_sleep):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"id": "b1"})

    assert _client_with(handler).get_business("b1") == {"id": "b1"}
    assert len(calls) == 3


def test_rate_limit_is_retried_for_post(no_sleep):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json={"id": "b1"})

    assert _client_with(handler).create_business({"name": "x"}) == {"id": "b1"}
    assert len(calls) == 2


def test_non_json_response_becomes_middesk_error():
    with pytest.raises(MiddeskError, match="not JSON"):
        _client_with(lambda req: httpx.Response(200, text="<html>")).get_business("b1")


def test_create_returns_502_when_middesk_unreachable(client, monkeypatch, no_sleep):
    from app import middesk

    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    monkeypatch.setattr(middesk, "_client", _client_with(boom))
    r = client.post("/api/applications", json=GOOD)
    assert r.status_code == 502
    assert client.get("/api/applications").json()[0]["middesk_status"] == "error"


# ---------- decisions ----------
def _verified_app(client) -> dict:
    a = client.post("/api/applications", json=GOOD).json()
    return client.get(f"/api/applications/{a['id']}?refresh=true").json()


def test_decision_is_recorded_once_with_time(client):
    a = _verified_app(client)
    first = client.post(f"/api/applications/{a['id']}/decision", json={"decision": "approved", "note": "ok"})
    assert first.status_code == 200 and first.json()["decided_at"]

    second = client.post(f"/api/applications/{a['id']}/decision", json={"decision": "declined"})
    assert second.status_code == 409
    after = client.get(f"/api/applications/{a['id']}").json()
    assert after["final_decision"] == "approved" and after["reviewer_note"] == "ok"


def test_decision_on_missing_application_is_404(client):
    assert client.post("/api/applications/nope/decision", json={"decision": "approved"}).status_code == 404


# ---------- webhook edge cases ----------
def _post_webhook(client, body: bytes):
    return client.post("/webhooks/middesk", content=body, headers={"X-Middesk-Signature-256": _sign(body)})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_webhook_rejects_signed_garbage(client, body):
    assert _post_webhook(client, body).status_code == 400


@pytest.mark.parametrize(
    "event",
    [{"type": None, "data": {"object": {"id": "x"}}}, {"type": "business.updated", "data": ["x"]}, {}],
)
def test_webhook_ignores_odd_shapes(client, event):
    assert _post_webhook(client, json.dumps(event).encode()).json() == {"received": True}


def test_webhook_before_middesk_id_saved_uses_external_id(client):
    from app import db

    db.insert(
        {
            "id": "app-early",
            "business_name": "Early LLC",
            "applicant": {"owners": GOOD["owners"]},
            "tin_submitted": True,
        }
    )
    obj = {
        "id": "mid-early",
        "external_id": "app-early",
        "status": "in_review",
        "review": {"tasks": [{"key": "tin", "label": "TIN Match", "status": "success", "message": "ok"}]},
    }
    body = json.dumps({"type": "business.updated", "data": {"object": obj}}).encode()
    assert _post_webhook(client, body).status_code == 200

    row = client.get("/api/applications/app-early").json()
    assert row["middesk_id"] == "mid-early" and row["recommendation"] == "recommend_approve"


def test_webhook_external_id_cannot_relink_another_business(client):
    from app import db

    a = client.post("/api/applications", json=GOOD).json()
    obj = {"id": "someone-else", "external_id": a["id"], "status": "in_review", "review": {"tasks": []}}
    body = json.dumps({"type": "business.updated", "data": {"object": obj}}).encode()
    _post_webhook(client, body)
    row = db.get(a["id"])
    assert row["middesk_id"] == a["middesk_id"] and row["recommendation"] == "pending"


# ---------- database ----------
def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    from app import config, db

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE applications (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "business_name TEXT NOT NULL, applicant TEXT NOT NULL, tin_submitted INTEGER NOT NULL, middesk_id TEXT, "
        "middesk_status TEXT, recommendation TEXT, priority TEXT, reasons TEXT, tasks TEXT, "
        "final_decision TEXT, reviewer_note TEXT)"
    )
    old.commit()
    old.close()

    monkeypatch.setattr(config, "DATABASE_PATH", path)
    db.insert({"id": "a1", "business_name": "Old Co", "applicant": {}, "tin_submitted": False})
    assert db.set_final_decision("a1", "declined", None)
    assert db.get("a1")["decided_at"]


def test_create_does_not_overwrite_results_from_an_early_webhook(client, monkeypatch):
    """Middesk's webhook can land before create_business() returns to us."""
    from app import middesk

    done = {"status": "in_review", "review": {"tasks": [{"key": "tin", "label": "TIN", "status": "success"}]}}

    class EarlyWebhookClient:
        def create_business(self, payload):
            obj = {"id": "mid-race", "external_id": payload["external_id"], **done}
            body = json.dumps({"type": "business.updated", "data": {"object": obj}}).encode()
            assert _post_webhook(client, body).status_code == 200
            return {"id": "mid-race", "status": "pending", "review": None}  # the older snapshot

    monkeypatch.setattr(middesk, "_client", EarlyWebhookClient())
    a = client.post("/api/applications", json=GOOD).json()
    assert a["recommendation"] == "recommend_approve" and a["middesk_status"] == "in_review"


def test_database_file_removed_while_running_is_recreated(client):
    import os

    from app import config

    client.post("/api/applications", json=GOOD)
    os.remove(config.DATABASE_PATH)
    assert client.get("/api/applications").json() == []
