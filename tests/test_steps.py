"""Tests for the step functions' error->result mapping (each step catches its own)."""

import deploy

# --- helpers ----------------------------------------------------------------


def fake_supervisor(code, info):
    """Monkeypatch deploy.supervisor_request to return (code, info)."""
    return lambda *a, **k: (code, info)


def raising_supervisor(exc):
    def _raise(*a, **k):
        raise exc

    return _raise


# --- create_backup ----------------------------------------------------------


def test_backup_2xx_with_slug(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "supervisor_request",
        fake_supervisor(200, {"result": "ok", "data": {"slug": "e2b7a9c4"}}),
    )
    b = deploy.create_backup()
    assert b["ok"] is True
    assert b["slug"] == "e2b7a9c4"
    assert b["code"] == 200
    assert b["error"] is None


def test_backup_name_fallback(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "supervisor_request",
        fake_supervisor(200, {"result": "ok", "data": {"name": "pre-deploy"}}),
    )
    b = deploy.create_backup()
    assert b["ok"] is True
    assert b["slug"] == "pre-deploy"


def test_backup_non_2xx_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "supervisor_request",
        fake_supervisor(500, {"result": "error", "message": "busy"}),
    )
    b = deploy.create_backup()
    assert b["ok"] is False
    assert "500" in b["error"]
    assert b["slug"] is None


def test_backup_2xx_body_mismatch_fails(monkeypatch):
    # 2xx-but-unexpected-body -> fail (old code silently continued; do not copy).
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"result": "ok", "data": {}})
    )
    b = deploy.create_backup()
    assert b["ok"] is False
    assert "unexpected body" in b["error"]


def test_backup_body_mismatch_no_data_key(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"weird": 1})
    )
    b = deploy.create_backup()
    assert b["ok"] is False


def test_backup_network_error_fails(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(-1, {"error": "timeout"})
    )
    b = deploy.create_backup()
    assert b["ok"] is False
    assert "-1" in b["error"]


def test_backup_unexpected_exception_is_mapped(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", raising_supervisor(RuntimeError("boom"))
    )
    b = deploy.create_backup()
    assert b["ok"] is False
    assert "boom" in b["error"]


def test_backup_non_dict_json_body_fails(monkeypatch):
    # Valid JSON that is not an object (e.g. an array) must hit the body
    # mismatch, not crash on .get().
    monkeypatch.setattr(deploy, "supervisor_request", fake_supervisor(200, []))
    b = deploy.create_backup()
    assert b["ok"] is False
    assert "unexpected body" in b["error"]


# --- stop_core / start_core -------------------------------------------------


def test_stop_2xx_ok(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"result": "ok", "data": {}})
    )
    s = deploy.stop_core()
    assert s["ok"] is True
    assert s["code"] == 200


def test_stop_non_2xx_fails(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(502, {"result": "error"})
    )
    s = deploy.stop_core()
    assert s["ok"] is False
    assert "502" in s["error"]


def test_stop_exception_mapped(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", raising_supervisor(RuntimeError("x"))
    )
    s = deploy.stop_core()
    assert s["ok"] is False


def test_start_2xx_ok(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"result": "ok", "data": {}})
    )
    s = deploy.start_core()
    assert s["ok"] is True


def test_start_non_2xx_fails(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(500, {"result": "error"})
    )
    s = deploy.start_core()
    assert s["ok"] is False
    assert "500" in s["error"]


def test_start_exception_mapped(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", raising_supervisor(RuntimeError("x"))
    )
    s = deploy.start_core()
    assert s["ok"] is False


# --- pull -------------------------------------------------------------------


def fake_git(args_result):
    """Monkeypatch deploy.run_git: map tuple args-prefix -> (rc, out, err)."""

    def _run_git(args, env=None):
        for prefix, result in args_result.items():
            if tuple(args[: len(prefix)]) == prefix:
                return result
        raise AssertionError(f"unexpected git call: {args}")

    return _run_git


def test_pull_rc0_clean_output_ok(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("pull",): (0, "Already up to date.", "")}),
    )
    p = deploy.pull({"GIT_SSH_COMMAND": "x"})
    assert p["ok"] is True
    assert p["rc"] == 0
    assert p["pull_output"] == "Already up to date."


def test_pull_rc_nonzero_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git(
            {("pull",): (1, "", "error: Your local changes would be overwritten")}
        ),
    )
    p = deploy.pull({})
    assert p["ok"] is False
    assert p["pull_output"] != ""


def test_pull_conflict_in_output_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("pull",): (1, "CONFLICT (content): Merge conflict in f", "")}),
    )
    p = deploy.pull({})
    assert p["ok"] is False


def test_pull_conflict_marker_alone_fails(monkeypatch):
    # Isolates the CONFLICT branch: rc=0 but merge-conflict markers in output.
    # (The rc=1 conflict test short-circuits on rc != 0 and proves nothing
    # about this check on its own.)
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("pull",): (0, "CONFLICT (content): Merge conflict in f", "")}),
    )
    p = deploy.pull({})
    assert p["ok"] is False
    assert p["rc"] == 0


def test_pull_error_marker_in_output_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("pull",): (0, "", "error: something bad")}),
    )
    p = deploy.pull({})
    assert p["ok"] is False


def test_pull_exception_mapped(monkeypatch):
    def _raise(args, env=None):
        raise RuntimeError("pull exploded")

    monkeypatch.setattr(deploy, "run_git", _raise)
    p = deploy.pull({})
    assert p["ok"] is False
    assert "pull exploded" in p["error"]


# --- verify -----------------------------------------------------------------


def test_verify_only_untracked_ok(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("status", "--porcelain"): (0, "?? a.txt\n?? b.txt", "")}),
    )
    v = deploy.verify({})
    assert v["ok"] is True
    assert v["modified"] == []


def test_verify_any_non_untracked_line_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("status", "--porcelain"): (0, "?? a.txt\n M x.txt", "")}),
    )
    v = deploy.verify({})
    assert v["ok"] is False
    assert v["modified"] == [" M x.txt"]


def test_verify_deletion_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("status", "--porcelain"): (0, " D gone.txt", "")}),
    )
    v = deploy.verify({})
    assert v["ok"] is False


def test_verify_status_rc_nonzero_fails(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("status", "--porcelain"): (1, "", "fatal: bad")}),
    )
    v = deploy.verify({})
    assert v["ok"] is False
    assert "fatal" in v["error"]


def test_verify_exception_mapped(monkeypatch):
    def _raise(args, env=None):
        raise RuntimeError("v")

    monkeypatch.setattr(deploy, "run_git", _raise)
    v = deploy.verify({})
    assert v["ok"] is False
    assert "v" in v["error"]
