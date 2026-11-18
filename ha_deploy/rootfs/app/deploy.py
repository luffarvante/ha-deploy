"""HA Deploy - clean deploy module, Elm-style.

Implements the validated deploy flow:
  gate #1 -> backup -> stop core -> gate #2 -> pull -> verify -> start core
with a try/finally restart safety net.

Everything below the /deploy route's last-resort catch returns sum types
(pydantic models with a `kind` tag in discriminated unions, consumed via
`match`); nothing raises. Failure is a variant, never a None or a flag.

Design decisions (see plan-typing.md and AGENTS.md "Code style"):
  - run_git maps subprocess outcomes into RunGitOutcome; git's exit code is
    never read as a value (only zero/non-zero), so the variants carry the
    streams only.
  - supervisor_request maps HTTP/network outcomes into SupervisorOutcome;
    the old code=-1 sentinel is gone - unreachable is its own variant.
  - Per-endpoint decoders turn the Supervisor envelope into typed outcomes.
  - GitState's variants ARE the classifications; there is no summary string
    and no gate_ok flag - the gate verdict is a match over the state.
  - deploy() returns one variant per flow-terminal point (DeployOutcome).
"""

import logging
import os
import re
import subprocess
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError
import supervisor
from typing_extensions import assert_never

_LOGGER = logging.getLogger(__name__)

GIT_DIR = "/homeassistant"
SSH_KEY = "/data/id_ed25519"

BACKUP_NAME = "pre-deploy"


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


# --- steps -------------------------------------------------------------------


class _SupervisorEnvelope(BaseModel):
    """The envelope Supervisor endpoints answer with; only data is needed."""

    data: JsonValue


class _BackupBySlug(BaseModel):
    slug: str


class _BackupByName(BaseModel):
    name: str


_BackupData: TypeAdapter[_BackupBySlug | _BackupByName] = TypeAdapter(
    _BackupBySlug | _BackupByName
)


def _backup_slug(body: JsonValue) -> str:
    """Decode the backup identifier from the /backups/new/full body.

    Raises ValidationError on a body mismatch - create_backup catches it.
    """
    payload = _SupervisorEnvelope.model_validate(body).data
    match _BackupData.validate_python(payload):
        case _BackupBySlug(slug=slug):
            return slug
        case _BackupByName(name=name):
            return name
        case never:
            assert_never(never)


class BackupCreated(BaseModel):
    """the full backup was taken; slug identifies it (name fallback kept)."""

    kind: Literal["backup-created"] = "backup-created"
    slug: str


class BackupFailed(BaseModel):
    """the backup API call failed for any reason; reason carries the detail."""

    kind: Literal["backup-failed"] = "backup-failed"
    reason: str


def create_backup() -> BackupCreated | BackupFailed:
    """POST /backups/new/full. Blocks until the backup is done (decided, D2)."""
    _LOGGER.info("DEPLOY: creating full backup")
    match supervisor.supervisor_request(
        "POST", "/backups/new/full", {"name": BACKUP_NAME}, timeout=300
    ):
        case supervisor.SupervisorOk(code=_, body=body):
            try:
                slug = _backup_slug(body)
            except ValidationError:
                _LOGGER.error("DEPLOY: backup body mismatch: %s", body)
                return BackupFailed(
                    reason=f"backup API returned unexpected body (no slug): {body}"
                )
            _LOGGER.info("DEPLOY: backup created slug=%s", slug)
            return BackupCreated(slug=slug)
        case supervisor.SupervisorError(code=code, body=body):
            _LOGGER.error("DEPLOY: backup failed code=%s body=%s", code, body)
            return BackupFailed(reason=f"backup API returned {code}: {body}")
        case supervisor.SupervisorBodyMismatch(code=code, raw=raw):
            _LOGGER.error(
                "DEPLOY: backup failed code=%s with non-JSON body %r", code, raw
            )
            return BackupFailed(
                reason=f"backup API returned {code} with a non-JSON body: {raw}"
            )
        case supervisor.SupervisorUnreachable(reason=reason):
            _LOGGER.error("DEPLOY: backup API unreachable: %s", reason)
            return BackupFailed(reason=f"backup API unreachable: {reason}")
        case never:
            assert_never(never)


