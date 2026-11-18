"""Git execution, status classification, and the deploy gate.

This module owns the git subprocess boundary, GitState variants, gate rules,
and the status view. It knows nothing about Supervisor or core control.
"""

import os
import re
import subprocess
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from typing_extensions import assert_never

GIT_DIR = "/homeassistant"
SSH_KEY = "/data/id_ed25519"

# --- git subprocess outcome --------------------------------------------------


class RunGitSuccess(BaseModel):
    """git ran with exit code 0; stderr can still carry progress/hints."""

    kind: Literal["success"] = "success"
    stdout: str
    stderr: str


class RunGitFailure(BaseModel):
    """git ran and exited non-zero."""

    kind: Literal["failure"] = "failure"
    stdout: str
    stderr: str


class RunGitException(BaseModel):
    """git could not run at all (binary gone, timeout, ...)."""

    kind: Literal["exception"] = "exception"
    reason: str


RunGitOutcome = Annotated[
    RunGitSuccess | RunGitFailure | RunGitException,
    Field(discriminator="kind"),
]


def run_git(args: list[str], env: dict[str, str]) -> RunGitOutcome:
    """Run git in GIT_DIR. Returns an outcome; never raises."""
    e = dict(os.environ)
    e.update(env)
    try:
        r = subprocess.run(
            ["git", "-C", GIT_DIR, *args],
            capture_output=True,
            text=True,
            env=e,
            timeout=60,
        )
    except Exception as ex:
        return RunGitException(reason=str(ex))
    if r.returncode == 0:
        return RunGitSuccess(stdout=r.stdout.strip(), stderr=r.stderr.strip())
    return RunGitFailure(stdout=r.stdout.strip(), stderr=r.stderr.strip())


def derive_git_env() -> dict[str, str]:
    """GIT_SSH_COMMAND env for all git calls in this deploy, derived once.

    SSH key missing -> silent fallback to {} (decided, D3): the fetch then
    fails and surfaces as an ordinary fetch/auth error.
    """
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    return {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}


# --- git state: the classifications are the variants -------------------------


class Change(BaseModel):
    """One line of `git status --porcelain` output."""

    status: str
    path: str


class Clean(BaseModel):
    """no local changes, in sync with origin."""

    kind: Literal["clean"] = "clean"
    branch: str


class UntrackedOnly(BaseModel):
    """only untracked files; deploys are allowed."""

    kind: Literal["untracked-only"] = "untracked-only"
    branch: str
    changes: list[Change]


class OnlyAdditions(BaseModel):
    """only staged/unstaged additions; deploy blocked."""

    kind: Literal["only-additions"] = "only-additions"
    branch: str
    changes: list[Change]


class OnlyDeletions(BaseModel):
    """only deletions; deploy blocked."""

    kind: Literal["only-deletions"] = "only-deletions"
    branch: str
    changes: list[Change]


class ContainsUpdates(BaseModel):
    """mixed modifications; deploy blocked."""

    kind: Literal["contains-updates"] = "contains-updates"
    branch: str
    changes: list[Change]


class Diverged(BaseModel):
    """local and origin have diverged; deploy blocked."""

    kind: Literal["diverged"] = "diverged"
    branch: str
    ahead: int
    behind: int


class BehindOrigin(BaseModel):
    """origin is ahead; deploy allowed (pull brings it home)."""

    kind: Literal["behind-origin"] = "behind-origin"
    branch: str
    behind: int


class AheadOfOrigin(BaseModel):
    """local is ahead (unpushed commits); deploy blocked."""

    kind: Literal["ahead-of-origin"] = "ahead-of-origin"
    branch: str
    ahead: int


class FetchFailed(BaseModel):
    """git fetch failed for a non-auth reason."""

    kind: Literal["fetch-failed"] = "fetch-failed"
    reason: str


class AuthError(BaseModel):
    """git fetch failed on authentication."""

    kind: Literal["auth-error"] = "auth-error"
    reason: str


class StatusFailed(BaseModel):
    """git status itself failed."""

    kind: Literal["status-failed"] = "status-failed"
    reason: str


GitState = Annotated[
    Clean
    | UntrackedOnly
    | OnlyAdditions
    | OnlyDeletions
    | ContainsUpdates
    | Diverged
    | BehindOrigin
    | AheadOfOrigin
    | FetchFailed
    | AuthError
    | StatusFailed,
    Field(discriminator="kind"),
]


