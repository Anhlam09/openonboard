"""One application that opens business AND personal accounts together."""

from app import decision, middesk
from tests.signing import SIGNED, without_random_parts
from tests.test_owners_kyc import BODY, OWNER
from tests.test_personal import person

URL = "/api/applications"
BIZ = {"product_id": "business-checking", "initial_deposit_cents": 10000}
BIZ_SAV = {"product_id": "business-savings", "initial_deposit_cents": 25000}
PERS = {"product_id": "personal-everyday-checking", "initial_deposit_cents": 2500}
PERS_SAV = {"product_id": "personal-savings", "initial_deposit_cents": 5000}
PERS_CD = {"product_id": "personal-certificate", "initial_deposit_cents": 100000, "term_months": 12}


def both(*, individuals=None, products=None, **kw):
    return {
        **BODY,
        **SIGNED,
        "account_type": "both",
        "individuals": [person("Jane Doe")] if individuals is None else individuals,
        "products": [BIZ, PERS] if products is None else products,
        **kw,
    }


def make(client, payload):
    a = client.post(URL, json=payload).json()
    return client.get(f"{URL}/{a['id']}?refresh=true").json()


def approve(client, payload):
    a = make(client, payload)
    r = client.post(f"{URL}/{a['id']}/decision", json={"decision": "approved"})
    assert r.status_code == 200
    return r.json()


def test_combined_happy_path(client, monkeypatch):
    calls = []
    real = middesk.get_client

    def spy():
        calls.append(1)
        return real()

    monkeypatch.setattr("app.main.get_client", spy)
    r = client.post(
        URL, json=both(individuals=[person("Jane Doe"), person("Ben Stone")], products=[BIZ, BIZ_SAV, PERS])
    )
    assert r.status_code == 201
    a = r.json()
    assert a["business_name"] == "Corporation"
    assert a["applicant"]["account_type"] == "both"
    assert calls == [1]  # Middesk is called once, for the business part
    assert [c["role"] for c in a["kyc"]] == ["owner", "applicant", "applicant"]
    assert [c["name"] for c in a["kyc"]] == ["Jane Doe", "Jane Doe", "Ben Stone"]
    a = client.get(f"{URL}/{a['id']}?refresh=true").json()
    assert calls == [1, 1]  # a refresh asks Middesk again, like a business
    assert a["recommendation"] == "recommend_approve"
    assert any(x.startswith("Business: ") for x in a["reasons"])
    assert any(x.startswith("Personal: ") for x in a["reasons"])
    done = client.post(f"{URL}/{a['id']}/decision", json={"decision": "approved"}).json()
    refs = [x["account_ref"] for x in done["accounts"]]
    assert len(refs) == 3 and len(set(refs)) == 3
    assert {x["product_id"] for x in done["accounts"]} == {"business-checking", "business-savings", PERS["product_id"]}


def test_combined_validation(client):
    def code(payload):
        return client.post(URL, json=payload).status_code

    only_biz = client.post(URL, json=both(products=[BIZ, BIZ_SAV]))
    assert only_biz.status_code == 422
    assert "at least one business account and one personal account" in str(only_biz.json())
    assert code(both(products=[PERS, PERS_SAV])) == 422
    assert code(both(individuals=[])) == 422
    no_biz = {k: v for k, v in both().items() if k not in ("business_name", "address_line1")}
    assert code(no_biz) == 422
    assert code(both(tin="123")) == 422
    assert code(both(owners=[{**OWNER, "ownership_pct": 60}, {"name": "Sam Lee", "ownership_pct": 50}])) == 422
    assert code(both(individuals=[person(ssn_last4="123-45-6789")])) == 422
    assert code(both(products=[BIZ, BIZ, PERS])) == 422
    seven = [
        BIZ,
        BIZ_SAV,
        {"product_id": "business-interest-checking", "initial_deposit_cents": 50000},
        PERS,
        PERS_SAV,
        {"product_id": "personal-interest-checking", "initial_deposit_cents": 10000},
        PERS_CD,
    ]
    assert code(both(products=seven)) == 422
    assert code(both(products=seven[:6])) == 201
    # a single kind still allows at most 4 accounts and only its own kind
    assert code({**BODY, **SIGNED, "products": [BIZ, BIZ_SAV, *seven[2:3], PERS]}) == 422


