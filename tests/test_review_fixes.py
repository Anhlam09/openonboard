"""Atomic results events, TIN redaction in notes and 422s, CSV safety, UI markup."""

import threading

from app import db
from app.main import csv_safe
from app.middesk import redact_tins
from tests.test_audit import BODY


def test_results_event_written_once_under_concurrency(client):
    a = client.post("/api/applications", json=BODY).json()
    fields = {"middesk_status": "open", "recommendation": "approve", "priority": "low"}
    errors = []

    def go():
        try:
            db.update_with_results_event(a["id"], "refresh", **fields)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    kinds = [e["kind"] for e in db.list_events(a["id"])]
    assert kinds.count("results_updated") == 2  # create snapshot, then one real change


def test_decision_note_tin_is_masked_everywhere(client):
    a = client.post("/api/applications", json=BODY).json()
    client.get(f"/api/applications/{a['id']}?refresh=true")
    r = client.post(
        f"/api/applications/{a['id']}/decision",
        json={"decision": "approved", "note": "123-45-6789 and 12-3456789 match"},
    )
    assert r.status_code == 200
    assert r.json()["reviewer_note"] == "*****6789 and *****6789 match"
    assert "*****6789" in str(client.get(f"/api/applications/{a['id']}/events").json())
    blob = str(db.list_events()) + client.get("/api/audit.csv").text
    assert "123-45-6789" not in blob and "3456789" not in blob.replace("*****6789", "")


def test_csv_safe_leading_whitespace():
    for v in ("  =1+1", "\n=1+1", "\x0b@x", " -2", "\t=1", "\r=1"):
        assert csv_safe(v).startswith("'"), repr(v)
    assert csv_safe("Acme") == "Acme"


def test_redact_mixed_separators():
    assert redact_tins("bad 123-45 6789 here") == "bad *****6789 here"
    assert redact_tins("bad 123 45-6789") == "bad *****6789"


def test_422_does_not_echo_tin(client):
    body = {k: v for k, v in BODY.items() if k != "business_name"}
    r = client.post("/api/applications", json={**body, "tin": "12-3456789"})
    assert r.status_code == 422
    assert "3456789" not in r.text and "input" not in r.text
    assert r.json()["detail"][0]["loc"]


def test_ui_has_filter_history_and_409_refresh(client):
    html = client.get("/").text
    for marker in (
        'id="filter"',
        'aria-label="Filter applications"',
        "aria-pressed",
        'id="history"',
        "loadList(false)",
    ):
        assert marker in html
    assert "if (x.status === 409) { await render(id); loadList(false); }" in html


# ---------- round 3 review fixes ----------
def test_omitted_or_blank_signature_rejected(client):
    from tests.test_audit import BODY

    body = {k: v for k, v in BODY.items() if k != "signature_name"}
    assert client.post("/api/applications", json=body).status_code == 422
    assert client.post("/api/applications", json={**BODY, "signature_name": "   "}).status_code == 422


def test_blank_owner_name_rejected(client):
    from tests.test_audit import BODY

    owners = [{"name": "   ", "ownership_pct": 100, "control_person": True}]
    assert client.post("/api/applications", json={**BODY, "owners": owners}).status_code == 422


def test_single_partial_owner_flags_missing_owner():
    from app import decision
    from tests.test_owners_kyc import OK_BUSINESS

    owners = [{"name": "A B", "ownership_pct": 30, "control_person": True}]
    ev = decision.evaluate(OK_BUSINESS, True, people=owners, kyc=[])
    assert ev["recommendation"] == decision.NEEDS_REVIEW
    assert "Owners with 25% or more may be missing." in ev["reasons"]


def test_old_application_without_owners_keeps_recommendation(client):
    from app import db, main
    from tests.test_owners_kyc import OK_BUSINESS

    db.insert(
        {"id": "old1", "business_name": "Old Co", "applicant": {"business_name": "Old Co"}, "tin_submitted": True}
    )
    out = main.apply_business(db.get("old1"), OK_BUSINESS)
    assert out["recommendation"] == "recommend_approve"


def test_oversized_upload_rejected_before_handler(client):
    r = client.post("/api/applications/nope/documents", content=b"x", headers={"content-length": str(50 * 1024 * 1024)})
    assert r.status_code == 413


def test_health_lists_simulated_parts(client):
    h = client.get("/api/health").json()
    assert {"kyc", "core_banking", "esign", "disclosures"} <= set(h["simulated"])


def test_form_has_in_flight_guard_and_banner_uses_simulated():
    from pathlib import Path

    html = (Path(__file__).parent.parent / "app/static/index.html").read_text()
    assert "if (submitting) return;" in html and "btn.disabled = true" in html
    assert "h.simulated" in html
