from typing import Annotated, Literal

from pydantic import Field, BaseModel
from typing_extensions import assert_never

from ha_deploy.rootfs.app.supervisor import supervisor_request, SupervisorOk, SupervisorBodyMismatch, SupervisorError, SupervisorUnreachable


# TYPES
class CoreStarted(BaseModel):
    kind: Literal["core-started"] = "core-started"


class CoreStopped(BaseModel):
    kind: Literal["core-stopped"] = "core-stopped"



class CoreStopFailed(BaseModel):
    kind: Literal["core-stop-failed"] = "core-stop-failed"
    reason: str


class CoreStartFailed(BaseModel):
    kind: Literal["core-start-failed"] = "core-start-failed"
    reason: str


CoreState = Annotated[
    CoreStarted | CoreStopped,
    Field(discriminator="kind")
]


class CoreStateChangeError(BaseModel):
    kind: Literal["core-state-change-error"] = "core-state-change-error"
    error: CoreStopFailed | CoreStartFailed


class CoreStateChangeSuccess(BaseModel):
    kind: Literal["core-state-change-success"] = "core-state-change-success"
    state: CoreState


CoreChangeResult = Annotated[
    CoreStateChangeError | CoreStateChangeSuccess,
    Field(discriminator="kind")
]


# UTILS

# TODO: logger type?
def _restart_core(logger) -> CoreChangeResult:
    # TODO: should this perhaps loop.. ? or have a helper function that does?
    #       so on a temporary issue, it will retry a certain amount of times, or something.. ?
    #       or perhaps just return the result like today (but do note that stopping core will make UI not work, so retrying prolly not a bad idea.. ?)
    """start_core() with the old restart-result logging (recovery paths)."""
    outcome = start_core(logger)
    match outcome:
        case CoreStarted():
            logger.info("DEPLOY: start result: ok")
        case CoreStartFailed(reason=reason):
            logger.error("DEPLOY: start result: failed: %s", reason)
        case never:
            assert_never(never)
    return outcome


# TODO: Logger type
def start_core(logger) -> CoreChangeResult:
    """POST /core/start - synchronous control call (2xx == started, no polling)."""
    match supervisor_request("POST", "/core/start", None, timeout=30):
        case SupervisorOk():
            return CoreStateChangeSuccess(state=CoreStarted())
        case SupervisorError(code=code, body=body):
            logger.error("DEPLOY: start failed code=%s body=%s", code, body)
            error = CoreStartFailed(reason=f"core start returned {code}: {body}")
            return CoreStateChangeError(error=error)
        case SupervisorBodyMismatch(code=code, raw=raw):
            logger.error("DEPLOY: start failed code=%s with non-JSON body %r", code, raw)
            error = CoreStartFailed(reason=f"core start returned {code} with a non-JSON body: {raw}")
            return CoreStateChangeError(error=error)
        case SupervisorUnreachable(reason=reason):
            logger.error("DEPLOY: start failed, unreachable: %s", reason)
            error = CoreStartFailed(reason=f"core start unreachable: {reason}")
            return CoreStateChangeError(error=error)
        case never:
            assert_never(never)


def stop_core(logger) -> CoreChangeResult:
    """POST /core/stop - synchronous control call (2xx == stopped, no polling)."""
    match supervisor_request("POST", "/core/stop", None, timeout=30):
        case SupervisorOk():
            return CoreStateChangeSuccess(state=CoreStopped())
        case SupervisorError(code=code, body=body):
            logger.error("DEPLOY: stop failed code=%s body=%s", code, body)
            error = CoreStopFailed(reason=f"core stop returned {code}: {body}")
            return CoreStateChangeError(error=error)
        case SupervisorBodyMismatch(code=code, raw=raw):
            logger.error(
                "DEPLOY: stop failed code=%s with non-JSON body %r", code, raw
            )
            error = CoreStopFailed(reason=f"core stop returned {code} with a non-JSON body: {raw}")
            return CoreStateChangeError(error=error)
        case SupervisorUnreachable(reason=reason):
            logger.error("DEPLOY: stop failed, unreachable: %s", reason)
            error = CoreStopFailed(reason=f"core stop unreachable: {reason}")
            return CoreStateChangeError(error=error)
        case never:
            assert_never(never)

