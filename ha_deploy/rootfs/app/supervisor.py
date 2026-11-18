"""Supervisor HTTP boundary: request outcomes as sum types.

This module knows nothing about deploy, git, or core. It turns HTTP/network
results into SupervisorOutcome values and never raises.
"""

import contextlib
import json
import os
from typing import Annotated, Literal
import urllib.error
import urllib.request

from pydantic import BaseModel, Field, JsonValue

SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


class SupervisorOk(BaseModel):
    """2xx with a parseable JSON body."""

    kind: Literal["ok"] = "ok"
    code: int
    body: JsonValue


class SupervisorError(BaseModel):
    """non-2xx with a parseable JSON body."""

    kind: Literal["error"] = "error"
    code: int
    body: JsonValue


class SupervisorBodyMismatch(BaseModel):
    """the body was not JSON (any status code)."""

    kind: Literal["body-mismatch"] = "body-mismatch"
    code: int
    raw: str


class SupervisorUnreachable(BaseModel):
    """the Supervisor API could not be reached at all (network, timeout)."""

    kind: Literal["unreachable"] = "unreachable"
    reason: str


SupervisorOutcome = Annotated[
    SupervisorOk | SupervisorError | SupervisorBodyMismatch | SupervisorUnreachable,
    Field(discriminator="kind"),
]


def _parse_supervisor_body(code: int, raw: str) -> SupervisorOutcome:
    try:
        parsed: JsonValue = json.loads(raw)
    except Exception:
        return SupervisorBodyMismatch(code=code, raw=raw[:500])
    if 200 <= code < 300:
        return SupervisorOk(code=code, body=parsed)
    return SupervisorError(code=code, body=parsed)


def supervisor_request(
    method: str, path: str, body: JsonValue | None, timeout: float
) -> SupervisorOutcome:
    """Defensive Supervisor API call: returns an outcome; never raises.

    `body=None` is a real state: the request carries no JSON body. HTTPError
    bodies are parsed so a non-2xx body reaches the log.
    """
    url = f"{SUPERVISOR}{path}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw: str = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = ""
        with contextlib.suppress(Exception):
            raw = e.read().decode(errors="replace")
        return _parse_supervisor_body(e.code, raw)
    except Exception as e:
        return SupervisorUnreachable(reason=str(e))
    return _parse_supervisor_body(status, raw)