def _classify_fetch(stderr: str) -> FetchFailed | AuthError:
    low = stderr.lower()
    if (
        "permission" in low
        or "auth" in low
        or "host key" in low
        or "could not read" in low
    ):
        return AuthError(reason=stderr)
    return FetchFailed(reason=stderr)


def _classify_status(status_out: str) -> GitState:
    """Classify porcelain -b output; the branch comes from the ## line.

    A missing ## line (unreachable in practice) is a status failure, never a
    silent empty branch.
    """
    branch: str | None = None
    ahead = 0
    behind = 0
    changes: list[Change] = []
    for line in status_out.splitlines():
        if line.startswith("##"):
            branch = line[2:].strip()
            m = re.search(r"ahead (\d+)", line)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", line)
            if m:
                behind = int(m.group(1))
        elif line.strip():
            changes.append(Change(status=line[:2].strip(), path=line[3:].strip()))

    if branch is None:
        return StatusFailed(reason="no branch line in status output")

    if ahead > 0 and behind > 0:
        return Diverged(branch=branch, ahead=ahead, behind=behind)
    if behind > 0:
        return BehindOrigin(branch=branch, behind=behind)
    if ahead > 0:
        return AheadOfOrigin(branch=branch, ahead=ahead)
    if not changes:
        return Clean(branch=branch)

    statuses = {c.status for c in changes}
    if all(s == "??" for s in statuses):
        return UntrackedOnly(branch=branch, changes=changes)
    if all(s in ("A", "AM", "??") for s in statuses):
        return OnlyAdditions(branch=branch, changes=changes)
    if all(s in ("D", "AD") for s in statuses):
        return OnlyDeletions(branch=branch, changes=changes)
    return ContainsUpdates(branch=branch, changes=changes)


def git_status_summary(env: dict[str, str]) -> GitState:
    """Git fetch origin + git status --porcelain=v1 -b; classifies the state."""
    match run_git(["fetch", "origin"], env):
        case RunGitSuccess():
            match run_git(["status", "--porcelain=v1", "-b"], env):
                case RunGitSuccess(stdout=stdout):
                    return _classify_status(stdout)
                case RunGitFailure(stdout=_, stderr=stderr):
                    return StatusFailed(reason=stderr)
                case RunGitException(reason=reason):
                    return StatusFailed(reason=reason)
                case never:
                    assert_never(never)
        case RunGitFailure(stdout=_, stderr=stderr):
            return _classify_fetch(stderr)
        case RunGitException(reason=reason):
            return _classify_fetch(reason)
        case never:
            assert_never(never)


# --- gate --------------------------------------------------------------------


class GatePass(BaseModel):
    """the deploy gate is open: clean, untracked-only or behind origin."""

    kind: Literal["gate-pass"] = "gate-pass"


class GateBlocked(BaseModel):
    """the deploy gate is closed, with the reason and the state behind it."""

    kind: Literal["gate-blocked"] = "gate-blocked"
    reason: str
    state: GitState


GateVerdict = Annotated[GatePass | GateBlocked, Field(discriminator="kind")]


def gate_verdict(state: GitState) -> GateVerdict:
    """The gate rule as a match over every state; exhaustive."""
    match state:
        case Clean() | UntrackedOnly() | BehindOrigin():
            return GatePass()
        case AheadOfOrigin() | Diverged():
            return GateBlocked(
                reason="local is ahead of origin (unpushed commits) - resolve manually or publish first",
                state=state,
            )
        case OnlyAdditions() | OnlyDeletions() | ContainsUpdates():
            return GateBlocked(
                reason="modified tracked files present - resolve manually first",
                state=state,
            )
        case FetchFailed(reason=reason) | AuthError(reason=reason):
            return GateBlocked(reason=f"fetch failed: {reason}", state=state)
        case StatusFailed(reason=reason):
            return GateBlocked(reason=f"status failed: {reason}", state=state)
        case never:
            assert_never(never)


def gate_check(env: dict[str, str]) -> GateVerdict:
    """Gate rule: clean/untracked-only/behind pass; everything else blocks."""
    return gate_verdict(git_status_summary(env))


# --- status view -------------------------------------------------------------


class StatusView(BaseModel):
    """What /status serves: the state plus the gate verdict over it."""

    state: GitState
    gate: GateVerdict


def status_view(env: dict[str, str]) -> StatusView:
    """One git pass, both consumers: the state plus the gate over it."""
    state = git_status_summary(env)
    return StatusView(state=state, gate=gate_verdict(state))