class CoreStopped(BaseModel):
    """POST /core/stop answered 2xx."""

    kind: Literal["core-stopped"] = "core-stopped"


class CoreStopFailed(BaseModel):
    """the core could not be stopped; reason carries the detail."""

    kind: Literal["core-stop-failed"] = "core-stop-failed"
    reason: str


def stop_core() -> CoreStopped | CoreStopFailed:
    """POST /core/stop - synchronous control call (2xx == stopped, no polling)."""
    match supervisor.supervisor_request("POST", "/core/stop", None, timeout=30):
        case supervisor.SupervisorOk():
            return CoreStopped()
        case supervisor.SupervisorError(code=code, body=body):
            _LOGGER.error("DEPLOY: stop failed code=%s body=%s", code, body)
            return CoreStopFailed(reason=f"core stop returned {code}: {body}")
        case supervisor.SupervisorBodyMismatch(code=code, raw=raw):
            _LOGGER.error(
                "DEPLOY: stop failed code=%s with non-JSON body %r", code, raw
            )
            return CoreStopFailed(
                reason=f"core stop returned {code} with a non-JSON body: {raw}"
            )
        case supervisor.SupervisorUnreachable(reason=reason):
            _LOGGER.error("DEPLOY: stop failed, unreachable: %s", reason)
            return CoreStopFailed(reason=f"core stop unreachable: {reason}")
        case never:
            assert_never(never)


class CoreStarted(BaseModel):
    """POST /core/start answered 2xx."""

    kind: Literal["core-started"] = "core-started"


class CoreStartFailed(BaseModel):
    """the core could not be started; reason carries the detail."""

    kind: Literal["core-start-failed"] = "core-start-failed"
    reason: str


def start_core() -> CoreStarted | CoreStartFailed:
    """POST /core/start - synchronous control call (2xx == started, no polling)."""
    match supervisor.supervisor_request("POST", "/core/start", None, timeout=30):
        case supervisor.SupervisorOk():
            return CoreStarted()
        case supervisor.SupervisorError(code=code, body=body):
            _LOGGER.error("DEPLOY: start failed code=%s body=%s", code, body)
            return CoreStartFailed(reason=f"core start returned {code}: {body}")
        case supervisor.SupervisorBodyMismatch(code=code, raw=raw):
            _LOGGER.error(
                "DEPLOY: start failed code=%s with non-JSON body %r", code, raw
            )
            return CoreStartFailed(
                reason=f"core start returned {code} with a non-JSON body: {raw}"
            )
        case supervisor.SupervisorUnreachable(reason=reason):
            _LOGGER.error("DEPLOY: start failed, unreachable: %s", reason)
            return CoreStartFailed(reason=f"core start unreachable: {reason}")
        case never:
            assert_never(never)


def _git_output(stdout: str, stderr: str) -> str:
    return (stdout + ("\n" + stderr if stderr else "")).strip()


class Pulled(BaseModel):
    """git pull succeeded without conflict/error markers; output is its text."""

    kind: Literal["pulled"] = "pulled"
    output: str


class PullFailed(BaseModel):
    """git pull failed or produced conflict/error markers."""

    kind: Literal["pull-failed"] = "pull-failed"
    output: str
    reason: str


def pull(env: dict[str, str]) -> Pulled | PullFailed:
    """Git pull. Success AND no CONFLICT AND no error marker in the output."""
    _LOGGER.info("DEPLOY: running git pull")
    match run_git(["pull"], env):
        case RunGitSuccess(stdout=stdout, stderr=stderr):
            output = _git_output(stdout, stderr)
            _LOGGER.info("DEPLOY: git pull output=%r", output)
            if "CONFLICT" in output or "error:" in output.lower():
                return PullFailed(output=output, reason="git pull failed")
            return Pulled(output=output)
        case RunGitFailure(stdout=stdout, stderr=stderr):
            output = _git_output(stdout, stderr)
            _LOGGER.info("DEPLOY: git pull output=%r", output)
            return PullFailed(output=output, reason="git pull failed")
        case RunGitException(reason=reason):
            _LOGGER.error("DEPLOY: git pull exception: %s", reason)
            return PullFailed(output="", reason=reason)
        case never:
            assert_never(never)


