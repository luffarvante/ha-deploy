"""Tests for the deploy() orchestrator: order, recovery, and flow outcomes."""

import deploy
import git_state
import ha_core
import pytest

ENV_SENTINEL = {"GIT_SSH_COMMAND": "sentinel"}
BRANCH = "main...origin/main"


def install_steps(monkeypatch, counts, **overrides):
    """Patch the six steps + derive_git_env; record call order in counts.

    An override value is a model instance (constant return), a list of model
    instances (sequential returns, last one repeated), or a callable.
    """
    counts["order"] = []
    defaults = {
        "gate_check": lambda env: git_state.GatePass(),
        "create_backup": lambda: deploy.BackupCreated(slug="abc123"),
        "stop_core": lambda: ha_core.CoreStopped(),
        "start_core": lambda: ha_core.CoreStarted(),
        "pull": lambda env: deploy.Pulled(output="updated"),
        "verify": lambda env, output: deploy.VerifiedClean(),
    }
    monkeypatch.setattr(git_state, "derive_git_env", lambda: ENV_SENTINEL)
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

        targets = {
            "gate_check": git_state,
            "stop_core": ha_core,
            "start_core": ha_core,
        }
        target = targets.get(name, deploy)
        monkeypatch.setattr(target, name, fake)


def install_start(monkeypatch, outcomes):
    """Patch ha_core.start_core with a sequential outcome list."""
    queue = list(outcomes)
    calls = []

    def fake():
        calls.append(1)
        return queue.pop(0)

    monkeypatch.setattr(ha_core, "start_core", fake)
    return calls


# --- flow order and early returns --------------------------------------------


def test_happy_path_step_order_single_start(monkeypatch):
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
        return git_state.GatePass()

    def pull(env):
        seen["pull"] = env
        return deploy.Pulled(output="x")

    def verify(env, output):
        seen["verify"] = env
        return deploy.VerifiedClean()

    install_steps(monkeypatch, counts, gate_check=gate, pull=pull, verify=verify)

    deploy.deploy()

    assert seen == {"gate": ENV_SENTINEL, "pull": ENV_SENTINEL, "verify": ENV_SENTINEL}


