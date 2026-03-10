from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from web.app import app


def test_health_endpoint_ok():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_auth_redirect_for_protected_page(monkeypatch):
    monkeypatch.setenv("CONTENTPIPE_AUTH_TOKEN", "secret-token")
    client = TestClient(app)
    resp = client.get("/runs", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers["location"]


def test_auth_header_allows_protected_api(monkeypatch):
    monkeypatch.setenv("CONTENTPIPE_AUTH_TOKEN", "secret-token")
    client = TestClient(app)
    resp = client.get("/api/runs", headers={"X-ContentPipe-Token": "secret-token"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
