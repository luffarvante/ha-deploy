"""Tests for git_status_summary / gate / status_view: states are the variants."""

import deploy

ENV = {"GIT_SSH_COMMAND": "sentinel"}
BRANCH = "main...origin/main"


def make_run_git(fetch_outcome, status_outcome):
    calls = []

    def _run_git(args, env):
        calls.append((list(args), env))
        if args[:2] == ["fetch", "origin"]:
            return fetch_outcome
        return status_outcome

    return _run_git, calls


def summarize(monkeypatch, fetch_outcome=None, status_outcome=None):
    fetch = (
        fetch_outcome
        if fetch_outcome is not None
        else deploy.RunGitSuccess(stdout="", stderr="")
    )
    status = (
        status_outcome
        if status_outcome is not None
        else deploy.RunGitSuccess(stdout="", stderr="")
    )
    run_git, calls = make_run_git(fetch, status)
    monkeypatch.setattr(deploy, "run_git", run_git)
    state = deploy.git_status_summary(ENV)
    return state, calls


# --- classification ----------------------------------------------------------


def test_clean(monkeypatch):
    state, calls = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    assert state == deploy.Clean(branch=BRANCH)
    assert calls == [
        (["fetch", "origin"], ENV),
        (["status", "--porcelain=v1", "-b"], ENV),
    ]


def test_behind_origin(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH} [behind 2]", stderr=""
        ),
    )
    assert state == deploy.BehindOrigin(branch=f"{BRANCH} [behind 2]", behind=2)


def test_ahead_of_origin(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout=f"## {BRANCH} [ahead 3]", stderr=""),
    )
    assert state == deploy.AheadOfOrigin(branch=f"{BRANCH} [ahead 3]", ahead=3)


def test_diverged(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH} [ahead 1, behind 2]", stderr=""
        ),
    )
    assert state == deploy.Diverged(
        branch=f"{BRANCH} [ahead 1, behind 2]", ahead=1, behind=2
    )


def test_untracked_only_keeps_changes(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH}\n?? new.txt\n?? dir/other.txt", stderr=""
        ),
    )
    assert state == deploy.UntrackedOnly(
        branch=BRANCH,
        changes=[
            deploy.Change(status="??", path="new.txt"),
            deploy.Change(status="??", path="dir/other.txt"),
        ],
    )


def test_only_additions(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH}\nA  added.txt\nAM mixed.txt", stderr=""
        ),
    )
    assert state == deploy.OnlyAdditions(
        branch=BRANCH,
        changes=[
            deploy.Change(status="A", path="added.txt"),
            deploy.Change(status="AM", path="mixed.txt"),
        ],
    )


def test_only_deletions(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH}\nD  gone.txt\nAD staged-deleted", stderr=""
        ),
    )
    assert state == deploy.OnlyDeletions(
        branch=BRANCH,
        changes=[
            deploy.Change(status="D", path="gone.txt"),
            deploy.Change(status="AD", path="staged-deleted"),
        ],
    )


def test_contains_updates(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(
            stdout=f"## {BRANCH}\n M mod.txt\n?? new", stderr=""
        ),
    )
    assert state == deploy.ContainsUpdates(
        branch=BRANCH,
        changes=[
            deploy.Change(status="M", path="mod.txt"),
            deploy.Change(status="??", path="new"),
        ],
    )


def test_fetch_failure_has_no_branch(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        fetch_outcome=deploy.RunGitFailure(stdout="", stderr="connection timed out"),
    )
    assert state == deploy.FetchFailed(reason="connection timed out")


def test_fetch_auth_error(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        fetch_outcome=deploy.RunGitFailure(
            stdout="", stderr="Permission denied (publickey)"
        ),
    )
    assert state == deploy.AuthError(reason="Permission denied (publickey)")


def test_fetch_exception_classified_like_failure_text(monkeypatch):
    state, _ = summarize(
        monkeypatch, fetch_outcome=deploy.RunGitException(reason="git gone")
    )
    assert state == deploy.FetchFailed(reason="git gone")


def test_status_failure(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitFailure(
            stdout="", stderr="fatal: not a git repository"
        ),
    )
    assert state == deploy.StatusFailed(reason="fatal: not a git repository")


def test_status_exception(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitException(reason="git status exploded"),
    )
    assert state == deploy.StatusFailed(reason="git status exploded")


