"""Tests for run_git (rc/exception mapping) and derive_git_env (silent fallback)."""

import deploy


def test_run_git_success_returns_stripped_tuple(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = " out \n"
        stderr = " err \n"

    def fake_run(args, capture_output, text, env, timeout):
        captured["args"] = args
        captured["env"] = env
        return Result()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    rc, out, err = deploy.run_git(["pull"], env={"GIT_SSH_COMMAND": "ssh-cmd"})
    assert rc == 0
    assert out == "out"
    assert err == "err"
    assert captured["args"] == ["git", "-C", "/homeassistant", "pull"]
    assert captured["env"]["GIT_SSH_COMMAND"] == "ssh-cmd"


def test_run_git_without_env_uses_process_env(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, capture_output, text, env, timeout):
        captured["env"] = env
        return Result()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    deploy.run_git(["status"], env=None)
    assert captured["env"] is not None


def test_run_git_exception_mapped_to_rc_minus_one(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("git gone")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    rc, out, err = deploy.run_git(["pull"])
    assert rc == -1
    assert out == ""
    assert "git gone" in err


def test_derive_git_env_with_key_present(monkeypatch):
    monkeypatch.setattr(deploy.os.path, "exists", lambda path: True)
    env = deploy.derive_git_env()
    assert env["GIT_SSH_COMMAND"] == (
        "ssh -i /data/id_ed25519 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    )


def test_derive_git_env_missing_key_falls_back_to_empty(monkeypatch):
    # DECIDED (D3): silent fallback -> fetch fails and surfaces as auth error.
    monkeypatch.setattr(deploy.os.path, "exists", lambda path: False)
    assert deploy.derive_git_env() == {}