def test_gate1_blocks_before_backup(monkeypatch):
    counts = {}
    blocked = git_state.GateBlocked(
        reason="modified tracked files present - resolve manually first",
        state=git_state.Clean(branch=BRANCH),
    )
    install_steps(monkeypatch, counts, gate_check=[blocked])

    result = deploy.deploy()

    assert result == deploy.BlockedBeforeBackup(
        reason=blocked.reason, state=git_state.Clean(branch=BRANCH)
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
    failed = ha_core.CoreStopFailed(
        reason="core stop returned 502: {'result': 'error'}"
    )
    install_steps(monkeypatch, counts, stop_core=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByStop(reason=failed.reason, backup="abc123")
    assert counts["order"].count("start_core") == 0
    assert counts["order"].count("pull") == 0


# --- blocked exits restart exactly once --------------------------------------


def test_gate2_failure_restarts_once(monkeypatch):
    counts = {}
    blocked = git_state.GateBlocked(
        reason="changed after stop", state=git_state.Clean(branch=BRANCH)
    )
    install_steps(monkeypatch, counts, gate_check=[git_state.GatePass(), blocked])

    result = deploy.deploy()

    assert result == deploy.BlockedAfterStop(
        reason=blocked.reason,
        state=git_state.Clean(branch=BRANCH),
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )
    assert counts["order"].count("start_core") == 1
    assert counts["order"].count("pull") == 0


def test_pull_failure_restarts_once(monkeypatch):
    counts = {}
    failed = deploy.PullFailed(
        output="CONFLICT (content): Merge conflict in x", reason="git pull failed"
    )
    install_steps(monkeypatch, counts, pull=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByPull(
        reason=failed.reason,
        output=failed.output,
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )
    assert counts["order"].count("start_core") == 1


def test_verify_failure_restarts_once(monkeypatch):
    counts = {}
    failed = deploy.VerifyFailed(
        output="updated",
        modified=[" M config.py"],
        reason="unexpected tracked changes after pull",
    )
    install_steps(monkeypatch, counts, verify=failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByVerify(
        reason=failed.reason,
        output="updated",
        modified=[" M config.py"],
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )
    assert counts["order"].count("start_core") == 1


def test_blocked_exit_restart_failure_is_recorded(monkeypatch):
    counts = {}
    failed = deploy.PullFailed(output="oops", reason="git pull failed")
    restart_failed = ha_core.CoreStartFailed(reason="restart failed")
    install_steps(monkeypatch, counts, pull=failed, start_core=restart_failed)

    result = deploy.deploy()

    assert result == deploy.BlockedByPull(
        reason=failed.reason,
        output=failed.output,
        backup="abc123",
        restart=restart_failed,
    )
    assert counts["order"].count("start_core") == 1


# --- final start: one explicit retry ----------------------------------------


def test_final_start_succeeds_without_retry(monkeypatch):
    counts = {}
    install_steps(monkeypatch, counts)

    deploy.deploy()

    assert counts["order"].count("start_core") == 1


def test_final_start_failure_retries_and_succeeds(monkeypatch):
    counts = {}
    first = ha_core.CoreStartFailed(reason="core start returned 500")
    install_steps(monkeypatch, counts, start_core=[first, ha_core.CoreStarted()])

    result = deploy.deploy()

    assert result == deploy.DeploySucceeded(output="updated", backup="abc123")
    assert counts["order"].count("start_core") == 2


def test_final_start_failure_twice_returns_blocked(monkeypatch):
    counts = {}
    first = ha_core.CoreStartFailed(reason="core start returned 500")
    second = ha_core.CoreStartFailed(reason="core start returned 502")
    install_steps(monkeypatch, counts, start_core=[first, second])

    result = deploy.deploy()

    assert result == deploy.BlockedByStart(
        reason=second.reason, output="updated", backup="abc123"
    )
    assert counts["order"].count("start_core") == 2


def test_start_with_retry_first_success(monkeypatch):
    calls = install_start(monkeypatch, [ha_core.CoreStarted()])

    assert deploy._start_with_retry() == ha_core.CoreStarted()
    assert len(calls) == 1


def test_start_with_retry_retry_succeeds(monkeypatch):
    first = ha_core.CoreStartFailed(reason="first")
    calls = install_start(monkeypatch, [first, ha_core.CoreStarted()])

    assert deploy._start_with_retry() == ha_core.CoreStarted()
    assert len(calls) == 2


def test_start_with_retry_both_fail(monkeypatch):
    first = ha_core.CoreStartFailed(reason="first")
    second = ha_core.CoreStartFailed(reason="second")
    calls = install_start(monkeypatch, [first, second])

    assert deploy._start_with_retry() == second
    assert len(calls) == 2


# --- pure composition --------------------------------------------------------


def test_finish_success_started():
    outcome = deploy._finish_success("abc123", "updated", ha_core.CoreStarted())
    assert outcome == deploy.DeploySucceeded(output="updated", backup="abc123")


def test_finish_success_failed():
    failed = ha_core.CoreStartFailed(reason="nope")
    outcome = deploy._finish_success("abc123", "updated", failed)
    assert outcome == deploy.BlockedByStart(
        reason="nope", output="updated", backup="abc123"
    )


def test_finish_blocked_gate_started():
    body = git_state.GateBlocked(reason="blocked", state=git_state.Clean(branch=BRANCH))
    outcome = deploy._finish_blocked("abc123", body, ha_core.CoreStarted())
    assert outcome == deploy.BlockedAfterStop(
        reason="blocked",
        state=git_state.Clean(branch=BRANCH),
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )


def test_finish_blocked_gate_restart_failed():
    body = git_state.GateBlocked(reason="blocked", state=git_state.Clean(branch=BRANCH))
    failed = ha_core.CoreStartFailed(reason="restart failed")
    outcome = deploy._finish_blocked("abc123", body, failed)
    assert outcome == deploy.BlockedAfterStop(
        reason="blocked",
        state=git_state.Clean(branch=BRANCH),
        backup="abc123",
        restart=failed,
    )


def test_finish_blocked_pull_started():
    body = deploy.PullFailed(output="pull out", reason="pull failed")
    outcome = deploy._finish_blocked("abc123", body, ha_core.CoreStarted())
    assert outcome == deploy.BlockedByPull(
        reason="pull failed",
        output="pull out",
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )


def test_finish_blocked_pull_restart_failed():
    body = deploy.PullFailed(output="pull out", reason="pull failed")
    failed = ha_core.CoreStartFailed(reason="restart failed")
    outcome = deploy._finish_blocked("abc123", body, failed)
    assert outcome == deploy.BlockedByPull(
        reason="pull failed",
        output="pull out",
        backup="abc123",
        restart=failed,
    )


def test_finish_blocked_verify_started():
    body = deploy.VerifyFailed(
        output="pull out", modified=[" M x"], reason="verify failed"
    )
    outcome = deploy._finish_blocked("abc123", body, ha_core.CoreStarted())
    assert outcome == deploy.BlockedByVerify(
        reason="verify failed",
        output="pull out",
        modified=[" M x"],
        backup="abc123",
        restart=ha_core.CoreStarted(),
    )


def test_finish_blocked_verify_restart_failed():
    body = deploy.VerifyFailed(
        output="pull out", modified=[" M x"], reason="verify failed"
    )
    failed = ha_core.CoreStartFailed(reason="restart failed")
    outcome = deploy._finish_blocked("abc123", body, failed)
    assert outcome == deploy.BlockedByVerify(
        reason="verify failed",
        output="pull out",
        modified=[" M x"],
        backup="abc123",
        restart=failed,
    )


# --- window steps ------------------------------------------------------------


def test_window_steps_gate_blocked(monkeypatch):
    counts = {}
    blocked = git_state.GateBlocked(
        reason="blocked", state=git_state.Clean(branch=BRANCH)
    )
    install_steps(monkeypatch, counts, gate_check=[blocked])

    assert deploy._window_steps(ENV_SENTINEL) == blocked
    assert counts["order"] == ["gate_check"]


def test_window_steps_pull_failed(monkeypatch):
    counts = {}
    failed = deploy.PullFailed(output="pull out", reason="pull failed")
    install_steps(monkeypatch, counts, pull=failed)

    assert deploy._window_steps(ENV_SENTINEL) == failed
    assert counts["order"] == ["gate_check", "pull"]


def test_window_steps_verify_failed(monkeypatch):
    counts = {}
    failed = deploy.VerifyFailed(
        output="updated", modified=[" M x"], reason="verify failed"
    )
    install_steps(monkeypatch, counts, verify=failed)

    assert deploy._window_steps(ENV_SENTINEL) == failed
    assert counts["order"] == ["gate_check", "pull", "verify"]


def test_window_steps_success(monkeypatch):
    counts = {}
    install_steps(monkeypatch, counts)

    assert deploy._window_steps(ENV_SENTINEL) == deploy.WindowSucceeded(
        output="updated"
    )
    assert counts["order"] == ["gate_check", "pull", "verify"]


# --- exception path ----------------------------------------------------------


def test_exception_after_stop_restarts_once(monkeypatch):
    counts = {}

    def exploding_pull(env):
        raise RuntimeError("boom")

    install_steps(monkeypatch, counts, pull=exploding_pull)

    with pytest.raises(RuntimeError, match="boom"):
        deploy.deploy()

    assert counts["order"].count("start_core") == 1


# --- presentation ------------------------------------------------------------


def test_describe_exact_strings():
    started = ha_core.CoreStarted()
    failed = ha_core.CoreStartFailed(reason="nope")

    assert (
        deploy.describe(deploy.DeploySucceeded(output="updated", backup="abc123"))
        == "ok"
    )
    assert (
        deploy.describe(
            deploy.BlockedBeforeBackup(
                reason="gate failed", state=git_state.Clean(branch=BRANCH)
            )
        )
        == "blocked before backup: gate failed"
    )
    assert deploy.describe(deploy.BlockedByBackup(reason="backup failed")) == (
        "backup failed: backup failed"
    )
    assert deploy.describe(deploy.BlockedByStop(reason="stop failed", backup="b")) == (
        "core stop failed: stop failed; no restart attempted"
    )
    assert (
        deploy.describe(
            deploy.BlockedAfterStop(
                reason="gate failed",
                state=git_state.Clean(branch=BRANCH),
                backup="b",
                restart=started,
            )
        )
        == "gate failed; core restarted"
    )
    assert (
        deploy.describe(
            deploy.BlockedAfterStop(
                reason="gate failed",
                state=git_state.Clean(branch=BRANCH),
                backup="b",
                restart=failed,
            )
        )
        == "gate failed; core restart failed: nope"
    )
    assert (
        deploy.describe(
            deploy.BlockedByPull(
                reason="pull failed", output="o", backup="b", restart=started
            )
        )
        == "pull failed: pull failed; core restarted"
    )
    assert (
        deploy.describe(
            deploy.BlockedByVerify(
                reason="verify failed",
                output="o",
                modified=[],
                backup="b",
                restart=failed,
            )
        )
        == "verify failed: verify failed; core restart failed: nope"
    )
    assert deploy.describe(
        deploy.BlockedByStart(reason="start failed", output="o", backup="b")
    ) == ("core start failed: start failed")
