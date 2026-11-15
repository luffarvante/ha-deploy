"""Route tests for server.py's live parts (decided, T).

Real ThreadingHTTPServer on an ephemeral localhost port; requests go through
urllib so status codes, headers, and JSON encoding are exercised as served.
The old v2b code inside server.py is deliberately left untested (removed at
the deferred cleanup). /status still calls the legacy git copy; this test is
re-pointed when the route switches to deploy.py.
"""

from http.server import ThreadingHTTPServer
import json
import os
import threading
import urllib.error
import urllib.request

import deploy
import pytest
import server


@pytest.fixture
def api():
    """The app's HTTP server on an ephemeral port; yields the base URL."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(base, path, method="GET"):
    """Returns (status, body_bytes, content_type); non-2xx comes back as data."""
    req = urllib.request.Request(base + path, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as err:
        resp = err
    with resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type")


def json_request(base, path, method="GET"):
    status, body, _ = request(base, path, method)
    return status, json.loads(body.decode())


# --- plain routes -----------------------------------------------------------


def test_health(api):
    status, body, ctype = request(api, "/health")
    assert status == 200
    assert body == b"ok"
    assert ctype == "text/plain"


def test_index_serves_index_html(api):
    status, body, ctype = request(api, "/")
    assert status == 200
    assert body.decode() == server.INDEX_HTML
    assert ctype == "text/html; charset=utf-8"


def test_debug_shape(api):
    status, body, _ = request(api, "/debug")
    info = json.loads(body.decode())
    assert status == 200
    assert info["homeassistant_exists"] == os.path.exists(server.GIT_DIR)
    assert info["ssh_key_exists"] == os.path.exists(server.SSH_KEY)
    assert isinstance(info["root"], list)
    if info["homeassistant_exists"]:
        assert isinstance(info["homeassistant_contents"], list)


def test_get_unknown_route_404(api):
    status, _, _ = request(api, "/nope")
    assert status == 404


def test_post_unknown_route_404(api):
    status, _, _ = request(api, "/nope", method="POST")
    assert status == 404


# --- /create ----------------------------------------------------------------


def test_create_not_mounted(api, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "GIT_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(server, "TEST_FILE", str(tmp_path / "missing" / "x.txt"))
    status, body = json_request(api, "/create", method="POST")
    assert status == 200
    assert body["ok"] is False
    assert "not mounted" in body["error"]


def test_create_writes_test_file(api, monkeypatch, tmp_path):
    test_file = tmp_path / "ha-deploy-test.txt"
    monkeypatch.setattr(server, "GIT_DIR", str(tmp_path))
    monkeypatch.setattr(server, "TEST_FILE", str(test_file))
    status, body = json_request(api, "/create", method="POST")
    assert status == 200
    assert body == {"ok": True, "path": str(test_file)}
    assert test_file.read_text() == "OHAI\n"


# --- /status (legacy copy; re-point when the route switches to deploy.py) ----


def test_status_uses_legacy_copy(api, monkeypatch):
    def fake_run_git(args, env=None):
        if args[:2] == ["fetch", "origin"]:
            return 0, "", ""
        return 0, "## main...origin/main\n?? t.txt", ""

    monkeypatch.setattr(server, "run_git", fake_run_git)
    status, body = json_request(api, "/status")
    assert status == 200
    assert body["summary"] == "untracked only"
    assert body["gate_ok"] is True
    assert "status_ok" not in body  # legacy copy contract, see comment above


# --- /deploy wiring + last-resort catch -------------------------------------


def test_deploy_route_success(api, monkeypatch):
    calls = []

    def fake_deploy():
        calls.append(1)
        return {"ok": True, "pull_output": "updated", "backup": "s", "started": True}

    monkeypatch.setattr(deploy, "deploy", fake_deploy)
    status, body = json_request(api, "/deploy", method="POST")
    assert calls == [1]
    assert status == 200
    assert body == {
        "ok": True,
        "pull_output": "updated",
        "backup": "s",
        "started": True,
    }


def test_deploy_route_failure_is_500_json(api, monkeypatch):
    monkeypatch.setattr(
        deploy, "deploy", lambda: {"ok": False, "error": "gate failed: x"}
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {"ok": False, "error": "gate failed: x"}


def test_deploy_route_last_resort_catch_guarantees_json(api, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(deploy, "deploy", boom)
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body["ok"] is False
    assert "RuntimeError('boom')" in body["error"]