def test_existing_payloads_unchanged(client):
    assert client.post(URL, json={**BODY, **SIGNED}).json()["applicant"]["account_type"] == "business"


def ev(rec, priority="normal", reasons=("r",)):
    return {"recommendation": rec, "priority": priority, "reasons": list(reasons)}


def test_evaluate_combined_severity():
    A, N, D = decision.RECOMMEND_APPROVE, decision.NEEDS_REVIEW, decision.RECOMMEND_DECLINE
    cases = [(A, A, A), (A, N, N), (N, A, N), (D, N, D), (N, D, D), (A, D, D), (D, A, D)]
    for b, p, want in cases:
        assert decision.evaluate_combined(ev(b), ev(p))["recommendation"] == want


def test_evaluate_combined_pending_priority_and_reasons():
    out = decision.evaluate_combined(ev(decision.PENDING, reasons=["wait"]), ev(decision.NEEDS_REVIEW, "high", ["hit"]))
    assert out["recommendation"] == decision.PENDING
    assert out["priority"] == "high"
    assert out["reasons"] == ["Business: wait", "Personal: hit"]
    assert decision.evaluate_combined(ev(decision.PENDING), ev(decision.RECOMMEND_APPROVE))["priority"] == "normal"
    out = decision.evaluate_combined(ev(decision.NEEDS_REVIEW, "high"), ev(decision.RECOMMEND_APPROVE))
    assert out["priority"] == "high"


def test_watchlist_applicant_on_clean_business(client):
    a = make(client, both(individuals=[person("Wendy Watchlist")]))
    assert a["recommendation"] == "needs_review" and a["priority"] == "high"
    assert any(r.startswith("Personal: ") and "Watchlist" in r for r in a["reasons"])
    # never an automatic decline or account
    assert a["final_decision"] is None and a["accounts"] == []


def test_failed_owner_id_is_business_part(client):
    a = make(client, both(owners=[{**OWNER, "name": "Fraud Person"}]))
    assert a["recommendation"] == "needs_review" and a["priority"] == "high"
    assert any(r.startswith("Business: ") and "Identity check" in r for r in a["reasons"])


# ---------- maintenance ----------
def ask(client, app, kind, details):
    return client.post(f"{URL}/{app['id']}/maintenance", json={"kind": kind, "details": details})


def combined_open(client):
    return approve(client, both(products=[BIZ, BIZ_SAV, PERS, PERS_SAV]))


def refs(a):
    return {x["product_id"]: x["account_ref"] for x in a["accounts"]}


def test_maintenance_per_account_rules(client):
    a = combined_open(client)
    r = refs(a)
    signer = {"action": "add", "name": "Sam Lee"}
    assert ask(client, a, "signer_change", {**signer, "account_ref": r["business-savings"]}).status_code == 201
    assert ask(client, a, "signer_change", {**signer, "account_ref": r[PERS["product_id"]]}).status_code == 422
    benes = [{"name": "Ben Stone", "relationship": "Spouse", "share_pct": 100}]
    assert (
        ask(client, a, "beneficiary_change", {"account_ref": r["personal-savings"], "beneficiaries": benes}).status_code
        == 201
    )
    assert (
        ask(client, a, "beneficiary_change", {"account_ref": r["business-savings"], "beneficiaries": benes}).status_code
        == 422
    )
    assert (
        ask(client, a, "debit_card_order", {"account_ref": r["business-checking"], "reason": "new"}).status_code == 201
    )
    # a business account can only switch to another business product
    pc = {"account_ref": r["business-savings"], "new_product_id": "personal-interest-checking"}
    assert ask(client, a, "product_change", pc).status_code == 422
    ok = {"account_ref": r["business-savings"], "new_product_id": "business-interest-checking"}
    assert ask(client, a, "product_change", ok).status_code == 201


