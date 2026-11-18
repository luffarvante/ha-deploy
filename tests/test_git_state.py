"""Tests for git_status_summary / gate / status_view: states are the variants."""

import git_state

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
        else git_state.RunGitSuccess(stdout="", stderr="")
    )
    status = (
        status_outcome
        if status_outcome is not None
        else git_state.RunGitSuccess(stdout="", stderr="")
    )
    run_git, calls = make_run_git(fetch, status)
    monkeypatch.setattr(git_state, "run_git", run_git)
    state = git_state.git_status_summary(ENV)
    return state, calls


# --- classification ----------------------------------------------------------


def test_clean(monkeypatch):
    state, calls = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    assert state == git_state.Clean(branch=BRANCH)
    assert calls == [
        (["fetch", "origin"], ENV),
        (["status", "--porcelain=v1", "-b"], ENV),
    ]


def test_behind_origin(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH} [behind 2]", stderr=""
        ),
    )
    assert state == git_state.BehindOrigin(branch=f"{BRANCH} [behind 2]", behind=2)


def test_ahead_of_origin(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH} [ahead 3]", stderr=""
        ),
    )
    assert state == git_state.AheadOfOrigin(branch=f"{BRANCH} [ahead 3]", ahead=3)


def test_diverged(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH} [ahead 1, behind 2]", stderr=""
        ),
    )
    assert state == git_state.Diverged(
        branch=f"{BRANCH} [ahead 1, behind 2]", ahead=1, behind=2
    )


def test_untracked_only_keeps_changes(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH}\n?? new.txt\n?? dir/other.txt", stderr=""
        ),
    )
    assert state == git_state.UntrackedOnly(
        branch=BRANCH,
        changes=[
            git_state.Change(status="??", path="new.txt"),
            git_state.Change(status="??", path="dir/other.txt"),
        ],
    )


def test_only_additions(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH}\nA  added.txt\nAM mixed.txt", stderr=""
        ),
    )
    assert state == git_state.OnlyAdditions(
        branch=BRANCH,
        changes=[
            git_state.Change(status="A", path="added.txt"),
            git_state.Change(status="AM", path="mixed.txt"),
        ],
    )


def test_only_deletions(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH}\nD  gone.txt\nAD staged-deleted", stderr=""
        ),
    )
    assert state == git_state.OnlyDeletions(
        branch=BRANCH,
        changes=[
            git_state.Change(status="D", path="gone.txt"),
            git_state.Change(status="AD", path="staged-deleted"),
        ],
    )


def test_contains_updates(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout=f"## {BRANCH}\n M mod.txt\n?? new", stderr=""
        ),
    )
    assert state == git_state.ContainsUpdates(
        branch=BRANCH,
        changes=[
            git_state.Change(status="M", path="mod.txt"),
            git_state.Change(status="??", path="new"),
        ],
    )


def test_fetch_failure_has_no_branch(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        fetch_outcome=git_state.RunGitFailure(stdout="", stderr="connection timed out"),
    )
    assert state == git_state.FetchFailed(reason="connection timed out")


def test_fetch_auth_error(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        fetch_outcome=git_state.RunGitFailure(
            stdout="", stderr="Permission denied (publickey)"
        ),
    )
    assert state == git_state.AuthError(reason="Permission denied (publickey)")


def test_fetch_exception_classified_like_failure_text(monkeypatch):
    state, _ = summarize(
        monkeypatch, fetch_outcome=git_state.RunGitException(reason="git gone")
    )
    assert state == git_state.FetchFailed(reason="git gone")


def test_status_failure(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitFailure(
            stdout="", stderr="fatal: not a git repository"
        ),
    )
    assert state == git_state.StatusFailed(reason="fatal: not a git repository")


def test_status_exception(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitException(reason="git status exploded"),
    )
    assert state == git_state.StatusFailed(reason="git status exploded")


