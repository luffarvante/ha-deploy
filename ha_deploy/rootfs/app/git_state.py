from typing import Literal, Annotated

from pydantic import BaseModel, Field


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

