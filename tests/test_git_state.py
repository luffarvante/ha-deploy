"""Tests for git state classification (Git states section of the plan)."""

import deploy


def make_run_git(fetch_rc=0, fetch_err="", status_rc=0, status_out=""):
    calls = []

    def _run_git(args, env=None):
        calls.append(list(args))
        if args[:2] == ["fetch", "origin"]:
            return fetch_rc, "", fetch_err
        return status_rc, status_out, ""

    return _run_git, calls


def summarize(monkeypatch, fetch_rc=0, fetch_err="", status_rc=0, status_out=""):
    run_git, calls = make_run_git(fetch_rc, fetch_err, status_rc, status_out)
    monkeypatch.setattr(deploy, "run_git", run_git)
    st = deploy.git_status_summary({"GIT_SSH_COMMAND": "x"})
    return st, calls


def test_both_git_calls_receive_env(monkeypatch):
    _, calls = summarize(monkeypatch)
    assert calls == [["fetch", "origin"], ["status", "--porcelain=v1", "-b"]]
    # env assertion done via run_git kwarg capture below

    captured_envs = []

    def _run_git(args, env=None):
        captured_envs.append(env)
        if args[:2] == ["fetch", "origin"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(deploy, "run_git", _run_git)
    deploy.git_status_summary({"GIT_SSH_COMMAND": "sentinel"})
    assert all(env == {"GIT_SSH_COMMAND": "sentinel"} for env in captured_envs)


def test_clean(monkeypatch):
    st, _ = summarize(monkeypatch, status_out="## main...origin/main")
    assert st["summary"] == "clean"
    assert st["gate_ok"] is True


def test_behind_origin_passes_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_out="## main...origin/main [behind 2]")
    assert st["summary"] == "behind origin"
    assert st["behind"] == 2
    assert st["gate_ok"] is True


def test_ahead_of_origin_fails_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_out="## main...origin/main [ahead 3]")
    assert st["summary"] == "ahead of origin"
    assert st["ahead"] == 3
    assert st["gate_ok"] is False


def test_diverged_fails_gate(monkeypatch):
    st, _ = summarize(
        monkeypatch, status_out="## main...origin/main [behind 1, ahead 1]"
    )
    assert st["summary"] == "diverged"
    assert st["gate_ok"] is False


def test_untracked_only_passes_gate(monkeypatch):
    st, _ = summarize(
        monkeypatch,
        status_out="## main...origin/main\n?? ha-deploy-test.txt\n?? tmp/",
    )
    assert st["summary"] == "untracked only"
    assert st["gate_ok"] is True
    assert len(st["changes"]) == 2


def test_only_additions_fails_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_out="A  newfile.txt")
    assert st["summary"] == "only additions"
    assert st["gate_ok"] is False


def test_only_deletions_fails_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_out="D  oldfile.txt")
    assert st["summary"] == "only deletions"
    assert st["gate_ok"] is False


def test_contains_updates_fails_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_out=" M tracked.txt\n?? other.txt")
    assert st["summary"] == "contains updates"
    assert st["gate_ok"] is False


def test_fetch_auth_error(monkeypatch):
    st, _ = summarize(
        monkeypatch,
        fetch_rc=128,
        fetch_err="git@github.com: Permission denied (publickey).",
    )
    assert st["fetch_ok"] is False
    assert st["summary"] == "auth error"
    assert st["gate_ok"] is False


def test_fetch_generic_error(monkeypatch):
    st, _ = summarize(
        monkeypatch, fetch_rc=128, fetch_err="fatal: Could not resolve host: example"
    )
    assert st["summary"] == "fetch failed"
    assert st["gate_ok"] is False


def test_status_failure_fails_gate(monkeypatch):
    st, _ = summarize(monkeypatch, status_rc=1, status_out="")
    assert st["status_ok"] is False
    assert st["summary"] == "status failed"
    assert st["gate_ok"] is False


def test_gate_check_clean(monkeypatch):
    run_git, _ = make_run_git(status_out="## main...origin/main")
    monkeypatch.setattr(deploy, "run_git", run_git)
    ok, reason, _st = deploy.gate_check({})
    assert ok is True
    assert reason == "clean"


def test_gate_check_ahead_reason(monkeypatch):
    run_git, _ = make_run_git(status_out="## main...origin/main [ahead 1]")
    monkeypatch.setattr(deploy, "run_git", run_git)
    ok, reason, _st = deploy.gate_check({})
    assert ok is False
    assert "ahead" in reason


def test_gate_check_modified_tracked_reason(monkeypatch):
    run_git, _ = make_run_git(status_out=" M tracked.txt")
    monkeypatch.setattr(deploy, "run_git", run_git)
    ok, reason, _st = deploy.gate_check({})
    assert ok is False
    assert "modified tracked" in reason


def test_gate_check_status_failed_reason(monkeypatch):
    run_git, _ = make_run_git(status_rc=1, status_out="")
    monkeypatch.setattr(deploy, "run_git", run_git)
    ok, reason, _st = deploy.gate_check({})
    assert ok is False
    assert "status failed" in reason


def test_gate_check_fetch_failed_reason(monkeypatch):
    run_git, _ = make_run_git(fetch_rc=128, fetch_err="nope")
    monkeypatch.setattr(deploy, "run_git", run_git)
    ok, reason, _st = deploy.gate_check({})
    assert ok is False
    assert reason.startswith("fetch failed")