def test_maintenance_targets(client):
    a = combined_open(client)
    for kind, d in (("contact_change", {"phone": "(212) 555-0199"}), ("tax_id_change", {"ein": "98-7654321"})):
        assert ask(client, a, kind, d).status_code == 422  # no target
    assert (
        ask(client, a, "contact_change", {"target": "business", "person_index": 0, "phone": "212 555 0199"}).status_code
        == 422
    )
    assert ask(client, a, "contact_change", {"person_index": 3, "phone": "212 555 0199"}).status_code == 422
    # business tax id: EIN in full, stored masked
    assert ask(client, a, "tax_id_change", {"target": "business", "ssn_last4": "1234"}).status_code == 422
    assert ask(client, a, "tax_id_change", {"person_index": 0, "ein": "98-7654321"}).status_code == 422
    assert ask(client, a, "tax_id_change", {"person_index": 0, "ssn_last4": "123456789"}).status_code == 422
    r = ask(client, a, "tax_id_change", {"target": "business", "ein": "98-7654321"})
    assert r.status_code == 201 and "987654321" not in r.text and "98-7654321" not in r.text
    assert client.post(f"/api/maintenance/{r.json()['id']}/decision", json={"decision": "approve"}).status_code == 200
    r = ask(client, a, "tax_id_change", {"person_index": 0, "ssn_last4": "4321"})
    assert r.status_code == 201
    client.post(f"/api/maintenance/{r.json()['id']}/decision", json={"decision": "approve"})
    r = ask(client, a, "contact_change", {"target": "business", "phone": "(212) 555-0199"})
    client.post(f"/api/maintenance/{r.json()['id']}/decision", json={"decision": "approve"})
    r = ask(client, a, "contact_change", {"person_index": 0, "name": "Jane Q Doe"})
    client.post(f"/api/maintenance/{r.json()['id']}/decision", json={"decision": "approve"})
    after = client.get(f"{URL}/{a['id']}").json()
    assert after["applicant"]["tin"] == "*****4321"
    assert after["applicant"]["individuals"][0]["ssn_last4"] == "4321"
    assert after["applicant"]["contact_phone"] == "(212) 555-0199"
    assert after["applicant"]["individuals"][0]["full_name"] == "Jane Q Doe"
    assert after["business_name"] == "Corporation"  # a person's name never renames the business


def test_target_only_for_combined_customers(client):
    a = approve(client, {**BODY, **SIGNED})
    r = ask(client, a, "contact_change", {"target": "business", "phone": "(212) 555-0199"})
    assert r.status_code == 422


# ---------- exports and markup ----------
def test_export_and_csv_are_clean(client):
    a = make(client, both(individuals=[person("Jane Doe", date_of_birth="1985-07-09", ssn_last4="4455")]))
    export = client.get(f"{URL}/{a['id']}/export").text
    csv_text = client.get("/api/audit.csv").text
    for text in (export, csv_text):
        text = without_random_parts(text)
        assert "1985-07-09" not in text and "4455" not in text
        assert "12-3410000" not in text and "123410000" not in text
    assert "date_of_birth" not in export and "ssn_last4" not in export
    assert "*****0000" in export


def test_start_screen_markup(client):
    html = client.get("/").text
    # one choice at a time: a radio group with three cards
    assert 'role="radiogroup" aria-label="What would you like to open?"' in html
    assert html.count('role="radio" aria-checked="false"') == 3
    assert 'data-start="business"' in html and 'data-start="personal"' in html and 'data-start="both"' in html
    assert 'id="start-continue"' in html and "Continue" in html and " disabled" in html
    assert '"both"' in html and "#both" in html
    assert "Business and personal" in html and "Let’s set up your business and personal accounts" in html
    assert "Business owner: both" in html and "Personal applicants" in html and "Start from an owner" in html


def test_type_switch_keeps_start_screen_and_chosen_accounts_in_sync():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app/static/index.html").read_text()
    # in-form radios update the start screen selection
    assert "startPick = r.value;" in html
    # chosen accounts survive a redraw of the tables
    assert "Object.keys(kept).forEach(id => pickProduct(id, kept[id]))" in html
    # captions are the short names; the instruction lives in the help text
    assert "accounts</caption>" in html and "(sample terms)" not in html
    assert "Tick the box above each account you want." in html