class VerifiedClean(BaseModel):
    """post-pull status shows no tracked changes."""

    kind: Literal["verified-clean"] = "verified-clean"


class VerifyFailed(BaseModel):
    """tracked changes appeared after the pull."""

    kind: Literal["verify-failed"] = "verify-failed"
    modified: list[str]
    reason: str


def verify(env: dict[str, str]) -> VerifiedClean | VerifyFailed:
    """Post-pull verify: git status --porcelain, zero non-?? lines -> clean."""
    match run_git(["status", "--porcelain"], env):
        case RunGitSuccess(stdout=stdout):
            modified = [
                entry
                for entry in stdout.splitlines()
                if entry.strip() and not entry.startswith("??")
            ]
            if modified:
                _LOGGER.error(
                    "DEPLOY: unexpected tracked changes after pull: %s", modified
                )
                return VerifyFailed(
                    modified=modified, reason="unexpected tracked changes after pull"
                )
            _LOGGER.info("DEPLOY: post-pull verify ok")
            return VerifiedClean()
        case RunGitFailure(stdout=_, stderr=stderr):
            _LOGGER.error("DEPLOY: verify failed: %s", stderr)
            return VerifyFailed(modified=[], reason=f"git status failed: {stderr}")
        case RunGitException(reason=reason):
            _LOGGER.error("DEPLOY: verify failed: %s", reason)
            return VerifyFailed(modified=[], reason=reason)
        case never:
            assert_never(never)


# --- orchestrator: one variant per flow-terminal point -----------------------


class BlockedBeforeBackup(BaseModel):
    """gate #1 failed; nothing stopped, no backup taken."""

    kind: Literal["blocked-before-backup"] = "blocked-before-backup"
    state: GitState


class BlockedByBackup(BaseModel):
    """the backup failed; nothing stopped."""

    kind: Literal["blocked-by-backup"] = "blocked-by-backup"
    reason: str


class BlockedByStop(BaseModel):
    """the core could not be stopped; it is presumably still running."""

    kind: Literal["blocked-by-stop"] = "blocked-by-stop"
    reason: str
    backup: str


class BlockedAfterStop(BaseModel):
    """gate #2 failed after the stop; the core was restarted."""

    kind: Literal["blocked-after-stop"] = "blocked-after-stop"
    state: GitState
    backup: str


class BlockedByPull(BaseModel):
    """git pull failed; the core was restarted."""

    kind: Literal["blocked-by-pull"] = "blocked-by-pull"
    output: str
    backup: str


class BlockedByVerify(BaseModel):
    """unexpected tracked changes after the pull; the core was restarted."""

    kind: Literal["blocked-by-verify"] = "blocked-by-verify"
    output: str
    modified: list[str]
    backup: str


class BlockedByStart(BaseModel):
    """the core could not be started again; the finally still tried."""

    kind: Literal["blocked-by-start"] = "blocked-by-start"
    reason: str
    output: str
    backup: str


class DeploySucceeded(BaseModel):
    """the whole flow completed: backup taken, code pulled, core started."""

    kind: Literal["deploy-succeeded"] = "deploy-succeeded"
    output: str
    backup: str


class RouteCrash(BaseModel):
    """The last-resort catch's answer: deploy() itself raised (a bug, not a flow end)."""

    kind: Literal["route-crash"] = "route-crash"
    reason: str


DeployOutcome = Annotated[
    BlockedBeforeBackup
    | BlockedByBackup
    | BlockedByStop
    | BlockedAfterStop
    | BlockedByPull
    | BlockedByVerify
    | BlockedByStart
    | DeploySucceeded,
    Field(discriminator="kind"),
]


def _restart_core() -> CoreStarted | CoreStartFailed:
    """start_core() with the old restart-result logging (recovery paths)."""
    outcome = start_core()
    match outcome:
        case CoreStarted():
            _LOGGER.info("DEPLOY: start result: ok")
        case CoreStartFailed(reason=reason):
            _LOGGER.error("DEPLOY: start result: failed: %s", reason)
        case never:
            assert_never(never)
    return outcome


