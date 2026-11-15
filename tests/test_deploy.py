"""Tests for the deploy() orchestrator: order, recovery, and flow outcomes."""

import deploy
import pytest

ENV_SENTINEL = {"GIT_SSH_COMMAND": "sentinel"}


def install_steps(monkeypatch, counts, **overrides):
    """Patch the six steps + derive_git_env; record call order in counts.

    An override value is a model instance (constant return), a list of model
    instances (sequential returns, last one repeated), or a callable.
    """
    counts["order"] = []
    defaults = {
        "gate_check": lambda env: deploy.GatePass(),
        "create_backup": lambda: deploy.BackupCreated(slug="abc123"),
        "stop_core": lambda: deploy.CoreStopped(),
        "start_core": lambda: deploy.CoreStarted(),
        "pull": lambda env: deploy.Pulled(output="updated"),
        "verify": lambda env: deploy.VerifiedClean(),
    }
    monkeypatch.setattr(deploy, "derive_git_env", lambda: ENV_SENTINEL)
    for name, default in defaults.items():
        seq = overrides.get(name, default)

        if isinstance(seq, list):
            queue = list(seq)

            def fake(*args, _queue=queue, _name=name, **kwargs):
                counts["order"].append(_name)
                return _queue.pop(0)

        elif callable(seq):

            def fake(*args, _f=seq, _name=name, **kwargs):
                counts["order"].append(_name)
                return _f(*args, **kwargs)

        else:

            def fake(*args, _model=seq, _name=name, **kwargs):
                counts["order"].append(_name)
                return _model

        monkeypatch.setattr(deploy, name, fake)


def test_happy_path_step_order_single_restart(monkeypatch):
    counts = {}
    install_steps(monkeypatch, counts)

    result = deploy.deploy()

    assert counts["order"] == [
        "gate_check",
        "create_backup",
        "stop_core",
        "gate_check",
        "pull",
        "verify",
        "start_core",
    ]
    assert result == deploy.DeploySucceeded(output="updated", backup="abc123")


def test_env_flows_to_git_steps(monkeypatch):
    counts = {}
    seen = {}

    def gate(env):
        seen["gate"] = env
        return deploy.GatePass()

    def pull(env):
        seen["pull"] = env
        return deploy.Pulled(output="x")

    def verify(env):
        seen["verify"] = env
        return deploy.VerifiedClean()

    install_steps(monkeypatch, counts, gate_check=gate, pull=pull, verify=verify)

    deploy.deploy()

    assert seen == {"gate": ENV_SENTINEL, "pull": ENV_SENTINEL, "verify": ENV_SENTINEL}


def test_gate1_blocks_before_backup(monkeypatch):
    counts = {}
    blocked = deploy.GateBlocked(
        reason="modified tracked files present - resolve manually first",
        state=deploy.Clean(branch="main...origin/main"),
    )
    install_steps(monkeypatch, counts, gate_check=[blocked])

    result = deploy.deploy()

    assert result == deploy.BlockedBeforeBackup(
        state=deploy.Clean(branch="main...origin/main")
    )
    assert counts["order"] == ["gate_check"]
    assert counts["order"].count("start_core") == 0


def test_backup_failure_returns_without_stopping(monkeypatch):
    counts = {}
    failed = deploy.BackupFailed(reason="backup API returned 500: {'result': 'error'}")
    install_steps(monkeypatch, counts, create_backup=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByBackup(reason=failed.reason)
    assert counts["order"] == ["gate_check", "create_backup"]
    assert counts["order"].count("stop_core") == 0


def test_stop_failure_does_not_restart(monkeypatch):
    counts = {}
    failed = deploy.CoreStopFailed(reason="core stop returned 502: {'result': 'error'}")
    install_steps(monkeypatch, counts, stop_core=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByStop(reason=failed.reason, backup="abc123")
    assert counts["order"].count("start_core") == 0
    assert counts["order"].count("pull") == 0


def test_gate2_failure_restarts_twice(monkeypatch):
    counts = {}
    blocked = deploy.GateBlocked(
        reason="changed after stop", state=deploy.Clean(branch="main...origin/main")
    )
    install_steps(monkeypatch, counts, gate_check=[deploy.GatePass(), blocked])

    result = deploy.deploy()

    assert result == deploy.BlockedAfterStop(
        state=deploy.Clean(branch="main...origin/main"), backup="abc123"
    )
    assert counts["order"].count("start_core") == 2
    assert counts["order"].count("pull") == 0


def test_pull_failure_restarts_twice(monkeypatch):
    counts = {}
    failed = deploy.PullFailed(
        output="CONFLICT (content): Merge conflict in x", reason="git pull failed"
    )
    install_steps(monkeypatch, counts, pull=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByPull(output=failed.output, backup="abc123")
    assert counts["order"].count("start_core") == 2


def test_verify_failure_restarts_twice(monkeypatch):
    counts = {}
    failed = deploy.VerifyFailed(
        modified=[" M config.py"], reason="unexpected tracked changes after pull"
    )
    install_steps(monkeypatch, counts, verify=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByVerify(
        output="updated", modified=[" M config.py"], backup="abc123"
    )
    assert counts["order"].count("start_core") == 2


def test_final_start_failure_returns_but_finally_restarts(monkeypatch):
    counts = {}
    failed = deploy.CoreStartFailed(
        reason="core start returned 500: {'result': 'error'}"
    )
    install_steps(monkeypatch, counts, start_core=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByStart(
        reason=failed.reason, output="updated", backup="abc123"
    )
    assert counts["order"].count("start_core") == 2


def test_exception_after_stop_restarts_in_finally(monkeypatch):
    counts = {}

    def exploding_pull(env):
        raise RuntimeError("boom")

    install_steps(monkeypatch, counts, pull=exploding_pull)

    with pytest.raises(RuntimeError, match="boom"):
        deploy.deploy()

    assert counts["order"].count("start_core") == 1
