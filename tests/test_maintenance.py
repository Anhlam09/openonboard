"""Account maintenance requests: validation, human approval, SIMULATED core update, no sensitive digits."""

import json
import re
import sqlite3
from pathlib import Path

from app import config, db
from tests.signing import PERSONAL_PRODUCTS, without_random_parts
from tests.test_owners_kyc import BODY
from tests.test_personal import body as personal_body
from tests.test_personal import person

URL = "/api/applications"
CHECKING = PERSONAL_PRODUCTS[0]
SAVINGS = {"product_id": "personal-savings", "initial_deposit_cents": 5000}
OFFICER = "staff-grace-kim"


def approved(client, payload):
    a = client.post(URL, json=payload).json()
    client.get(f"{URL}/{a['id']}?refresh=true")
    r = client.post(f"{URL}/{a['id']}/decision", json={"decision": "approved"})
    assert r.status_code == 200
    return r.json()


def personal_app(client, *prods, **kw):
    return approved(client, personal_body(person(), products=list(prods or [CHECKING, SAVINGS]), **kw))


def business_app(client):
    return approved(client, BODY)


def ref(app, i=0):
    return app["accounts"][i]["account_ref"]


def ask(client, app_id, kind, details, **kw):
    return client.post(f"{URL}/{app_id}/maintenance", json={"kind": kind, "details": details, **kw})


def decide(client, req_id, decision="approve", **kw):
    return client.post(f"/api/maintenance/{req_id}/decision", json={"decision": decision, **kw})


def kinds_of(app_id):
    return [e["kind"] for e in db.list_events(app_id)]


def test_kinds_listed(client):
    ks = client.get("/api/maintenance/kinds").json()
    assert [k["id"] for k in ks] == [
        "contact_change",
        "product_change",
        "beneficiary_change",
        "signer_change",
        "debit_card_order",
        "check_order",
        "safe_deposit_box",
        "tax_id_change",
        "note",
    ]
    assert all(k["label"] and k["description"] for k in ks)


def test_not_approved_is_409_but_note_allowed(client):
    a = client.post(URL, json=personal_body()).json()
    r = ask(client, a["id"], "contact_change", {"email": "new@example.com"})
    assert r.status_code == 409
    assert ask(client, a["id"], "note", {"text": "Called the customer."}).status_code == 201
    declined = client.post(URL, json=personal_body()).json()
    client.get(f"{URL}/{declined['id']}?refresh=true")
    client.post(f"{URL}/{declined['id']}/decision", json={"decision": "declined"})
    assert ask(client, declined["id"], "check_order", {"quantity": 50}).status_code == 409
    assert ask(client, "nope", "note", {"text": "x"}).status_code == 404
    assert ask(client, a["id"], "weird", {}).status_code == 422


def test_note_completes_immediately(client):
    a = personal_app(client)
    r = ask(client, a["id"], "note", {"text": "Asked about fees. Ref 123-45-6789"}, requested_by=OFFICER)
    assert r.status_code == 201
    req = r.json()
    assert req["status"] == "completed" and req["requested_by"] == OFFICER
    assert "123-45-6789" not in str(req) and "123456789" not in str(req)
    assert kinds_of(a["id"])[-2:] == ["maintenance_requested", "maintenance_completed"]
    assert decide(client, req["id"]).status_code == 409
    assert ask(client, a["id"], "note", {"text": ""}).status_code == 422
    assert ask(client, a["id"], "note", {"text": "x" * 2001}).status_code == 422
    assert ask(client, a["id"], "note", {"text": "ok"}, requested_by="nobody").status_code == 404