def _backup_taken_phase(backup: str, env: dict[str, str]) -> DeployOutcome:
    """Everything after a successful backup; owns core_stopped + the finally."""
    core_stopped = False
    try:
        # Stop HA core. Fail -> return without starting: the core is
        # presumably still running (decided).
        _LOGGER.info("DEPLOY: stopping ha core")
        match stop_core():
            case CoreStopped():
                core_stopped = True
            case CoreStopFailed(reason=reason):
                return BlockedByStop(reason=reason, backup=backup)
            case never:
                assert_never(never)

        # Gate #2 (after stop). Fail -> start, return.
        _LOGGER.info("DEPLOY: running after-stop check (gate #2)")
        match gate_check(env):
            case GatePass():
                pass
            case GateBlocked(reason=reason, state=state):
                _LOGGER.error(
                    "DEPLOY: after-stop check was not fine, will return error but first will start ha core"
                )
                _ = _restart_core()
                return BlockedAfterStop(state=state, backup=backup)
            case never:
                assert_never(never)

        match pull(env):
            case Pulled(output=pulled_output):
                match verify(env):
                    case VerifiedClean():
                        # Start HA core. Success clears the finally's restart.
                        _LOGGER.info("DEPLOY: starting ha core")
                        match start_core():
                            case CoreStarted():
                                core_stopped = False
                                _LOGGER.info("DEPLOY: ===== deploy finished OK =====")
                                return DeploySucceeded(
                                    output=pulled_output, backup=backup
                                )
                            case CoreStartFailed(reason=reason):
                                _LOGGER.error(
                                    "DEPLOY: start failed - returning: %s", reason
                                )
                                return BlockedByStart(
                                    reason=reason, output=pulled_output, backup=backup
                                )
                            case never:
                                assert_never(never)
                    case VerifyFailed(modified=modified):
                        _LOGGER.error(
                            "DEPLOY: verify was not fine, will return error but first will start ha core"
                        )
                        _ = _restart_core()
                        return BlockedByVerify(
                            output=pulled_output, modified=modified, backup=backup
                        )
                    case never:
                        assert_never(never)
            case PullFailed(output=output):
                _LOGGER.error(
                    "DEPLOY: pull was not fine, will return error but first will start ha core"
                )
                _ = _restart_core()
                return BlockedByPull(output=output, backup=backup)
            case never:
                assert_never(never)
    finally:
        # Safety net for ANY exception after a successful stop - this exact
        # gap is what broke the old code, because core_stopped stayed False
        # there.
        if core_stopped:
            _LOGGER.warning("DEPLOY: finally - core was stopped, restarting")
            _ = _restart_core()


def deploy() -> DeployOutcome:
    """Single entrypoint. Straight-line steps; the stopped phase owns recovery."""
    _LOGGER.info("DEPLOY: ===== deploy started =====")
    env = derive_git_env()

    # Gate #1 (pre-stop). Fail -> return; nothing stopped, no core action.
    _LOGGER.info("DEPLOY: running pre-check (gate #1)")
    match gate_check(env):
        case GatePass():
            _LOGGER.info("DEPLOY: pre-check OK")
        case GateBlocked(reason=reason, state=state):
            _LOGGER.error("DEPLOY: gate #1 failed - returning: %s", reason)
            return BlockedBeforeBackup(state=state)
        case never:
            assert_never(never)

    match create_backup():
        case BackupCreated(slug=slug):
            return _backup_taken_phase(slug, env)
        case BackupFailed(reason=reason):
            _LOGGER.error("DEPLOY: backup failed - returning: %s", reason)
            return BlockedByBackup(reason=reason)
        case never:
            assert_never(never)


# --- status view -------------------------------------------------------------


class StatusView(BaseModel):
    """What /status serves: the state plus the gate verdict over it."""

    state: GitState
    gate: GateVerdict


def status_view(env: dict[str, str]) -> StatusView:
    """One git pass, both consumers: the state plus the gate over it."""
    state = git_status_summary(env)
    return StatusView(state=state, gate=gate_verdict(state))
