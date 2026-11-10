"""Append-only audit log and CSV export."""

import csv
import io
import sqlite3

import pytest

from app import db
from tests.signing import SIGNED

BODY = {
    "business_name": "Corporation",
    "tin": "12-3410000",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "ny",
    "postal_code": "10013",
    **SIGNED,
    "owners": [{"name": "Jane Doe", "title": "CEO", "ownership_pct": 100, "control_person": True}],
}


def _kinds(client, app_id):
    return [e["kind"] for e in client.get(f"/api/applications/{app_id}/events").json()]


def test_events_submit_refresh_decision_in_order(client):
    a = client.post("/api/applications", json=BODY).json()
    client.get(f"/api/applications/{a['id']}?refresh=true")
    client.get(f"/api/applications/{a['id']}?refresh=true")
    r = client.post(f"/api/applications/{a['id']}/decision", json={"decision": "approved", "note": "ok"})
    assert r.status_code == 200
    events = client.get(f"/api/applications/{a['id']}/events").json()
    assert [e["kind"] for e in events] == [
        "submitted",
        "kyc_completed",
        "disclosures_accepted",
        "products_selected",
        "results_updated",  # pending, from create
        "results_updated",  # first refresh: finished; the second refresh adds nothing
        "decision_recorded",
        "account_opened",  # simulated core, after the human approves
    ]
    assert events[0]["detail"] == {"tin_submitted": True, "owners": 1}
    assert events[6]["detail"] == {"decision": "approved", "note": "ok"}


def test_rejected_decision_adds_no_event(client):
    a = client.post("/api/applications", json=BODY).json()
    client.get(f"/api/applications/{a['id']}?refresh=true")
    d = {"decision": "approved", "note": None}
    assert client.post(f"/api/applications/{a['id']}/decision", json=d).status_code == 200
    n = len(_kinds(client, a["id"]))
    assert client.post(f"/api/applications/{a['id']}/decision", json={"decision": "declined"}).status_code == 409
    assert len(_kinds(client, a["id"])) == n


def test_events_unknown_app_404(client):
    assert client.get("/api/applications/nope/events").status_code == 404


def test_events_table_rejects_update_and_delete(client):
    a = client.post("/api/applications", json=BODY).json()
    with pytest.raises(sqlite3.IntegrityError), db.conn() as c:
        c.execute("UPDATE events SET kind='x' WHERE app_id=?", (a["id"],))
    with pytest.raises(sqlite3.IntegrityError), db.conn() as c:
        c.execute("DELETE FROM events")
    assert _kinds(client, a["id"])


def test_csv_export_masks_tin_and_neutralizes_formulas(client):
    client.post("/api/applications", json=BODY)
    client.post("/api/applications", json={**BODY, "business_name": '=HYPERLINK("x")'})
    r = client.get("/api/audit.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="openonboard-audit.csv"'
    assert "123410000" not in r.text and "12-3410000" not in r.text
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["time", "application_id", "reference_id", "business_name", "event", "details"]
    assert len(rows) >= 3
    assert rows[1][2] == "KYB-" + rows[1][1].replace("-", "")[:6].upper()
    assert any(row[3] == '\'=HYPERLINK("x")' for row in rows[1:])


def test_submitted_event_is_written_with_the_application(client, monkeypatch):
    """If the event insert fails, the application must not be saved without it."""
    from app import db

    real_add_event = db.add_event

    def failing_add_event(app_id, kind, detail=None, c=None):
        if kind == "submitted":
            raise RuntimeError("disk full")
        return real_add_event(app_id, kind, detail, c=c)

    monkeypatch.setattr(db, "add_event", failing_add_event)
    with pytest.raises(RuntimeError):
        db.insert({"id": "a-atomic", "business_name": "Atomic LLC", "applicant": {}, "tin_submitted": False}, event={})
    assert db.get("a-atomic") is None
