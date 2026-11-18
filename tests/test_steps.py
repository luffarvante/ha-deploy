"""Tests for the deploy steps: backup/stop/start/pull/verify outcome mapping."""

import deploy
import git_state
import supervisor


def fake_supervisor(outcome):
    def handler(method, path, body, timeout):
        return outcome

    return handler


def fake_run_git(outcome):
    def handler(args, env):
        return outcome

    return handler


# --- create_backup ------------------------------------------------------------


def test_backup_success_slug(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(
            supervisor.SupervisorOk(
                code=200, body={"result": "ok", "data": {"slug": "e2b7a9c4"}}
            )
        ),
    )
    outcome = deploy.create_backup()
    assert outcome == deploy.BackupCreated(slug="e2b7a9c4")


def test_backup_name_fallback_kept(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(
            supervisor.SupervisorOk(
                code=200, body={"result": "ok", "data": {"name": "pre-deploy"}}
            )
        ),
    )
    outcome = deploy.create_backup()
    assert outcome == deploy.BackupCreated(slug="pre-deploy")


def test_backup_api_error(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorError(code=500, body={"result": "error"})),
    )
    outcome = deploy.create_backup()
    assert isinstance(outcome, deploy.BackupFailed)
    assert "500" in outcome.reason


def test_backup_body_without_slug(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(
            supervisor.SupervisorOk(code=200, body={"result": "ok", "data": {}})
        ),
    )
    outcome = deploy.create_backup()
    assert isinstance(outcome, deploy.BackupFailed)
    assert "unexpected body" in outcome.reason


def test_backup_non_dict_body(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorOk(code=200, body=[])),
    )
    outcome = deploy.create_backup()
    assert isinstance(outcome, deploy.BackupFailed)
    assert "unexpected body" in outcome.reason


def test_backup_non_json_body(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorBodyMismatch(code=502, raw="oops")),
    )
    outcome = deploy.create_backup()
    assert isinstance(outcome, deploy.BackupFailed)
    assert "non-JSON" in outcome.reason


def test_backup_unreachable(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorUnreachable(reason="timeout")),
    )
    outcome = deploy.create_backup()
    assert isinstance(outcome, deploy.BackupFailed)
    assert "unreachable" in outcome.reason


def test_backup_request_posts_backup_name(monkeypatch):
    captured = {}

    def handler(method, path, body, timeout):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        captured["timeout"] = timeout
        return supervisor.SupervisorError(code=500, body={"result": "error"})

    monkeypatch.setattr(supervisor, "supervisor_request", handler)
    deploy.create_backup()
    assert captured["method"] == "POST"
    assert captured["path"] == "/backups/new/full"
    assert captured["body"] == {"name": "pre-deploy"}
    assert captured["timeout"] == 300


# --- stop / start -------------------------------------------------------------


def test_stop_ok(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorOk(code=200, body={"result": "ok"})),
    )
    assert deploy.stop_core() == deploy.CoreStopped()


def test_stop_api_error(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorError(code=502, body={"result": "error"})),
    )
    outcome = deploy.stop_core()
    assert isinstance(outcome, deploy.CoreStopFailed)
    assert "502" in outcome.reason


def test_stop_2xx_unexpected_body_fails(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorBodyMismatch(code=200, raw="weird")),
    )
    outcome = deploy.stop_core()
    assert isinstance(outcome, deploy.CoreStopFailed)
    assert "non-JSON" in outcome.reason


def test_stop_unreachable(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorUnreachable(reason="timeout")),
    )
    outcome = deploy.stop_core()
    assert isinstance(outcome, deploy.CoreStopFailed)
    assert "unreachable" in outcome.reason


def test_start_ok(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorOk(code=200, body={"result": "ok"})),
    )
    assert deploy.start_core() == deploy.CoreStarted()


def test_start_api_error(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorError(code=500, body={"result": "error"})),
    )
    outcome = deploy.start_core()
    assert isinstance(outcome, deploy.CoreStartFailed)
    assert "500" in outcome.reason


def test_start_unreachable(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorUnreachable(reason="timeout")),
    )
    outcome = deploy.start_core()
    assert isinstance(outcome, deploy.CoreStartFailed)
    assert "unreachable" in outcome.reason


# --- pull ---------------------------------------------------------------------


def test_pull_clean_output(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitSuccess(stdout="Already up to date.", stderr="")),
    )
    assert deploy.pull({}) == deploy.Pulled(output="Already up to date.")


def test_pull_success_with_conflict_marker_fails(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(
            git_state.RunGitSuccess(
                stdout="CONFLICT (content): Merge conflict in x", stderr=""
            )
        ),
    )
    outcome = deploy.pull({})
    assert isinstance(outcome, deploy.PullFailed)
    assert outcome.reason == "git pull failed"
    assert "CONFLICT" in outcome.output


def test_pull_success_with_error_marker_fails(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(
            git_state.RunGitSuccess(stdout="ok", stderr="error: something bad")
        ),
    )
    outcome = deploy.pull({})
    assert isinstance(outcome, deploy.PullFailed)
    assert "error: something bad" in outcome.output


def test_pull_failure_combines_streams(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitFailure(stdout="o", stderr="e")),
    )
    outcome = deploy.pull({})
    assert isinstance(outcome, deploy.PullFailed)
    assert outcome.output == "o\ne"


def test_pull_exception_empty_output(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitException(reason="pull exploded")),
    )
    outcome = deploy.pull({})
    assert isinstance(outcome, deploy.PullFailed)
    assert outcome.output == ""
    assert "pull exploded" in outcome.reason


# --- verify -------------------------------------------------------------------


def test_verify_only_untracked_is_clean(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitSuccess(stdout="?? new.txt\n", stderr="")),
    )
    assert deploy.verify({}) == deploy.VerifiedClean()


def test_verify_tracked_change_fails(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(
            git_state.RunGitSuccess(stdout=" M mod.txt\n?? new.txt\n", stderr="")
        ),
    )
    outcome = deploy.verify({})
    assert isinstance(outcome, deploy.VerifyFailed)
    assert outcome.modified == [" M mod.txt"]
    assert "unexpected tracked changes" in outcome.reason


def test_verify_deletion_fails(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitSuccess(stdout="D  gone.txt", stderr="")),
    )
    outcome = deploy.verify({})
    assert isinstance(outcome, deploy.VerifyFailed)
    assert outcome.modified == ["D  gone.txt"]


def test_verify_status_failure(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitFailure(stdout="", stderr="fatal: bad object")),
    )
    outcome = deploy.verify({})
    assert isinstance(outcome, deploy.VerifyFailed)
    assert outcome.modified == []
    assert "fatal" in outcome.reason


def test_verify_exception(monkeypatch):
    monkeypatch.setattr(
        git_state,
        "run_git",
        fake_run_git(git_state.RunGitException(reason="status exploded")),
    )
    outcome = deploy.verify({})
    assert isinstance(outcome, deploy.VerifyFailed)
    assert "status exploded" in outcome.reason
