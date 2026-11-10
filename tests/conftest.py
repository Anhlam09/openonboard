import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MIDDESK_API_KEY"] = ""  # force mock mode
os.environ["MIDDESK_WEBHOOK_SECRET"] = "sec_test"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DOCUMENTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "MIDDESK_WEBHOOK_SECRET", "sec_test")
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
