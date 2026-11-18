"""Route tests for server.py (decided, T).

Real ThreadingHTTPServer on an ephemeral localhost port; requests go through
urllib so status codes, headers, and JSON encoding are exercised as served.
/status serves the typed StatusView; /deploy serves DeployOutcome (or
RouteCrash from the last-resort catch) as model_dump_json.
"""

from http.server import ThreadingHTTPServer
import json
import os
import threading
import urllib.error
import urllib.request

import deploy
import git_state
import ha_core
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


# --- /status (typed StatusView) ----------------------------------------------


def test_status_serves_typed_view(api, monkeypatch):
    def fake_run_git(args, env):
        if args[:2] == ["fetch", "origin"]:
            return git_state.RunGitSuccess(stdout="", stderr="")
        return git_state.RunGitSuccess(
            stdout="## main...origin/main\n?? t.txt", stderr=""
        )

    monkeypatch.setattr(git_state, "run_git", fake_run_git)
    status, body = json_request(api, "/status")
    assert status == 200
    assert body["state"] == {
        "kind": "untracked-only",
        "branch": "main...origin/main",
        "changes": [{"status": "??", "path": "t.txt"}],
    }
    assert body["gate"] == {"kind": "gate-pass"}


# --- /deploy wiring + last-resort catch -------------------------------------


def test_deploy_route_success(api, monkeypatch):
    calls = []

    def fake_deploy():
        calls.append(1)
        return deploy.DeploySucceeded(output="updated", backup="s")

    monkeypatch.setattr(deploy, "deploy", fake_deploy)
    status, body = json_request(api, "/deploy", method="POST")
    assert calls == [1]
    assert status == 200
    assert body == {"kind": "deploy-succeeded", "output": "updated", "backup": "s"}


def test_deploy_route_failure_is_500_json(api, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda: deploy.BlockedByBackup(
            reason="backup API returned 500: {'result': 'error'}"
        ),
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {
        "kind": "blocked-by-backup",
        "reason": "backup API returned 500: {'result': 'error'}",
    }


def test_deploy_route_blocked_before_backup_serializes_reason(api, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda: deploy.BlockedBeforeBackup(
            reason="modified tracked files present",
            state=git_state.Clean(branch="main...origin/main"),
        ),
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {
        "kind": "blocked-before-backup",
        "reason": "modified tracked files present",
        "state": {"kind": "clean", "branch": "main...origin/main"},
    }


def test_deploy_route_blocked_after_stop_serializes_restart_failure(api, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda: deploy.BlockedAfterStop(
            reason="changed after stop",
            state=git_state.Clean(branch="main...origin/main"),
            backup="abc123",
            restart=ha_core.CoreStartFailed(reason="start failed"),
        ),
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {
        "kind": "blocked-after-stop",
        "reason": "changed after stop",
        "state": {"kind": "clean", "branch": "main...origin/main"},
        "backup": "abc123",
        "restart": {"kind": "core-start-failed", "reason": "start failed"},
    }


def test_deploy_route_blocked_by_pull_serializes_restart_success(api, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda: deploy.BlockedByPull(
            reason="git pull failed",
            output="CONFLICT (content)",
            backup="abc123",
            restart=ha_core.CoreStarted(),
        ),
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {
        "kind": "blocked-by-pull",
        "reason": "git pull failed",
        "output": "CONFLICT (content)",
        "backup": "abc123",
        "restart": {"kind": "core-started"},
    }


def test_deploy_route_blocked_by_verify_serializes_new_fields(api, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda: deploy.BlockedByVerify(
            reason="unexpected tracked changes after pull",
            output="updated",
            modified=[" M config.py"],
            backup="abc123",
            restart=ha_core.CoreStarted(),
        ),
    )
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body == {
        "kind": "blocked-by-verify",
        "reason": "unexpected tracked changes after pull",
        "output": "updated",
        "modified": [" M config.py"],
        "backup": "abc123",
        "restart": {"kind": "core-started"},
    }


def test_deploy_route_last_resort_catch_guarantees_json(api, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(deploy, "deploy", boom)
    status, body = json_request(api, "/deploy", method="POST")
    assert status == 500
    assert body["kind"] == "route-crash"
    assert "RuntimeError('boom')" in body["reason"]
