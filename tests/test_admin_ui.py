"""The admin page is served only locally, with strict headers, and only the known files."""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import ui

LOCAL = {"host": "localhost:8000"}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ui.router)
    # TestClient's peer address is "testclient"; pretend it is loopback for the page tests
    monkeypatch.setattr("app.admin.api.LOOPBACK", {"127.0.0.1", "::1", "localhost", "testclient"})
    return TestClient(app, base_url="http://localhost:8000")


def test_page_and_assets_served_with_security_headers(client):
    for path, kind in [("/admin", "text/html"), ("/admin/", "text/html"),
                       ("/admin/static/admin.js", "javascript"), ("/admin/static/admin.css", "text/css")]:
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"


def test_only_allowlisted_files(client):
    for name in ("../api.py", "..%2fapi.py", "index.html", "secrets.txt", "admin.js.map"):
        assert client.get(f"/admin/static/{name}").status_code == 404, name


def test_refused_through_proxy_or_tunnel(client):
    assert client.get("/admin", headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 403
    assert client.get("/admin/static/admin.js", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    assert client.get("/admin", headers={"host": "abc.trycloudflare.com"}).status_code == 403


def test_page_shell_has_no_inline_script_or_style():
    html = (ui.ROOT / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html and "style=" not in html


def test_javascript_never_uses_innerhtml():
    js = (Path(ui.ROOT) / "admin.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", js)
    assert "style." not in js and ".style" not in js           # CSP forbids inline styles


def test_settings_screen_reuses_the_generic_spec_form():
    js = (Path(ui.ROOT) / "admin.js").read_text(encoding="utf-8")
    assert '["settings", "Settings"]' in js and "viewSettings" in js and '"/settings"' in js
    # one renderer for every spec-driven screen: channels and settings both build their form with it
    assert js.count("specForm(") >= 3 and "function specField(" in js and 'f.kind === "number"' in js


def test_providers_screen_and_alert_banners_exist():
    js = (Path(ui.ROOT) / "admin.js").read_text(encoding="utf-8")
    assert '["providers", "Providers"]' in js and "viewProviders" in js and "loadAlerts" in js
    for path in ('"/providers"', '"/providers/approval"', '"/model-settings"', '"/me/alerts"'):
        assert path in js
    assert "LLM provider" not in js            # no longer shown as 'upcoming'
