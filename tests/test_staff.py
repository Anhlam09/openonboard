"""SAMPLE staff directory, in-branch applications, assignees and primary officer."""

from app import db
from tests.test_owners_kyc import BODY

URL = "/api/applications"
BANKER = "staff-maria-lopez"


def make(client, **extra):
    return client.post(URL, json={**BODY, **extra})


def events(client, app_id, kind):
    return [e for e in client.get(f"{URL}/{app_id}/events").json() if e["kind"] == kind]


def test_directory_is_sample(client):
    staff = client.get("/api/staff").json()
    assert 4 <= len(staff) <= 6
    assert all(s["sample"] is True and s["role"] in ("banker", "reviewer", "officer") for s in staff)
    assert all({"id", "name", "email", "role"} <= set(s) for s in staff)


def test_online_default_unchanged(client):
    r = make(client)
    assert r.status_code == 201
    a = r.json()
    assert (a["channel"], a["created_by"], a["primary_officer"], a["assignees"]) == ("online", None, None, [])
    assert events(client, a["id"], "submitted")[0]["detail"].get("channel") is None


def test_online_ignores_created_by(client):
    assert make(client, created_by=BANKER).json()["created_by"] is None


def test_in_branch_requires_created_by(client):
    r = make(client, channel="in_branch")
    assert r.status_code == 422
    assert "Choose the banker helping the customer." in " ".join(e["msg"] for e in r.json()["detail"])


def test_in_branch_ok_and_recorded(client):
    a = make(client, channel="in_branch", created_by=BANKER).json()
    assert (a["channel"], a["created_by"]) == ("in_branch", BANKER)
    d = events(client, a["id"], "submitted")[0]["detail"]
    assert d["channel"] == "in_branch" and d["created_by"] == BANKER
    assert client.get(f"{URL}/{a['id']}").json()["created_by"] == BANKER


def test_invalid_staff_ids(client):
    assert make(client, channel="in_branch", created_by="nobody").status_code == 422
    assert make(client, channel="walk_in", created_by=BANKER).status_code == 422
    a = make(client).json()["id"]
    assert client.post(f"{URL}/{a}/assignees", json={"staff_id": "nobody"}).status_code == 404
    assert client.put(f"{URL}/{a}/primary-officer", json={"staff_id": "nobody"}).status_code == 404
    assert client.delete(f"{URL}/{a}/assignees/nobody").status_code == 404


def test_add_remove_assignees_with_events(client):
    a = make(client).json()["id"]
    r = client.post(f"{URL}/{a}/assignees", json={"staff_id": "staff-priya-patel"})
    assert r.status_code == 200 and r.json()["assignees"] == ["staff-priya-patel"]
    assert events(client, a, "assignee_added")[0]["detail"] == {"staff_id": "staff-priya-patel", "name": "Priya Patel"}
    r = client.delete(f"{URL}/{a}/assignees/staff-priya-patel")
    assert r.status_code == 200 and r.json()["assignees"] == []
    assert events(client, a, "assignee_removed")[0]["detail"] == {
        "staff_id": "staff-priya-patel",
        "name": "Priya Patel",
    }
    assert client.delete(f"{URL}/{a}/assignees/staff-priya-patel").status_code == 404


def test_duplicate_and_max_assignees(client):
    a = make(client).json()["id"]
    ids = [s["id"] for s in client.get("/api/staff").json()]
    assert db.MAX_ASSIGNEES == 5 and len(ids) >= 5
    for i in ids[:5]:
        assert client.post(f"{URL}/{a}/assignees", json={"staff_id": i}).status_code == 200
    assert client.post(f"{URL}/{a}/assignees", json={"staff_id": ids[0]}).status_code == 409
    assert len(events(client, a, "assignee_added")) == 5


def test_max_assignees_with_extra_person(client, monkeypatch):
    from app import staff

    monkeypatch.setattr(
        staff, "STAFF", [*staff.STAFF, {"id": "staff-x", "name": "X Y", "email": "x@example.com", "role": "banker"}]
    )
    a = make(client).json()["id"]
    for s in staff.STAFF[:5]:
        client.post(f"{URL}/{a}/assignees", json={"staff_id": s["id"]})
    r = client.post(f"{URL}/{a}/assignees", json={"staff_id": "staff-x"})
    assert r.status_code == 409 and "at most 5" in r.json()["detail"]
    assert len(events(client, a, "assignee_added")) == 5


def test_primary_officer_set_and_clear(client):
    a = make(client).json()["id"]
    r = client.put(f"{URL}/{a}/primary-officer", json={"staff_id": "staff-grace-kim"})
    assert r.status_code == 200 and r.json()["primary_officer"] == "staff-grace-kim"
    r = client.put(f"{URL}/{a}/primary-officer", json={"staff_id": None})
    assert r.json()["primary_officer"] is None
    ev = events(client, a, "primary_officer_set")
    assert [e["detail"] for e in ev] == [
        {"staff_id": "staff-grace-kim", "name": "Grace Kim"},
        {"staff_id": None, "name": None},
    ]


def test_unknown_application(client):
    assert client.post(f"{URL}/nope/assignees", json={"staff_id": BANKER}).status_code == 404
    assert client.delete(f"{URL}/nope/assignees/{BANKER}").status_code == 404
    assert client.put(f"{URL}/nope/primary-officer", json={"staff_id": None}).status_code == 404


def test_staff_changes_do_not_touch_the_decision(client):
    a = make(client).json()
    client.post(f"{URL}/{a['id']}/assignees", json={"staff_id": BANKER})
    got = client.get(f"{URL}/{a['id']}").json()
    assert got["final_decision"] is None and got["accounts"] == []
