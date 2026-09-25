"""The header's row of links to the owner's other apps (workbench#100).

Pinned here: /api/session says where a browser opens spendglass; the probe
of the other apps leaves out any that don't answer, only talks to this
machine, and asks at most once a minute; the row shows loopback links on
this machine and only tailnet links anywhere else; and the route that
hands the row to the page needs a session.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from spendglass import app_links
from spendglass.auth import Auth
from spendglass.config import DEFAULT_SIBLING_APPS, Config
from spendglass.ui import PAGE, PAGE_VIZ, create_app

TAILNET = "my-mac.my-tailnet.ts.net"
SECRET = "test-recovery-secret"
SIBLINGS = [
    {"name": "crossband", "origin": f"https://{TAILNET}", "local": "http://127.0.0.1:8902"},
    {"name": "membro", "origin": f"https://{TAILNET}:8443", "local": "http://127.0.0.1:8901"},
    {"name": "threadfold", "origin": "", "local": "http://127.0.0.1:8904"},
]


def _app(tmp_path, siblings=None, fetch=None):
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret=SECRET)
    app = create_app(tmp_path / "store.db", auth, sibling_apps=siblings)
    if fetch is not None:
        app.state.sibling_probe = app_links.SiblingProbe(siblings, fetch=fetch)
    return app


# ---- spendglass's own address, on its health route ----

def test_session_route_says_where_spendglass_opens(tmp_path):
    s = TestClient(_app(tmp_path), base_url="http://127.0.0.1:8903").get("/api/session")
    assert s.status_code == 200
    assert s.json()["app"] == "spendglass"
    assert s.json()["browser_origin"] == "http://127.0.0.1:8903"


# ---- config ----

def test_siblings_default_to_the_fleet_and_env_can_empty_them(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.delenv("SPENDGLASS_SIBLING_APPS", raising=False)
    assert Config.load(env).sibling_apps == DEFAULT_SIBLING_APPS
    monkeypatch.setenv("SPENDGLASS_SIBLING_APPS", "{}")
    assert Config.load(env).sibling_apps == {}
    monkeypatch.setenv("SPENDGLASS_SIBLING_APPS", "not json")
    assert Config.load(env).sibling_apps == DEFAULT_SIBLING_APPS


# ---- the probe ----

class _Health(BaseHTTPRequestHandler):
    status = 200
    body = {"ok": True, "browser_origin": f"https://{TAILNET}"}

    def do_GET(self):
        raw = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class _Broken(_Health):
    status = 500


@pytest.fixture
def serve():
    servers = []

    def start(handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for srv in servers:
        srv.shutdown()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_siblings_that_dont_answer_are_left_out(serve):
    up = serve(_Health)
    broken = serve(_Broken)
    probe = app_links.SiblingProbe({
        "crossband": up + "/api/auth/session",
        "membro": f"http://127.0.0.1:{_closed_port()}/v1/health",
        "threadfold": broken + "/health",
    })
    assert probe.found() == [{"name": "crossband", "origin": f"https://{TAILNET}",
                              "local": up}]


def test_answers_are_kept_for_a_minute():
    calls, now = [], [1000.0]

    def fetch(url, timeout):
        calls.append(url)
        return {"browser_origin": ""}
    probe = app_links.SiblingProbe({"membro": DEFAULT_SIBLING_APPS["membro"]},
                                   fetch=fetch, clock=lambda: now[0])
    probe.found()
    now[0] += 59
    probe.found()
    assert len(calls) == 1
    now[0] += 2
    probe.found()
    assert len(calls) == 2


def test_a_slow_sibling_is_left_out_within_the_timeout():
    def fetch(url, timeout):
        time.sleep(2)
        return {"browser_origin": ""}
    probe = app_links.SiblingProbe({"membro": DEFAULT_SIBLING_APPS["membro"]},
                                   fetch=fetch, timeout=0.2)
    started = time.monotonic()
    assert probe.found() == []
    assert time.monotonic() - started < 1.5


def test_only_loopback_addresses_are_ever_probed():
    calls = []
    probe = app_links.SiblingProbe(
        {"elsewhere": "http://192.0.2.10:8901/v1/health",
         "tailnet": f"https://{TAILNET}:8443/v1/health"},
        fetch=lambda url, timeout: calls.append(url))
    assert probe.found() == []
    assert calls == []


def test_a_reported_address_that_isnt_a_plain_origin_is_dropped():
    probe = app_links.SiblingProbe(
        {"membro": DEFAULT_SIBLING_APPS["membro"]},
        fetch=lambda url, timeout: {"browser_origin": "javascript:alert(1)"})
    assert probe.found()[0]["origin"] == ""


# ---- which links a page shows ----

def test_on_this_machine_every_sibling_opens_at_its_loopback_address():
    assert app_links.link_row("127.0.0.1", "spendglass", SIBLINGS) == [
        {"name": "crossband", "href": "http://127.0.0.1:8902/", "current": False},
        {"name": "membro", "href": "http://127.0.0.1:8901/", "current": False},
        {"name": "spendglass", "href": "", "current": True},
        {"name": "threadfold", "href": "http://127.0.0.1:8904/", "current": False},
    ]


def test_a_localhost_page_keeps_the_name_for_its_passkeys():
    row = app_links.link_row("localhost", "spendglass", SIBLINGS)
    assert row[0]["href"] == "http://localhost:8902/"


def test_anywhere_else_only_siblings_served_on_the_tailnet_show():
    assert [r["href"] for r in app_links.link_row(TAILNET, "spendglass", SIBLINGS)] \
        == [f"https://{TAILNET}/", f"https://{TAILNET}:8443/", ""]


def test_no_sibling_that_can_open_means_no_row():
    assert app_links.link_row(TAILNET, "spendglass", SIBLINGS[2:]) == []
    assert app_links.link_row("127.0.0.1", "spendglass", []) == []


# ---- the route the page reads ----

def test_route_needs_a_session(tmp_path):
    app = _app(tmp_path, {"membro": DEFAULT_SIBLING_APPS["membro"]},
               fetch=lambda url, timeout: {"browser_origin": ""})
    client = TestClient(app, base_url="http://127.0.0.1:8903")
    assert client.get("/api/app-links").status_code == 401
    assert client.post("/api/setup", json={
        "recovery_secret": SECRET, "password": "correct-horse-battery"}).status_code == 200
    assert client.get("/api/app-links").json() == {"links": [
        {"name": "membro", "href": "http://127.0.0.1:8901/", "current": False},
        {"name": "spendglass", "href": "", "current": True},
    ]}


def test_create_app_alone_asks_no_live_service(tmp_path):
    """Tests build the app directly, so it must stay inert: no siblings
    unless the caller passes some."""
    assert _app(tmp_path).state.sibling_probe.siblings == {}


def test_both_pages_draw_the_row():
    for page in (PAGE, PAGE_VIZ):
        assert 'id="app-links"' in page
        assert 'fetch("/api/app-links")' in page
        assert "/*app-links-" not in page  # every marker was filled