# pinned: a missing ## line is a status failure, never a silent empty branch
def test_missing_branch_line_is_status_failed(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(
            stdout="?? untracked-only.txt", stderr=""
        ),
    )
    assert state == git_state.StatusFailed(reason="no branch line in status output")


# pinned: upstream-gone line currently classifies as clean with no ahead/behind
def test_upstream_gone_currently_classified_clean(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(stdout=f"## {BRANCH} [gone]", stderr=""),
    )
    assert state == git_state.Clean(branch=f"{BRANCH} [gone]")


# --- gate --------------------------------------------------------------------


def test_gate_passes_on_clean(monkeypatch):
    state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    verdict = git_state.gate_verdict(state)
    assert verdict == git_state.GatePass()


def test_gate_passes_on_untracked_only(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.UntrackedOnly(
            branch=BRANCH, changes=[git_state.Change(status="??", path="x")]
        )
    )
    assert verdict == git_state.GatePass()


def test_gate_passes_on_behind_origin(monkeypatch):
    verdict = git_state.gate_verdict(git_state.BehindOrigin(branch=BRANCH, behind=2))
    assert verdict == git_state.GatePass()


def test_gate_blocks_ahead_with_reason(monkeypatch):
    verdict = git_state.gate_verdict(git_state.AheadOfOrigin(branch=BRANCH, ahead=1))
    assert isinstance(verdict, git_state.GateBlocked)
    assert "unpushed" in verdict.reason


def test_gate_blocks_diverged_with_ahead_reason(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.Diverged(branch=BRANCH, ahead=1, behind=1)
    )
    assert isinstance(verdict, git_state.GateBlocked)
    assert "unpushed" in verdict.reason


def test_gate_blocks_modified_tracked_files(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.ContainsUpdates(
            branch=BRANCH, changes=[git_state.Change(status="M", path="mod.txt")]
        )
    )
    assert isinstance(verdict, git_state.GateBlocked)
    assert verdict.reason == "modified tracked files present - resolve manually first"


def test_gate_blocks_only_additions(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.OnlyAdditions(
            branch=BRANCH, changes=[git_state.Change(status="A", path="added.txt")]
        )
    )
    assert isinstance(verdict, git_state.GateBlocked)
    assert "modified tracked files" in verdict.reason


def test_gate_blocks_fetch_failure(monkeypatch):
    verdict = git_state.gate_verdict(git_state.FetchFailed(reason="no route to host"))
    assert isinstance(verdict, git_state.GateBlocked)
    assert verdict.reason == "fetch failed: no route to host"


def test_gate_blocks_auth_error(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.AuthError(reason="Permission denied (publickey)")
    )
    assert isinstance(verdict, git_state.GateBlocked)
    assert verdict.reason == "fetch failed: Permission denied (publickey)"


def test_gate_blocks_status_failure(monkeypatch):
    verdict = git_state.gate_verdict(
        git_state.StatusFailed(reason="fatal: not a git repository")
    )
    assert isinstance(verdict, git_state.GateBlocked)
    assert verdict.reason == "status failed: fatal: not a git repository"


# --- status view -------------------------------------------------------------


def test_status_view_carries_state_and_gate(monkeypatch):
    _state, _ = summarize(
        monkeypatch,
        status_outcome=git_state.RunGitSuccess(stdout=f"## {BRANCH}", stderr=""),
    )
    view = git_state.status_view(ENV)
    assert view.state == git_state.Clean(branch=BRANCH)
    assert view.gate == git_state.GatePass()


def test_status_view_gate_blocked_keeps_state(monkeypatch):
    run_git, _ = make_run_git(
        git_state.RunGitSuccess(stdout="", stderr=""),
        git_state.RunGitFailure(stdout="", stderr="fatal: bad object"),
    )
    monkeypatch.setattr(git_state, "run_git", run_git)
    view = git_state.status_view(ENV)
    assert view.state == git_state.StatusFailed(reason="fatal: bad object")
    assert isinstance(view.gate, git_state.GateBlocked)
    assert "status failed" in view.gate.reason