def test_contact_change_validates_and_applies(client):
    a = personal_app(client)
    for bad in (
        {},
        {"email": "nope"},
        {"phone": "abc"},
        {"state": "New York"},
        {"postal_code": "1"},
        {"name": "A"},
        {"person_index": 3, "email": "a@example.com"},
        {"email": "a@example.com", "ssn": "1"},
    ):
        assert ask(client, a["id"], "contact_change", bad).status_code == 422, bad
    r = ask(client, a["id"], "contact_change", {"email": "new@example.com", "city": "Troy", "name": "Ava S Stone"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    mid = client.get(f"{URL}/{a['id']}").json()["applicant"]["individuals"][0]
    assert mid["email"] == "ava@example.com"  # nothing changes until a person approves
    done = decide(client, r.json()["id"], decided_by=OFFICER, note="Verified by phone")
    assert done.status_code == 200 and done.json()["status"] == "completed"
    after = client.get(f"{URL}/{a['id']}").json()
    p = after["applicant"]["individuals"][0]
    assert (p["email"], p["city"], p["full_name"]) == ("new@example.com", "Troy", "Ava S Stone")
    assert after["business_name"] == "Ava S Stone"
    assert {"maintenance_requested", "maintenance_approved", "maintenance_completed"} <= set(kinds_of(a["id"]))


def test_business_contact_change(client):
    a = business_app(client)
    assert ask(client, a["id"], "contact_change", {"person_index": 0, "city": "Troy"}).status_code == 422
    r = ask(client, a["id"], "contact_change", {"name": "New Corp", "city": "Troy", "email": "ops@example.com"})
    assert decide(client, r.json()["id"]).status_code == 200
    after = client.get(f"{URL}/{a['id']}").json()
    assert after["business_name"] == "New Corp" and after["applicant"]["city"] == "Troy"
    assert after["applicant"]["contact_email"] == "ops@example.com"


def test_product_change_validates_and_switches_account(client):
    a = personal_app(client)
    ref0 = ref(a)
    bad = [
        {"account_ref": "SIM-nope", "new_product_id": "personal-interest-checking"},
        {"account_ref": ref0, "new_product_id": "business-checking"},
        {"account_ref": ref0, "new_product_id": "nothing"},
        {"account_ref": ref0, "new_product_id": "personal-savings"},  # already has it
        {"account_ref": ref0, "new_product_id": "personal-certificate"},  # needs a term
        {"account_ref": ref0, "new_product_id": "personal-interest-checking", "term_months": 6},
    ]
    for d in bad:
        assert ask(client, a["id"], "product_change", d).status_code == 422, d
    r = ask(client, a["id"], "product_change", {"account_ref": ref0, "new_product_id": "personal-interest-checking"})
    assert decide(client, r.json()["id"]).status_code == 200
    after = client.get(f"{URL}/{a['id']}").json()
    assert after["accounts"][0]["product_id"] == "personal-interest-checking"
    assert after["accounts"][0]["product_name"] == "Interest Checking"
    assert after["accounts"][0]["account_ref"] == ref0
    assert [c["product_id"] for c in after["applicant"]["products"]][0] == "personal-interest-checking"
    # the open-account retry must not think the old product is missing
    assert client.post(f"{URL}/{a['id']}/open-account").status_code == 409


def test_product_change_to_certificate_needs_term(client):
    a = personal_app(client)
    d = {"account_ref": ref(a), "new_product_id": "personal-certificate", "term_months": 12}
    assert decide(client, ask(client, a["id"], "product_change", d).json()["id"]).status_code == 200
    ch = client.get(f"{URL}/{a['id']}").json()["applicant"]["products"][0]
    assert ch["product_id"] == "personal-certificate" and ch["term_months"] == 12


def test_beneficiary_change(client):
    a = personal_app(client)
    savings, checking = ref(a, 1), ref(a, 0)
    bens = [
        {"name": "Sam Stone", "relationship": "Brother", "share_pct": 60},
        {"name": "Lee Stone", "relationship": "Sister", "share_pct": 40},
    ]
    assert (
        ask(client, a["id"], "beneficiary_change", {"account_ref": checking, "beneficiaries": bens}).status_code == 422
    )
    assert ask(client, a["id"], "beneficiary_change", {"account_ref": savings, "beneficiaries": []}).status_code == 422
    over = [{**bens[0], "share_pct": 70}, bens[1]]
    r = ask(client, a["id"], "beneficiary_change", {"account_ref": savings, "beneficiaries": over})
    assert r.status_code == 422 and "100" in str(r.json())
    r = ask(client, a["id"], "beneficiary_change", {"account_ref": savings, "beneficiaries": bens})
    assert r.status_code == 201
    assert decide(client, r.json()["id"]).status_code == 200
    assert client.get(f"{URL}/{a['id']}").json()["accounts"][1]["beneficiaries"] == bens
    biz = business_app(client)
    assert (
        ask(client, biz["id"], "beneficiary_change", {"account_ref": ref(biz), "beneficiaries": bens}).status_code
        == 422
    )


def test_signer_change_business_only(client):
    a = business_app(client)
    base = {"account_ref": ref(a), "name": "Pat Lee", "title": "CFO"}
    assert ask(client, a["id"], "signer_change", {**base, "action": "remove"}).status_code == 422
    assert ask(client, a["id"], "signer_change", {**base, "action": "swap"}).status_code == 422
    assert (
        decide(client, ask(client, a["id"], "signer_change", {**base, "action": "add"}).json()["id"]).status_code == 200
    )
    assert ask(client, a["id"], "signer_change", {**base, "action": "add"}).status_code == 422
    signers = client.get(f"{URL}/{a['id']}").json()["accounts"][0]["signers"]
    assert signers == ["Jane Doe", "Pat Lee"]
    r = ask(client, a["id"], "signer_change", {**base, "action": "remove"})
    assert decide(client, r.json()["id"]).status_code == 200
    assert client.get(f"{URL}/{a['id']}").json()["accounts"][0]["signers"] == ["Jane Doe"]
    p = personal_app(client)
    assert ask(client, p["id"], "signer_change", {**base, "account_ref": ref(p), "action": "add"}).status_code == 422


def test_debit_card_and_checks_checking_only(client):
    a = personal_app(client)
    checking, savings = ref(a, 0), ref(a, 1)
    assert ask(client, a["id"], "debit_card_order", {"account_ref": savings, "reason": "new"}).status_code == 422
    assert ask(client, a["id"], "debit_card_order", {"account_ref": checking, "reason": "stolen"}).status_code == 422
    assert ask(client, a["id"], "check_order", {"account_ref": savings, "quantity": 50}).status_code == 422
    assert ask(client, a["id"], "check_order", {"account_ref": checking, "quantity": 75}).status_code == 422
    assert (
        ask(client, a["id"], "check_order", {"account_ref": checking, "quantity": 50, "style": "fancy"}).status_code
        == 422
    )
    r1 = ask(client, a["id"], "debit_card_order", {"account_ref": checking, "reason": "lost_stolen"})
    r2 = ask(client, a["id"], "check_order", {"account_ref": checking, "quantity": 100})
    assert decide(client, r1.json()["id"]).status_code == 200 and decide(client, r2.json()["id"]).status_code == 200
    orders = client.get(f"{URL}/{a['id']}").json()["accounts"][0]["orders"]
    assert [o["kind"] for o in orders] == ["debit_card_order", "check_order"]
    assert orders[0]["reason"] == "lost_stolen" and orders[1]["quantity"] == 100


def test_safe_deposit_box(client):
    a = personal_app(client)
    assert ask(client, a["id"], "safe_deposit_box", {"box_size": "huge", "branch": "Main"}).status_code == 422
    assert ask(client, a["id"], "safe_deposit_box", {"box_size": "small", "branch": ""}).status_code == 422
    r = ask(client, a["id"], "safe_deposit_box", {"box_size": "medium", "branch": "Downtown"})
    assert decide(client, r.json()["id"]).status_code == 200
    boxes = client.get(f"{URL}/{a['id']}").json()["applicant"]["safe_deposit_boxes"]
    assert boxes[0]["box_size"] == "medium" and boxes[0]["branch"] == "Downtown"


def test_tax_id_personal_last4_only(client):
    a = personal_app(client)
    for bad in ({"ssn_last4": "123456789"}, {"ssn_last4": "12"}, {"ssn_last4": "12ab"}, {"ein": "12-3456789"}, {}):
        assert ask(client, a["id"], "tax_id_change", bad).status_code == 422, bad
    r = ask(client, a["id"], "tax_id_change", {"ssn_last4": "4321"})
    assert r.status_code == 201
    assert decide(client, r.json()["id"]).status_code == 200
    assert client.get(f"{URL}/{a['id']}").json()["applicant"]["individuals"][0]["ssn_last4"] == "4321"


def test_tax_id_business_ein_masked_everywhere(client):
    a = business_app(client)
    assert ask(client, a["id"], "tax_id_change", {"ein": "12-345"}).status_code == 422
    assert ask(client, a["id"], "tax_id_change", {"ssn_last4": "1234"}).status_code == 422
    r = ask(client, a["id"], "tax_id_change", {"ein": "98-7654321"})
    assert r.status_code == 201
    assert "987654321" not in r.text and "98-7654321" not in r.text
    assert decide(client, r.json()["id"]).status_code == 200
    after = client.get(f"{URL}/{a['id']}").json()
    assert after["applicant"]["tin"] == "*****4321"
    assert after["tin_submitted"] is True
    blobs = [
        client.get(f"{URL}/{a['id']}/maintenance").text,
        client.get(f"{URL}/{a['id']}/events").text,
        client.get(f"{URL}/{a['id']}/export").text,
        client.get("/api/audit.csv").text,
    ]
    for b in blobs:
        assert "987654321" not in b and "98-7654321" not in b
    raw = b"".join(
        Path(config.DATABASE_PATH + s).read_bytes()
        for s in ("", "-wal", "-journal")
        if Path(config.DATABASE_PATH + s).exists()
    )
    assert b"987654321" not in raw and b"98-7654321" not in raw


def test_reject_leaves_data_unchanged(client):
    a = personal_app(client)
    before = client.get(f"{URL}/{a['id']}").json()
    r = ask(client, a["id"], "contact_change", {"email": "new@example.com"}).json()
    done = decide(client, r["id"], "reject", decided_by=OFFICER, note="Could not verify")
    assert done.status_code == 200 and done.json()["status"] == "rejected"
    assert done.json()["decision_note"] == "Could not verify" and done.json()["decided_by"] == OFFICER
    after = client.get(f"{URL}/{a['id']}").json()
    assert after["applicant"] == before["applicant"] and after["accounts"] == before["accounts"]
    ks = kinds_of(a["id"])
    assert "maintenance_rejected" in ks and "maintenance_completed" not in ks


def test_decide_twice_and_unknown(client):
    a = personal_app(client)
    r = ask(client, a["id"], "check_order", {"account_ref": ref(a), "quantity": 50}).json()
    assert decide(client, r["id"]).status_code == 200
    assert decide(client, r["id"]).status_code == 409
    assert decide(client, r["id"], "reject").status_code == 409
    assert decide(client, "nope").status_code == 404
    assert client.post(f"/api/maintenance/{r['id']}/decision", json={"decision": "maybe"}).status_code == 422
    r2 = ask(client, a["id"], "check_order", {"account_ref": ref(a), "quantity": 50}).json()
    assert decide(client, r2["id"], decided_by="nobody").status_code == 404


def test_list_requests_newest_first_and_missing_app(client):
    a = personal_app(client)
    ask(client, a["id"], "note", {"text": "first"})
    ask(client, a["id"], "note", {"text": "second"})
    got = client.get(f"{URL}/{a['id']}/maintenance").json()
    assert [g["details"]["text"] for g in got] == ["second", "first"]
    assert client.get(f"{URL}/nope/maintenance").status_code == 404


def test_events_have_no_sensitive_digits_and_csv_clean(client):
    a = personal_app(client)
    r = ask(client, a["id"], "tax_id_change", {"ssn_last4": "4321"}, requested_by=OFFICER).json()
    decide(client, r["id"], decided_by=OFFICER, note="SSN 123-45-6789 confirmed")
    ev = [e for e in db.list_events(a["id"]) if e["kind"].startswith("maintenance_")]
    assert [e["kind"] for e in ev] == ["maintenance_requested", "maintenance_approved", "maintenance_completed"]
    assert all(e["detail"]["request_id"] == r["id"] and e["detail"]["kind"] == "tax_id_change" for e in ev)
    text = without_random_parts(str(ev))
    assert "4321" not in text and not re.search(r"\d{9}", text)
    csv = client.get("/api/audit.csv").text
    assert "maintenance_requested" in csv and "123-45-6789" not in csv and "123456789" not in csv
    assert "123-45-6789" not in str(db.get_maintenance(r["id"]))


def test_table_and_index_exist(client):
    client.get("/api/health")
    db.list_all()
    c = sqlite3.connect(config.DATABASE_PATH)
    cols = {r[1] for r in c.execute("PRAGMA table_info(maintenance_requests)")}
    idx = {r[1] for r in c.execute("PRAGMA index_list(maintenance_requests)")}
    assert {
        "id",
        "app_id",
        "kind",
        "status",
        "details",
        "requested_by",
        "decided_by",
        "decided_at",
        "decision_note",
        "account_ref",
    } <= cols
    assert "idx_maintenance_app_id" in idx


def test_free_text_ssn_like_numbers_are_masked_everywhere(client):
    a = personal_app(client)
    r = ask(
        client,
        a["id"],
        "beneficiary_change",
        {
            "account_ref": ref(a, 1),
            "beneficiaries": [{"name": "Bob 987654321", "relationship": "son 123-45-6789", "share_pct": 100}],
        },
    )
    assert r.status_code == 201
    r2 = ask(client, a["id"], "contact_change", {"name": "Zed 111-22-3333"})
    assert r2.status_code == 201
    decide(client, r.json()["id"])
    decide(client, r2.json()["id"])
    everything = (
        client.get(f"/api/applications/{a['id']}/maintenance").text
        + client.get(f"/api/applications/{a['id']}").text
        + client.get("/api/audit.csv").text
    )
    for raw in ("987654321", "123-45-6789", "111-22-3333"):
        assert raw not in everything


def test_intake_name_masks_ssn_like_numbers(client):
    r = client.post("/api/applications", json=personal_body(person("Jane 123-45-6789 Doe")))
    assert r.status_code == 201
    assert "123-45-6789" not in r.text
    assert "123-45-6789" not in client.get("/api/audit.csv").text


def test_switch_to_a_chosen_but_unopened_product_is_refused(client):
    a = personal_app(client, CHECKING, SAVINGS)
    kept = [x for x in a["accounts"] if x["product_id"] == CHECKING["product_id"]]
    with db.conn() as c:  # as if the core had failed to open savings
        c.execute("UPDATE applications SET accounts=? WHERE id=?", (json.dumps(kept), a["id"]))
    r = ask(
        client,
        a["id"],
        "product_change",
        {"account_ref": kept[0]["account_ref"], "new_product_id": SAVINGS["product_id"]},
    )
    assert r.status_code == 422


def test_old_application_without_products_gets_a_default_account(client):
    from app import main

    a = approved(client, personal_body(person(), products=[CHECKING]))
    with db.conn() as c:
        row = db.get(a["id"])
        applicant = {k: v for k, v in row["applicant"].items() if k != "products"}
        c.execute(
            "UPDATE applications SET applicant=?, accounts='[]' WHERE id=?",
            (json.dumps(applicant), a["id"]),
        )
    row = db.get(a["id"])
    assert row["accounts"] == [] and len(main.missing_accounts(row)) == 1
    r = client.post(f"/api/applications/{a['id']}/open-account")
    assert r.status_code == 200
    assert len(r.json()["accounts"]) == 1
