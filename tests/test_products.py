"""SAMPLE product catalog, product selection rules, and one simulated account per product."""

from app import core_banking, db, products
from tests.test_owners_kyc import BODY
from tests.test_personal import body as personal_body

URL = "/api/applications"


def biz(*choices):
    return {**BODY, "products": list(choices)}


def ch(pid, cents, **kw):
    return {"product_id": pid, "initial_deposit_cents": cents, **kw}


def _fields(r):
    return [e["msg"] for e in r.json()["detail"]]


def test_catalog_by_type_and_flagged_sample(client):
    allp = client.get("/api/products").json()
    assert len(allp) == 8 and all(p["sample"] is True for p in allp)
    for kind, names in (
        ("personal", ["Everyday Checking", "Interest Checking", "Personal Savings", "Certificate of Deposit"]),
        ("business", ["Business Checking", "Business Interest Checking", "Business Savings", "Business Certificate"]),
    ):
        got = client.get(f"/api/products?account_type={kind}").json()
        assert [p["name"] for p in got] == names
        assert {p["account_type"] for p in got} == {kind}
    certs = [p for p in allp if p["kind"] == "certificate"]
    assert all(p["terms_months"] == [6, 12, 24] for p in certs)
    assert client.get("/api/products?account_type=other").status_code == 422
    assert products.__doc__ and "SAMPLE" in products.__doc__


def test_no_products_rejected(client):
    r = client.post(URL, json={**BODY, "products": []})
    assert r.status_code == 422 and "Choose at least one account." in " ".join(_fields(r))
    no_key = {k: v for k, v in BODY.items() if k != "products"}
    assert client.post(URL, json=no_key).status_code == 422
    assert client.post(URL, json=biz(*[ch("business-checking", 10000)] * 5)).status_code == 422


def test_min_and_max_deposit_enforced(client):
    assert client.post(URL, json=biz(ch("business-checking", 9999))).status_code == 422
    assert client.post(URL, json=biz(ch("business-checking", 10000))).status_code == 201
    assert client.post(URL, json=biz(ch("business-checking", 100_000_001))).status_code == 422
    assert client.post(URL, json=biz(ch("business-checking", 100_000_000))).status_code == 201
    assert client.post(URL, json=biz(ch("nope", 10000))).status_code == 422


def test_term_rules(client):
    assert client.post(URL, json=biz(ch("business-certificate", 250000))).status_code == 422  # term missing
    assert client.post(URL, json=biz(ch("business-certificate", 250000, term_months=7))).status_code == 422
    ok = client.post(URL, json=biz(ch("business-certificate", 250000, term_months=12)))
    assert ok.status_code == 201
    assert ok.json()["applicant"]["products"][0]["term_months"] == 12
    assert client.post(URL, json=biz(ch("business-savings", 25000, term_months=12))).status_code == 422


def test_checking_only_options(client):
    for opt in ("overdraft_protection", "debit_card"):
        assert client.post(URL, json=biz(ch("business-savings", 25000, **{opt: True}))).status_code == 422
        assert (
            client.post(URL, json=biz(ch("business-certificate", 250000, term_months=6, **{opt: True}))).status_code
            == 422
        )
    r = client.post(URL, json=biz(ch("business-checking", 10000, overdraft_protection=True, debit_card=True)))
    assert r.status_code == 201
    p = r.json()["applicant"]["products"][0]
    assert p["overdraft_protection"] is True and p["debit_card"] is True


def test_wrong_type_and_duplicates_rejected(client):
    assert client.post(URL, json=biz(ch("personal-everyday-checking", 2500))).status_code == 422
    r = client.post(URL, json=personal_body(products=[ch("business-checking", 10000)]))
    assert r.status_code == 422
    dup = biz(ch("business-checking", 10000), ch("business-checking", 20000))
    assert client.post(URL, json=dup).status_code == 422


def _approved_two(client):
    body = biz(ch("business-checking", 10000, debit_card=True), ch("business-savings", 30000))
    a = client.post(URL, json=body).json()
    client.get(f"{URL}/{a['id']}?refresh=true")
    r = client.post(f"{URL}/{a['id']}/decision", json={"decision": "approved"})
    return a["id"], r.json()


def test_approval_opens_one_account_per_product(client):
    app_id, r = _approved_two(client)
    accts = r["accounts"]
    assert [a["product_id"] for a in accts] == ["business-checking", "business-savings"]
    assert accts[0]["account_ref"] != accts[1]["account_ref"]
    assert [a["expected_deposit_cents"] for a in accts] == [10000, 30000]
    assert all(a["funding_status"] == "awaiting_deposit" for a in accts)
    assert r["core_account_ref"] == accts[0]["account_ref"] and r["account_last4"] == accts[0]["account_last4"]
    opened = [e for e in db.list_events(app_id) if e["kind"] == "account_opened"]
    assert [e["detail"]["product_id"] for e in opened] == ["business-checking", "business-savings"]


def test_deposit_per_product(client):
    app_id, _ = _approved_two(client)
    url = f"{URL}/{app_id}/simulate-deposit"
    assert client.post(url, json={"amount_cents": 500, "product_id": "business-certificate"}).status_code == 404
    r = client.post(url, json={"amount_cents": 30000, "product_id": "business-savings"}).json()
    by = {a["product_id"]: a for a in r["accounts"]}
    assert (
        by["business-savings"]["funding_status"] == "funded" and by["business-savings"]["funded_amount_cents"] == 30000
    )
    assert by["business-checking"]["funding_status"] == "awaiting_deposit"
    assert r["funding_status"] == "awaiting_deposit"  # legacy columns follow the first account
    r = client.post(url, json={"amount_cents": 10000}).json()  # default: the first account
    assert r["funding_status"] == "funded" and r["funded_amount_cents"] == 10000
    assert client.post(url, json={"amount_cents": 10000}).status_code == 409
    ev = [e["detail"] for e in db.list_events(app_id) if e["kind"] == "deposit_received"]
    assert [d["product_id"] for d in ev] == ["business-savings", "business-checking"]


def test_retry_opens_only_failed_accounts(client, monkeypatch):
    real = core_banking.open_account

    def flaky(row, product_id):
        if product_id == "business-savings":
            raise core_banking.CoreError("down (simulated)")
        return real(row, product_id)

    monkeypatch.setattr(core_banking, "open_account", flaky)
    app_id, r = _approved_two(client)
    assert [a["product_id"] for a in r["accounts"]] == ["business-checking"]
    failed = [e for e in db.list_events(app_id) if e["kind"] == "account_open_failed"]
    assert failed[0]["detail"]["product_id"] == "business-savings"
    monkeypatch.setattr(core_banking, "open_account", real)
    r = client.post(f"{URL}/{app_id}/open-account").json()
    assert [a["product_id"] for a in r["accounts"]] == ["business-checking", "business-savings"]
    assert client.post(f"{URL}/{app_id}/open-account").status_code == 409


def test_personal_products_and_csv_safe(client):
    r = client.post(URL, json=personal_body(products=[ch("personal-certificate", 100000, term_months=24)]))
    assert r.status_code == 201
    assert r.json()["applicant"]["products"][0]["term_months"] == 24
