"""HA Deploy - clean deploy module, Elm-style.

Implements the validated deploy flow:
  gate #1 -> backup -> stop core -> gate #2 -> pull -> verify -> start core
with a try/finally restart safety net.

Everything below the /deploy route's last-resort catch returns sum types
(pydantic models with a `kind` tag in discriminated unions, consumed via
`match`); nothing raises. Failure is a variant, never a None or a flag.

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
    modified: list[str]
    reason: str


def verify(env: dict[str, str]) -> VerifiedClean | VerifyFailed:
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
                    modified=modified, reason="unexpected tracked changes after pull"
                )
            _LOGGER.info("DEPLOY: post-pull verify ok")
            return VerifiedClean()
        case git_state.RunGitFailure(stdout=_, stderr=stderr):
            _LOGGER.error("DEPLOY: verify failed: %s", stderr)
            return VerifyFailed(modified=[], reason=f"git status failed: {stderr}")
        case git_state.RunGitException(reason=reason):
            _LOGGER.error("DEPLOY: verify failed: %s", reason)
            return VerifyFailed(modified=[], reason=reason)
        case never:
            assert_never(never)


# --- orchestrator: one variant per flow-terminal point -----------------------


class BlockedBeforeBackup(BaseModel):
    """gate #1 failed; nothing stopped, no backup taken."""

    kind: Literal["blocked-before-backup"] = "blocked-before-backup"
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
    state: git_state.GitState
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


def _restart_core() -> ha_core.CoreStarted | ha_core.CoreStartFailed:
    """start_core() with the old restart-result logging (recovery paths)."""
    outcome = ha_core.start_core()
    match outcome:
        case ha_core.CoreStarted():
            _LOGGER.info("DEPLOY: start result: ok")
        case ha_core.CoreStartFailed(reason=reason):
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
        match ha_core.stop_core():
            case ha_core.CoreStopped():
                core_stopped = True
            case ha_core.CoreStopFailed(reason=reason):
                return BlockedByStop(reason=reason, backup=backup)
            case never:
                assert_never(never)

        # Gate #2 (after stop). Fail -> start, return.
        _LOGGER.info("DEPLOY: running after-stop check (gate #2)")
        match git_state.gate_check(env):
            case git_state.GatePass():
                pass
            case git_state.GateBlocked(reason=reason, state=state):
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
                        match ha_core.start_core():
                            case ha_core.CoreStarted():
                                core_stopped = False
                                _LOGGER.info("DEPLOY: ===== deploy finished OK =====")
                                return DeploySucceeded(
                                    output=pulled_output, backup=backup
                                )
                            case ha_core.CoreStartFailed(reason=reason):
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
    env = git_state.derive_git_env()

    # Gate #1 (pre-stop). Fail -> return; nothing stopped, no core action.
    _LOGGER.info("DEPLOY: running pre-check (gate #1)")
    match git_state.gate_check(env):
        case git_state.GatePass():
            _LOGGER.info("DEPLOY: pre-check OK")
        case git_state.GateBlocked(reason=reason, state=state):
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
