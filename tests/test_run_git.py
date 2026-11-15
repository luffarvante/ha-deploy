"""Tests for run_git (outcome mapping) and derive_git_env (silent fallback)."""

import deploy


def test_run_git_success_carries_stripped_streams(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = " out \n"
        stderr = " hint \n"

    def fake_run(args, capture_output, text, env, timeout):
        captured["args"] = args
        captured["env"] = env
        return Result()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    outcome = deploy.run_git(["pull"], env={"GIT_SSH_COMMAND": "ssh-cmd"})
    assert isinstance(outcome, deploy.RunGitSuccess)
    assert outcome.stdout == "out"
    assert outcome.stderr == "hint"
    assert captured["args"] == ["git", "-C", "/homeassistant", "pull"]
    assert captured["env"]["GIT_SSH_COMMAND"] == "ssh-cmd"


def test_run_git_failure_carries_streams(monkeypatch):
    class Result:
        returncode = 1
        stdout = " o \n"
        stderr = " e \n"

    def fake_run(args, capture_output, text, env, timeout):
        return Result()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    outcome = deploy.run_git(["pull"], env={})
    assert isinstance(outcome, deploy.RunGitFailure)
    assert outcome.stdout == "o"
    assert outcome.stderr == "e"


def test_run_git_exception_mapped_to_variant(monkeypatch):
    def fake_run(args, capture_output, text, env, timeout):
        raise OSError("git gone")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    outcome = deploy.run_git(["pull"], env={})
    assert isinstance(outcome, deploy.RunGitException)
    assert "git gone" in outcome.reason


def test_derive_git_env_with_key_present(monkeypatch):
    monkeypatch.setattr(deploy.os.path, "exists", lambda path: True)
    env = deploy.derive_git_env()
    assert env["GIT_SSH_COMMAND"] == (
        "ssh -i /data/id_ed25519 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    )


def test_derive_git_env_missing_key_is_silent_fallback(monkeypatch):
    monkeypatch.setattr(deploy.os.path, "exists", lambda path: False)
    assert deploy.derive_git_env() == {}
