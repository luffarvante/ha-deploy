from typing import Literal, Annotated

from pydantic import BaseModel, Field


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


Result = Annotated[
    CoreStateChangeError | CoreState,
    Field(discriminator="kind")
]
