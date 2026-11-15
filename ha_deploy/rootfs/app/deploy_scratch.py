from typing import Literal, Annotated

from pydantic import BaseModel, Field
from typing_extensions import assert_never

from ha_deploy.rootfs.app import ha_core_utils
from ha_deploy.rootfs.app.git_state import GitState


# TYPES
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


"""the deploy gate is open: clean, untracked-only or behind origin."""
class GatePass(BaseModel):
    kind: Literal["gate-pass"] = "gate-pass"


"""the deploy gate is closed, with the reason and the state behind it."""
class GateBlocked(BaseModel):
    kind: Literal["gate-blocked"] = "gate-blocked"
    reason: str
    state: GitState

GateVerdict = Annotated[GatePass | GateBlocked, Field(discriminator="kind")]

# TODO: logger type
def _backup_taken_phase(logger, backup: str, env: dict[str, str]) -> DeployOutcome:
    """Everything after a successful backup; owns core_stopped + the finally."""
    core_stopped = False
    try:
        # Stop HA core. Fail -> return without starting: the core is
        # presumably still running (decided).
        logger.info("DEPLOY: stopping ha core")
        match ha_core_utils.stop_core(logger):
            case ha_core_utils.CoreStateChangeSuccess(state=state):
                return gate_check(logger, state, env)
            case ha_core_utils.CoreStateChangeError(error=error):
                reason = error.reason
                return BlockedByStop(reason=reason, backup=backup)
            case never:
                assert_never(never)

        # Gate #2 (after stop). Fail -> start, return.
        logger.info("DEPLOY: running after-stop check (gate #2)")
        match gate_check(env):
            case GatePass():
                pass
            case GateBlocked(reason=reason, state=state):
                logger.error(
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
                        logger.info("DEPLOY: starting ha core")
                        match start_core():
                            case CoreStarted():
                                core_stopped = False
                                logger.info("DEPLOY: ===== deploy finished OK =====")
                                return DeploySucceeded(
                                    output=pulled_output, backup=backup
                                )
                            case CoreStartFailed(reason=reason):
                                logger.error(
                                    "DEPLOY: start failed - returning: %s", reason
                                )
                                return BlockedByStart(
                                    reason=reason, output=pulled_output, backup=backup
                                )
                            case never:
                                assert_never(never)
                    case VerifyFailed(modified=modified):
                        logger.error(
                            "DEPLOY: verify was not fine, will return error but first will start ha core"
                        )
                        _ = _restart_core()
                        return BlockedByVerify(
                            output=pulled_output, modified=modified, backup=backup
                        )
                    case never:
                        assert_never(never)
            case PullFailed(output=output):
                logger.error(
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
            logger.warning("DEPLOY: finally - core was stopped, restarting")
            _ = _restart_core()


# TODO: logger type
# TODO: return type here should not be gate verdict (that is remain from copy paste, now gate check happens here instead of returnig the gate check from here)
def gate_check(logger, state: ha_core_utils.CoreState, env: dict[str, str]) -> GateVerdict:
    """Gate rule: clean/untracked-only/behind pass; everything else blocks."""
    # TODO: git_status_summary and run_git etc should probably be in its own git module.. ?
    match gate_verdict(git_status_summary(env)):
        case GatePass():
            return pull_result(state, env) # this continues calling the methods
        case GateBlocked(reason=reason, state=state):
            # TODO; should we not log the reason? and probably the state we are in.. ?
            logger.error(
                "DEPLOY: after-stop check was not fine, will return error but first will start ha core"
            )
            # TODO: the restart should be assigned to a variable
            #       then included in the response/result, so we know whether the core is running or not, no?
            return ha_core_utils._restart_core(logger)
        case never:
            assert_never(never)



# TODO: cont