"""Home Assistant core start/stop control.

This module owns the core control boundary and its outcomes. It knows nothing
about git or the deploy flow.
"""

import logging
from typing import Annotated, Literal

from pydantic import BaseModel, Field
import supervisor
from typing_extensions import assert_never

_LOGGER = logging.getLogger(__name__)


class CoreStopped(BaseModel):
    """POST /core/stop answered 2xx."""

    kind: Literal["core-stopped"] = "core-stopped"


class CoreStopFailed(BaseModel):
    """the core could not be stopped; reason carries the detail."""

    kind: Literal["core-stop-failed"] = "core-stop-failed"
    reason: str


CoreStopResult = Annotated[
    CoreStopped | CoreStopFailed,
    Field(discriminator="kind"),
]


def stop_core() -> CoreStopResult:
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


CoreStartResult = Annotated[
    CoreStarted | CoreStartFailed,
    Field(discriminator="kind"),
]


def start_core() -> CoreStartResult:
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