# pinned: a missing ## line is a status failure, never a silent empty branch
def test_missing_branch_line_is_status_failed(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout="?? untracked-only.txt", stderr=""),
    )
    assert state == deploy.StatusFailed(reason="no branch line in status output")


# pinned: upstream-gone line currently classifies as clean with no ahead/behind
def test_upstream_gone_currently_classified_clean(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout=f"## {BRANCH} [gone]", stderr=""),
    )
    assert state == deploy.Clean(branch=f"{BRANCH} [gone]")


# --- gate --------------------------------------------------------------------


def test_gate_passes_on_clean(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    verdict = deploy.gate_verdict(state)
    assert verdict == deploy.GatePass()


def test_gate_passes_on_untracked_only(monkeypatch):
    verdict = deploy.gate_verdict(
        deploy.UntrackedOnly(
            branch=BRANCH, changes=[deploy.Change(status="??", path="x")]
        )
    )
    assert verdict == deploy.GatePass()


def test_gate_passes_on_behind_origin(monkeypatch):
    verdict = deploy.gate_verdict(deploy.BehindOrigin(branch=BRANCH, behind=2))
    assert verdict == deploy.GatePass()


def test_gate_blocks_ahead_with_reason(monkeypatch):
    verdict = deploy.gate_verdict(deploy.AheadOfOrigin(branch=BRANCH, ahead=1))
    assert isinstance(verdict, deploy.GateBlocked)
    assert "unpushed" in verdict.reason


def test_gate_blocks_diverged_with_ahead_reason(monkeypatch):
    verdict = deploy.gate_verdict(deploy.Diverged(branch=BRANCH, ahead=1, behind=1))
    assert isinstance(verdict, deploy.GateBlocked)
    assert "unpushed" in verdict.reason


def test_gate_blocks_modified_tracked_files(monkeypatch):
    verdict = deploy.gate_verdict(
        deploy.ContainsUpdates(
            branch=BRANCH, changes=[deploy.Change(status="M", path="mod.txt")]
        )
    )
    assert isinstance(verdict, deploy.GateBlocked)
    assert verdict.reason == "modified tracked files present - resolve manually first"


def test_gate_blocks_only_additions(monkeypatch):
    verdict = deploy.gate_verdict(
        deploy.OnlyAdditions(
            branch=BRANCH, changes=[deploy.Change(status="A", path="added.txt")]
        )
    )
    assert isinstance(verdict, deploy.GateBlocked)
    assert "modified tracked files" in verdict.reason


def test_gate_blocks_fetch_failure(monkeypatch):
    verdict = deploy.gate_verdict(deploy.FetchFailed(reason="no route to host"))
    assert isinstance(verdict, deploy.GateBlocked)
    assert verdict.reason == "fetch failed: no route to host"


def test_gate_blocks_auth_error(monkeypatch):
    verdict = deploy.gate_verdict(
        deploy.AuthError(reason="Permission denied (publickey)")
    )
    assert isinstance(verdict, deploy.GateBlocked)
    assert verdict.reason == "fetch failed: Permission denied (publickey)"


def test_gate_blocks_status_failure(monkeypatch):
    verdict = deploy.gate_verdict(
        deploy.StatusFailed(reason="fatal: not a git repository")
    )
    assert isinstance(verdict, deploy.GateBlocked)
    assert verdict.reason == "status failed: fatal: not a git repository"


# --- status view -------------------------------------------------------------


def test_status_view_carries_state_and_gate(monkeypatch):
    _state, _ = summarize(
        monkeypatch,
        status_outcome=deploy.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    view = deploy.status_view(ENV)
    assert view.state == deploy.Clean(branch=BRANCH)
    assert view.gate == deploy.GatePass()


def test_status_view_gate_blocked_keeps_state(monkeypatch):
    run_git, _ = make_run_git(
        deploy.RunGitSuccess(stdout="", stderr=""),
        deploy.RunGitFailure(stdout="", stderr="fatal: bad object"),
    )
    monkeypatch.setattr(deploy, "run_git", run_git)
    view = deploy.status_view(ENV)
    assert view.state == deploy.StatusFailed(reason="fatal: bad object")
    assert isinstance(view.gate, deploy.GateBlocked)
    assert "status failed" in view.gate.reason
