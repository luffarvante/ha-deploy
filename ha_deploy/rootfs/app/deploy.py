"""HA Deploy - clean deploy module, Elm-style.

Implements the validated deploy flow:
  gate #1 -> backup -> stop core -> gate #2 -> pull -> verify -> start core.
The stopped-core window is explicit: a blocked body restarts the core once; a
successful body uses a final start with one retry. Unexpected exceptions
restart once and then re-raise to the route's last-resort catch.

Everything below the /deploy route's last-resort catch returns sum types
(pydantic models with a `kind` tag in discriminated unions, consumed via
`match`). Flow failures are variants, never a None or a flag; unexpected
exceptions are re-raised only after the stopped-core bracket.

Design decisions (see plan-typing.md and AGENTS.md "Code style"):
  - git_state.run_git maps subprocess outcomes into RunGitOutcome; git's exit
    code is never read as a value (only zero/non-zero), so the variants carry
    the streams only.
  - supervisor.supervisor_request maps HTTP/network outcomes into
    SupervisorOutcome; the old code=-1 sentinel is gone - unreachable is its
    own variant.
  - Per-endpoint decoders turn the Supervisor envelope into typed outcomes.
  - git_state.GitState's variants ARE the classifications; there is no summary
    string and no gate_ok flag - the gate verdict is a match over the state.
  - deploy() returns one variant per flow-terminal point (DeployOutcome).
"""

import logging
from typing import Annotated, Literal

import git_state
import ha_core
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError
import supervisor
from typing_extensions import assert_never

_LOGGER = logging.getLogger(__name__)

BACKUP_NAME = "pre-deploy"


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
    match git_state.run_git(["pull"], env):
        case git_state.RunGitSuccess(stdout=stdout, stderr=stderr):
            output = _git_output(stdout, stderr)
            _LOGGER.info("DEPLOY: git pull output=%r", output)
            if "CONFLICT" in output or "error:" in output.lower():
                return PullFailed(output=output, reason="git pull failed")
            return Pulled(output=output)
        case git_state.RunGitFailure(stdout=stdout, stderr=stderr):
            output = _git_output(stdout, stderr)
            _LOGGER.info("DEPLOY: git pull output=%r", output)
            return PullFailed(output=output, reason="git pull failed")
        case git_state.RunGitException(reason=reason):
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
    output: str
    modified: list[str]
    reason: str


def verify(env: dict[str, str], output: str) -> VerifiedClean | VerifyFailed:
    """Post-pull verify: git status --porcelain, zero non-?? lines -> clean."""
    match git_state.run_git(["status", "--porcelain"], env):
        case git_state.RunGitSuccess(stdout=stdout):
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
                    output=output,
                    modified=modified,
                    reason="unexpected tracked changes after pull",
                )
            _LOGGER.info("DEPLOY: post-pull verify ok")
            return VerifiedClean()
        case git_state.RunGitFailure(stdout=_, stderr=stderr):
            _LOGGER.error("DEPLOY: verify failed: %s", stderr)
            return VerifyFailed(
                output=output, modified=[], reason=f"git status failed: {stderr}"
            )
        case git_state.RunGitException(reason=reason):
            _LOGGER.error("DEPLOY: verify failed: %s", reason)
            return VerifyFailed(output=output, modified=[], reason=reason)
        case never:
            assert_never(never)


# --- orchestrator: one variant per flow-terminal point -----------------------


class WindowSucceeded(BaseModel):
    """the post-stop window completed: gate #2, pull, and verify all passed."""

    kind: Literal["window-succeeded"] = "window-succeeded"
    output: str


WindowOutcome = Annotated[
    WindowSucceeded | git_state.GateBlocked | PullFailed | VerifyFailed,
    Field(discriminator="kind"),
]


class BlockedBeforeBackup(BaseModel):
    """gate #1 failed; nothing stopped, no backup taken."""

    kind: Literal["blocked-before-backup"] = "blocked-before-backup"
    reason: str
    state: git_state.GitState


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
    reason: str
    state: git_state.GitState
    backup: str
    restart: ha_core.CoreStartResult


class BlockedByPull(BaseModel):
    """git pull failed; the core was restarted."""

    kind: Literal["blocked-by-pull"] = "blocked-by-pull"
    reason: str
    output: str
    backup: str
    restart: ha_core.CoreStartResult


class BlockedByVerify(BaseModel):
    """unexpected tracked changes after the pull; the core was restarted."""

    kind: Literal["blocked-by-verify"] = "blocked-by-verify"
    reason: str
    output: str
    modified: list[str]
    backup: str
    restart: ha_core.CoreStartResult


class BlockedByStart(BaseModel):
    """the final start failed, including one explicit retry; core is not running."""

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


def _window_steps(env: dict[str, str]) -> WindowOutcome:
    """Gate #2 -> pull -> verify. No core action; returns the window result."""
    _LOGGER.info("DEPLOY: running after-stop check (gate #2)")
    match git_state.gate_check(env):
        case git_state.GateBlocked(reason=reason, state=state):
            return git_state.GateBlocked(reason=reason, state=state)
        case git_state.GatePass():
            pass
        case never:
            assert_never(never)

    match pull(env):
        case PullFailed(reason=reason, output=output):
            return PullFailed(reason=reason, output=output)
        case Pulled(output=output):
            match verify(env, output):
                case VerifiedClean():
                    return WindowSucceeded(output=output)
                case VerifyFailed() as failed:
                    return failed
                case never:
                    assert_never(never)
        case never:
            assert_never(never)


