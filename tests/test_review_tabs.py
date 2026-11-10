"""Review screen tabs (markup) and the application JSON export."""

import json
from pathlib import Path

from tests.signing import without_random_parts
from tests.test_documents import PDF, _up
from tests.test_owners_kyc import BODY
from tests.test_personal import body, person


def test_markup_has_review_tabs(client):
    html = client.get("/").text
    assert 'role="tablist"' in html and 'role="tabpanel"' in html and 'role="tab"' in html
    for name in ("Overview", "Forms", "Verifications", "Signatures", "Documents", "Accounts", "Funding", "Decision"):
        assert f'"{name}"' in html or f">{name}<" in html or f"{name}${{" in html
    for marker in ("aria-controls", "tabindex", "ArrowRight", "ArrowLeft", "Home", "End", "Export application (JSON)"):
        assert marker in html


def test_export_business(client):
    a = client.post("/api/applications", json=BODY).json()
    assert _up(client, a["id"], PDF).status_code == 201
    r = client.get(f"/api/applications/{a['id']}/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    data = r.json()
    assert data["application"]["id"] == a["id"] and data["events"] and len(data["documents"]) == 1
    assert "hello" not in r.text and "12-3410000" not in r.text and "123410000" not in r.text
    assert "*****0000" in r.text


def test_export_personal_has_no_dob_or_ssn(client):
    a = client.post("/api/applications", json=body(person())).json()
    r = client.get(f"/api/applications/{a['id']}/export")
    assert r.status_code == 200
    text = without_random_parts(r.text)
    assert "1990-04-12" not in text and "6789" not in text
    assert "date_of_birth" not in text and "ssn" not in text.lower()
    assert json.loads(text)["application"]["applicant"]["individuals"][0]["full_name"] == "Ava Stone"


def test_export_unknown_is_404(client):
    assert client.get("/api/applications/nope/export").status_code == 404


def test_tab_markup_has_valid_aria_and_keeps_focus():
    html = (Path(__file__).resolve().parent.parent / "app/static/index.html").read_text()
    assert 'aria-controls="rpanel"' in html and 'id="rpanel"' in html
    assert "rpanel-" not in html
    assert "b.onclick = () => goTab(b.dataset.tab)" in html


def test_hidden_attribute_always_hides_and_headline_switches(client):
    """Elements with display rules (like the 'What we check' lists) must still honor [hidden]."""
    html = client.get("/").text
    assert "[hidden] { display: none !important; }" in html
    assert 'id="apply-h1"' in html and "Personal banking, verified in minutes" in html


def test_start_screen_offers_business_and_personal(client):
    html = client.get("/").text
    assert 'data-start="business"' in html and "Business accounts" in html
    assert 'data-start="personal"' in html and "Personal accounts" in html
    assert 'id="apply-grid" hidden' in html  # the form waits until a choice is made
    assert 'id="change-type"' in html and "startFromLink" in html  # /#business and /#personal links
    assert "c.dataset.for !== t" in html  # samples match the chosen type
