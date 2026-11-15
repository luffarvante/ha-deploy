"""Tests for the deploy() orchestrator: step order, recovery, and the finally net.

All steps are monkeypatched with recorder fakes; the real git/Supervisor code
is exercised by the other test files.
"""

import deploy
import pytest

ENV_SENTINEL = {"GIT_SSH_COMMAND": "sentinel"}


def gate_result(ok, reason="clean"):
    return ok, reason, {"summary": reason}


def make_steps(**overrides):
    """Build (monkeypatch-factories, recorder) for every deploy() step.

    Defaults are the happy-path results; overrides replace per-test behavior.
    Each fake records its own name (and kwargs for gate_check/pull).
    """
    calls = []

    def gate_check(env):
        calls.append(("gate_check", env))
        override = overrides.get("gate_check", gate_result(True))
        if isinstance(override, list):
            # Per-call results (consumed in order); last one repeats.
            return override.pop(0) if len(override) > 1 else override[0]
        return override

    def create_backup():
        calls.append(("create_backup",))
        return overrides.get(
            "create_backup",
            {"ok": True, "slug": "abc123", "code": 200, "error": None},
        )

    def stop_core():
        calls.append(("stop_core",))
        return overrides.get("stop_core", {"ok": True, "code": 200, "error": None})

    def start_core():
        calls.append(("start_core",))
        return overrides.get("start_core", {"ok": True, "code": 200, "error": None})

    def pull(env):
        calls.append(("pull", env))
        return overrides.get(
            "pull",
            {"ok": True, "rc": 0, "pull_output": "updated", "error": None},
        )

    def verify(env):
        calls.append(("verify", env))
        return overrides.get("verify", {"ok": True, "modified": [], "error": None})

    def derive_git_env():
        return ENV_SENTINEL

    fakes = {
        "gate_check": gate_check,
        "create_backup": create_backup,
        "stop_core": stop_core,
        "start_core": start_core,
        "pull": pull,
        "verify": verify,
        "derive_git_env": derive_git_env,
    }
    return fakes, calls


def install(monkeypatch, **overrides):
    fakes, calls = make_steps(**overrides)
    for name, fake in fakes.items():
        monkeypatch.setattr(deploy, name, fake)
    return calls


def names(calls):
    return [c[0] for c in calls]


def test_happy_path_order_and_single_restart(monkeypatch):
    calls = install(monkeypatch)
    result = deploy.deploy()
    assert result["ok"] is True
    assert result["backup"] == "abc123"
    assert result["pull_output"] == "updated"
    assert names(calls) == [
        "gate_check",
        "create_backup",
        "stop_core",
        "gate_check",
        "pull",
        "verify",
        "start_core",
    ]
    assert names(calls).count("start_core") == 1


def test_env_derived_once_and_passed_to_git_steps(monkeypatch):
    calls = install(monkeypatch)
    result = deploy.deploy()
    assert result["ok"] is True
    gate_envs = [c[1] for c in calls if c[0] == "gate_check"]
    pull_envs = [c[1] for c in calls if c[0] == "pull"]
    verify_envs = [c[1] for c in calls if c[0] == "verify"]
    assert gate_envs == [ENV_SENTINEL, ENV_SENTINEL]
    assert pull_envs == [ENV_SENTINEL]
    assert verify_envs == [ENV_SENTINEL]


def test_gate1_fail_returns_without_any_core_action(monkeypatch):
    calls = install(
        monkeypatch, gate_check=gate_result(False, "modified tracked files present")
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert "modified tracked" in result["error"]
    assert result["status"]["summary"] == "modified tracked files present"
    # No backup, no stop, no start.
    assert names(calls) == ["gate_check"]


def test_backup_fail_returns_without_stopping(monkeypatch):
    calls = install(
        monkeypatch,
        create_backup={
            "ok": False,
            "slug": None,
            "code": 500,
            "error": "backup API returned 500",
        },
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert "backup failed" in result["error"]
    assert names(calls) == ["gate_check", "create_backup"]


def test_stop_fail_returns_without_start(monkeypatch):
    calls = install(
        monkeypatch,
        stop_core={"ok": False, "code": 502, "error": "core stop returned 502"},
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert "failed to stop core" in result["error"]
    assert result["backup"] == "abc123"
    # Do NOT start: the core is presumably still running (decided).
    assert "start_core" not in names(calls)


def test_gate2_fail_starts_core_before_returning(monkeypatch):
    # First gate passes, second fails. Recovery: explicit start + finally start.
    calls = install(
        monkeypatch,
        gate_check=[gate_result(True), gate_result(False, "changed after stop")],
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert "post-stop gate failed" in result["error"]
    assert result["backup"] == "abc123"
    assert names(calls).count("start_core") == 2  # explicit + finally


def test_pull_fail_starts_core_before_returning(monkeypatch):
    calls = install(
        monkeypatch,
        pull={
            "ok": False,
            "rc": 1,
            "pull_output": "CONFLICT",
            "error": "git pull failed",
        },
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert result["error"] == "git pull failed"
    assert result["pull_output"] == "CONFLICT"
    assert names(calls).count("start_core") == 2  # explicit + finally


def test_verify_fail_starts_core_before_returning(monkeypatch):
    calls = install(
        monkeypatch,
        verify={
            "ok": False,
            "modified": [" M x"],
            "error": "unexpected tracked changes",
        },
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert result["error"] == "unexpected tracked changes after pull"
    assert result["modified"] == [" M x"]
    assert names(calls).count("start_core") == 2  # explicit + finally


def test_final_start_fail_returns_error_and_finally_retries(monkeypatch):
    # The last start fails; the finally must still have attempted a restart.
    calls = install(
        monkeypatch,
        start_core={"ok": False, "code": 500, "error": "core start returned 500"},
    )
    result = deploy.deploy()
    assert result["ok"] is False
    assert "failed to start core" in result["error"]
    assert names(calls).count("start_core") == 2


def test_exception_after_stop_still_restarts_in_finally(monkeypatch):
    # The exact gap that broke the old code: a stray exception post-stop must
    # not leave the core stopped.
    calls = install(monkeypatch)

    def exploding_pull(env):
        calls.append(("pull", env))
        raise RuntimeError("stray broken-pipe")

    monkeypatch.setattr(deploy, "pull", exploding_pull)
    with pytest.raises(RuntimeError):
        deploy.deploy()
    assert names(calls).count("start_core") == 1  # from the finally only