def _backup_phase(env: dict[str, str]) -> DeployOutcome:
    """Backup, then the stopped phase. A failed backup never stops core."""
    match create_backup():
        case BackupFailed(reason=reason):
            _LOGGER.error("DEPLOY: backup failed - returning: %s", reason)
            return BlockedByBackup(reason=reason)
        case BackupCreated(slug=slug):
            return _stop_phase(slug, env)
        case never:
            assert_never(never)


def _stop_phase(backup: str, env: dict[str, str]) -> DeployOutcome:
    """Stop core. A failed stop returns without a restart attempt."""
    _LOGGER.info("DEPLOY: stopping ha core")
    match ha_core.stop_core():
        case ha_core.CoreStopFailed(reason=reason):
            _LOGGER.error("DEPLOY: stop failed - returning: %s", reason)
            return BlockedByStop(reason=reason, backup=backup)
        case ha_core.CoreStopped():
            return _after_stop(backup, env)
        case never:
            assert_never(never)


def _after_stop(backup: str, env: dict[str, str]) -> DeployOutcome:
    """The stopped-core bracket: body once, then one final/recovery start."""
    try:
        body = _window_steps(env)
    except BaseException:
        _ = ha_core.start_core()
        raise

    match body:
        case WindowSucceeded(output=output):
            return _finish_success(backup, output, _start_with_retry())
        case git_state.GateBlocked() | PullFailed() | VerifyFailed():
            return _finish_blocked(backup, body, ha_core.start_core())
        case never:
            assert_never(never)


def _start_with_retry() -> ha_core.CoreStartResult:
    """Start once, then retry once only if the first final start failed."""
    match ha_core.start_core():
        case ha_core.CoreStarted():
            return ha_core.CoreStarted()
        case ha_core.CoreStartFailed(reason=reason):
            _LOGGER.warning("DEPLOY: final start failed, retrying once: %s", reason)
            return ha_core.start_core()
        case never:
            assert_never(never)


def _finish_success(
    backup: str,
    output: str,
    start: ha_core.CoreStartResult,
) -> DeployOutcome:
    """Pure success-path composition: final start result -> outcome."""
    match start:
        case ha_core.CoreStarted():
            return DeploySucceeded(output=output, backup=backup)
        case ha_core.CoreStartFailed(reason=reason):
            return BlockedByStart(reason=reason, output=output, backup=backup)
        case never:
            assert_never(never)


def _finish_blocked(
    backup: str,
    body: git_state.GateBlocked | PullFailed | VerifyFailed,
    restart: ha_core.CoreStartResult,
) -> DeployOutcome:
    """Pure blocked-path composition: body + recovery result -> outcome."""
    match body:
        case git_state.GateBlocked(reason=reason, state=state):
            return BlockedAfterStop(
                reason=reason, state=state, backup=backup, restart=restart
            )
        case PullFailed(reason=reason, output=output):
            return BlockedByPull(
                reason=reason, output=output, backup=backup, restart=restart
            )
        case VerifyFailed(reason=reason, output=output, modified=modified):
            return BlockedByVerify(
                reason=reason,
                output=output,
                modified=modified,
                backup=backup,
                restart=restart,
            )
        case never:
            assert_never(never)


def deploy() -> DeployOutcome:
    """Single entrypoint. Gate #1, backup, then the stopped-core phase."""
    _LOGGER.info("DEPLOY: ===== deploy started =====")
    env = git_state.derive_git_env()

    _LOGGER.info("DEPLOY: running pre-check (gate #1)")
    match git_state.gate_check(env):
        case git_state.GatePass():
            _LOGGER.info("DEPLOY: pre-check OK")
            return _backup_phase(env)
        case git_state.GateBlocked(reason=reason, state=state):
            _LOGGER.error("DEPLOY: gate #1 failed - returning: %s", reason)
            return BlockedBeforeBackup(reason=reason, state=state)
        case never:
            assert_never(never)


def describe(outcome: DeployOutcome) -> str:
    """Pure presentation of an outcome; no IO and no side effects."""
    match outcome:
        case DeploySucceeded():
            return "ok"
        case BlockedBeforeBackup(reason=reason):
            return f"blocked before backup: {reason}"
        case BlockedByBackup(reason=reason):
            return f"backup failed: {reason}"
        case BlockedByStop(reason=reason):
            return f"core stop failed: {reason}; no restart attempted"
        case BlockedAfterStop(reason=reason, restart=restart):
            return f"{reason}; {_describe_restart(restart)}"
        case BlockedByPull(reason=reason, restart=restart):
            return f"pull failed: {reason}; {_describe_restart(restart)}"
        case BlockedByVerify(reason=reason, restart=restart):
            return f"verify failed: {reason}; {_describe_restart(restart)}"
        case BlockedByStart(reason=reason):
            return f"core start failed: {reason}"
        case never:
            assert_never(never)


def _describe_restart(restart: ha_core.CoreStartResult) -> str:
    """Pure description of a recovery start result."""
    match restart:
        case ha_core.CoreStarted():
            return "core restarted"
        case ha_core.CoreStartFailed(reason=reason):
            return f"core restart failed: {reason}"
        case never:
            assert_never(never)
