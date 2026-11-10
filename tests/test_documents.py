"""Document upload: magic-byte checks, limits, safe storage and download."""

import uuid
from pathlib import Path

import pytest

from app import config, db, main
from tests.signing import SIGNED

BODY = {
    "business_name": "Corporation",
    "tin": "12-3410000",
    "address_line1": "223 Grand St.",
    "city": "New York",
    "state": "NY",
    "postal_code": "10013",
    **SIGNED,
}
PDF = b"%PDF-1.4\nhello\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff\xe0" + b"0" * 20


def _app(client):
    return client.post("/api/applications", json=BODY).json()["id"]


def _up(client, app_id, data=PDF, name="a.pdf", ctype="application/pdf", doc_type="ein_letter"):
    return client.post(
        f"/api/applications/{app_id}/documents",
        data={"doc_type": doc_type},
        files={"file": (name, data, ctype)},
    )


def test_upload_list_download(client):
    aid = _app(client)
    r = _up(client, aid, name="EIN letter é.pdf")
    assert r.status_code == 201
    meta = r.json()
    assert meta["content_type"] == "application/pdf" and meta["size"] == len(PDF) and meta["doc_type"] == "ein_letter"
    assert "app_id" not in meta
    listed = client.get(f"/api/applications/{aid}/documents").json()
    assert [d["id"] for d in listed] == [meta["id"]]
    d = client.get(f"/api/applications/{aid}/documents/{meta['id']}")
    assert d.status_code == 200 and d.content == PDF
    assert d.headers["content-type"] == "application/pdf"
    assert d.headers["x-content-type-options"] == "nosniff"
    assert d.headers["content-disposition"] == "attachment; filename*=UTF-8''EIN%20letter%20%C3%A9.pdf"
    # stored under the server-generated id, not the client name
    assert (Path(config.DOCUMENTS_DIR) / meta["id"]).read_bytes() == PDF


def test_png_and_jpeg_accepted_whatever_client_says(client):
    aid = _app(client)
    assert _up(client, aid, PNG, "x.txt", "text/plain").json()["content_type"] == "image/png"
    assert _up(client, aid, JPG, "x.pdf", "application/pdf").json()["content_type"] == "image/jpeg"


def test_wrong_magic_bytes_rejected(client):
    aid = _app(client)
    assert _up(client, aid, b"just text", "evil.pdf", "application/pdf").status_code == 415
    assert _up(client, aid, b"", "empty.pdf").status_code == 415
    assert client.get(f"/api/applications/{aid}/documents").json() == []
    assert list(Path(config.DOCUMENTS_DIR).iterdir()) == []  # no leftovers


def test_bad_doc_type_and_unknown_app(client):
    aid = _app(client)
    assert _up(client, aid, doc_type="passport").status_code == 422
    assert _up(client, str(uuid.uuid4())).status_code == 404
    assert client.get(f"/api/applications/{uuid.uuid4()}/documents").status_code == 404


def test_too_large_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_DOC_BYTES", 100)
    aid = _app(client)
    assert _up(client, aid, PDF + b"0" * 200).status_code == 413
    assert _up(client, aid, PDF).status_code == 201
    assert len(list(Path(config.DOCUMENTS_DIR).iterdir())) == 1


def test_eleventh_document_rejected(client):
    aid = _app(client)
    for _ in range(10):
        assert _up(client, aid).status_code == 201
    assert _up(client, aid).status_code == 409
    assert len(client.get(f"/api/applications/{aid}/documents").json()) == 10
    assert len(list(Path(config.DOCUMENTS_DIR).iterdir())) == 10


def test_document_of_another_application_is_404(client):
    a, b = _app(client), _app(client)
    doc = _up(client, a).json()["id"]
    assert client.get(f"/api/applications/{b}/documents/{doc}").status_code == 404
    assert client.get(f"/api/applications/{a}/documents/{doc}").status_code == 200


def test_non_uuid_doc_id_is_404(client):
    aid = _app(client)
    for bad in ("..%2F..%2Fetc%2Fpasswd", "abc", "%2e%2e"):
        assert client.get(f"/api/applications/{aid}/documents/{bad}").status_code == 404


def test_filename_sanitized(client):
    aid = _app(client)
    assert _up(client, aid, name="../../etc/passwd.pdf").json()["filename"] == "passwd.pdf"
    # the test client percent-encodes control characters on the wire, so check the cleaner directly
    assert main.clean_filename("../../etc/pass\x00wd\r\n\t.pdf") == "passwd.pdf"
    assert main.clean_filename("a\x1b[31m\u202e.pdf") == "a[31m.pdf"
    assert _up(client, aid, name="C:\\Users\\bob\\x.pdf").json()["filename"] == "x.pdf"
    assert _up(client, aid, name="a" * 300 + ".pdf").json()["filename"] == "a" * 120
    assert _up(client, aid, name="..").json()["filename"] == "document"


def test_event_recorded_without_contents(client):
    aid = _app(client)
    _up(client, aid, name="f.pdf", doc_type="formation_document")
    ev = [e for e in db.list_events(aid) if e["kind"] == "document_uploaded"]
    assert len(ev) == 1
    assert ev[0]["detail"] == {"doc_type": "formation_document", "filename": "f.pdf", "size": len(PDF)}


def test_files_stay_inside_documents_dir(client):
    aid = _app(client)
    _up(client, aid, name="../../escape.pdf")
    root = Path(config.DOCUMENTS_DIR).resolve()
    files = [p for p in root.parent.rglob("*") if p.is_file() and p.suffix != ".db" and "-journal" not in p.name]
    assert files and all(root in p.resolve().parents for p in files)


def test_file_removed_if_db_insert_fails(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "add_document", boom)
    aid = _app(client)
    with pytest.raises(RuntimeError):
        _up(client, aid)
    assert list(Path(config.DOCUMENTS_DIR).iterdir()) == []
