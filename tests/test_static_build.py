"""The static GitHub Pages demo: the in_browser flag and the site build."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _build_module():
    spec = importlib.util.spec_from_file_location("build_static", ROOT / "scripts" / "build_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_reports_in_browser(client, monkeypatch):
    from app import config

    assert client.get("/api/health").json()["in_browser"] is False
    monkeypatch.setattr(config, "IN_BROWSER", True)
    assert client.get("/api/health").json()["in_browser"] is True


def test_build_writes_page_python_and_manifest(tmp_path):
    out = _build_module().build(tmp_path / "site")
    page = (out / "index.html").read_text()
    assert page.index('<script src="static-shim.js"></script>') < page.index("<script>")  # API ready before page code
    assert (out / "static-shim.js").exists() and (out / ".nojekyll").exists()

    manifest = json.loads((out / "py" / "manifest.json").read_text())
    assert "app/main.py" in manifest["files"] and "bridge.py" in manifest["files"]
    assert "app/check_middesk.py" not in manifest["files"]
    for f in manifest["files"]:
        assert (out / "py" / f).exists()


def test_browser_bridge_forces_mock_mode():
    bridge = (ROOT / "web" / "bridge.py").read_text()
    assert 'MIDDESK_API_KEY=""' in bridge and 'IN_BROWSER="true"' in bridge
