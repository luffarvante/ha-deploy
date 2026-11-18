# HA Deploy — deploy flow design

Status: design settled; not implemented. This document describes the target
shape of the deploy flow before the next implementation round.

## Goals

- No mutable state flags (`core_stopped`, `ok: bool`, etc.).
- No `CoreState` threaded through step functions.
- No `<something> | None`; absence is represented by a variant.
- No generic `Result` wrapper. Results are flat, operation-specific sum types.
- Each function does one thing and returns its result.
- IO stays at the boundary; composition and presentation are pure and easy to test.
- Exhaustive `match` with `assert_never` everywhere.

The central rule:

> "Core is stopped" is a phase, not a stored state. "Restart succeeded" is an
> outcome, not a boolean.

## Module layout

```text
supervisor.py  ->  no app-specific imports
git_state.py   ->  subprocess only
ha_core.py     ->  supervisor
deploy.py      ->  git_state + ha_core + supervisor
server.py      ->  deploy
```

### `supervisor.py`

Owns the Supervisor HTTP boundary and its outcomes:

- `SupervisorOk`
- `SupervisorError`
- `SupervisorBodyMismatch`
- `SupervisorUnreachable`
- `SupervisorOutcome`
- `supervisor_request()`

No deploy, git, or core knowledge.

### `git_state.py`

Owns git execution, status parsing, and gate rules:

- `RunGitSuccess`, `RunGitFailure`, `RunGitException`
- `GitState` variants (`Clean`, `UntrackedOnly`, `BehindOrigin`, etc.)
- `gate_verdict()`, `gate_check()`
- `status_view()`
- `run_git()`, `git_status_summary()`

### `ha_core.py`

Owns core start/stop only:

- `CoreStopped`, `CoreStopFailed`, `CoreStopResult`
- `CoreStarted`, `CoreStartFailed`, `CoreStartResult`
- `stop_core()`, `start_core()`

There is no `CoreState` type. `CoreStartResult` is the result of a recovery
action, not a state field.

### `deploy.py`

Owns the deploy vocabulary, phases, and presentation:

- `BackupCreated`, `BackupFailed`, `BackupOutcome`
- `Pulled`, `PullFailed`, `PullOutcome`
- `VerifiedClean`, `VerifyFailed`, `VerifyOutcome`
- `WindowSucceeded`, `WindowOutcome`
- `DeploySucceeded` and the `Blocked*` variants, `DeployOutcome`
- `create_backup()`, `pull()`, `verify()`
- `_window_steps()`, `_stop_phase()`, `_after_stop()`
- `_start_with_retry()`, `_finish_success()`, `_finish_blocked()`
- `deploy()`, `describe()`

### `server.py`

Routes and serialization only. It calls `deploy.deploy()` and optionally
`deploy.describe()`; it does not sequence phases.

### Tests

Mirror the modules:

- `test_supervisor.py`
- `test_git_state.py`
- `test_ha_core.py`
- `test_deploy.py`
- `test_routes.py`

Do not add `types.py`, `models.py`, or `errors.py` dumping grounds. Each module
owns its own types. `deploy_scratch.py` and the current `types.py` sketch are not
part of the design.

## Import rule

The app runs as `/app/server.py`, with `/app` on `sys.path`; there are no
package `__init__.py` files in `rootfs/app`. Use sibling imports:

```python
import git_state
import ha_core
import supervisor
```

Not:

```python
from ha_deploy.rootfs.app import git_state  # wrong in the app runtime
```

Tests patch the module where the function is defined, e.g.
`git_state.gate_check`, `ha_core.stop_core`, `deploy.pull`.

## Core results

Flat unions; no generic `Result`.

```python
class CoreStopped(BaseModel):
    kind: Literal["core-stopped"] = "core-stopped"


class CoreStopFailed(BaseModel):
    kind: Literal["core-stop-failed"] = "core-stop-failed"
    reason: str


CoreStopResult = Annotated[
    CoreStopped | CoreStopFailed,
    Field(discriminator="kind"),
]


class CoreStarted(BaseModel):
    kind: Literal["core-started"] = "core-started"


class CoreStartFailed(BaseModel):
    kind: Literal["core-start-failed"] = "core-start-failed"
    reason: str


CoreStartResult = Annotated[
    CoreStarted | CoreStartFailed,
    Field(discriminator="kind"),
]
```

`stop_core() -> CoreStopResult`

`start_core() -> CoreStartResult`

`start_core()` is used for both the final start and recovery. There is no
separate `_restart_core()` wrapper and no state flag.

## Step results

```python
class GatePass(BaseModel):
    kind: Literal["gate-pass"] = "gate-pass"


class GateBlocked(BaseModel):
    kind: Literal["gate-blocked"] = "gate-blocked"
    reason: str
    state: GitState


GateVerdict = Annotated[
    GatePass | GateBlocked,
    Field(discriminator="kind"),
]
```

```python
class BackupCreated(BaseModel):
    kind: Literal["backup-created"] = "backup-created"
    slug: str


class BackupFailed(BaseModel):
    kind: Literal["backup-failed"] = "backup-failed"
    reason: str


BackupOutcome = Annotated[
    BackupCreated | BackupFailed,
    Field(discriminator="kind"),
]
```

```python
class Pulled(BaseModel):
    kind: Literal["pulled"] = "pulled"
    output: str


class PullFailed(BaseModel):
    kind: Literal["pull-failed"] = "pull-failed"
    output: str
    reason: str


PullOutcome = Annotated[
    Pulled | PullFailed,
    Field(discriminator="kind"),
]
```

```python
class VerifiedClean(BaseModel):
    kind: Literal["verified-clean"] = "verified-clean"


class VerifyFailed(BaseModel):
    kind: Literal["verify-failed"] = "verify-failed"
    modified: list[str]
    reason: str


VerifyOutcome = Annotated[
    VerifiedClean | VerifyFailed,
    Field(discriminator="kind"),
]
```

## The stopped window

After `stop_core()` succeeds, the body answers only "did gate2/pull/verify
succeed, and with what error?" It does not start the core.

```python
class WindowSucceeded(BaseModel):
    kind: Literal["window-succeeded"] = "window-succeeded"
    output: str


WindowOutcome = Annotated[
    WindowSucceeded | GateBlocked | PullFailed | VerifyFailed,
    Field(discriminator="kind"),
]
```

`WindowOutcome` reuses the step failure types, so no parallel set of
`WindowGateBlocked`/`WindowPullFailed`/etc. is needed.

```python
def _window_steps(env: dict[str, str]) -> WindowOutcome:
    match gate_check(env):
        case GatePass():
            match pull(env):
                case PullFailed():
                    return PullFailed(...)
                case Pulled(output=output):
                    match verify(env):
                        case VerifiedClean():
                            return WindowSucceeded(output=output)
                        case VerifyFailed():
                            return VerifyFailed(...)
        case GateBlocked():
            return GateBlocked(...)
```

No core logic here at all.

## Top-level outcome

The recovery result lives in the outcome variants that need it. No `CoreState`.

```python
class DeploySucceeded(BaseModel):
    kind: Literal["deploy-succeeded"] = "deploy-succeeded"
    output: str
    backup: str


class BlockedBeforeBackup(BaseModel):
    kind: Literal["blocked-before-backup"] = "blocked-before-backup"
    reason: str
    state: GitState


class BlockedByBackup(BaseModel):
    kind: Literal["blocked-by-backup"] = "blocked-by-backup"
    reason: str


class BlockedByStop(BaseModel):
    kind: Literal["blocked-by-stop"] = "blocked-by-stop"
    reason: str
    backup: str


class BlockedAfterStop(BaseModel):
    kind: Literal["blocked-after-stop"] = "blocked-after-stop"
    reason: str
    state: GitState
    backup: str
    restart: CoreStartResult


class BlockedByPull(BaseModel):
    kind: Literal["blocked-by-pull"] = "blocked-by-pull"
    reason: str
    output: str
    backup: str
    restart: CoreStartResult


class BlockedByVerify(BaseModel):
    kind: Literal["blocked-by-verify"] = "blocked-by-verify"
    reason: str
    output: str
    modified: list[str]
    backup: str
    restart: CoreStartResult


class BlockedByStart(BaseModel):
    kind: Literal["blocked-by-start"] = "blocked-by-start"
    reason: str
    output: str
    backup: str
```

Core status is read from the variant and the `restart` result:

- `BlockedBeforeBackup` / `BlockedByBackup` — core was never stopped.
- `BlockedByStop` — stop failed; no restart attempted; core state not confirmed.
- `BlockedAfterStop` / `BlockedByPull` / `BlockedByVerify` with
  `restart=CoreStarted` — error, core is running again.
- Same variants with `restart=CoreStartFailed` — error, core is not running.
- `BlockedByStart` — final start failed, including one explicit retry; core is
  not running.
- `DeploySucceeded` — success; core is running.

There is no boolean and no `CoreState` field.

## Orchestration

```python
def deploy() -> DeployOutcome:
    env = derive_git_env()
    match gate_check(env):
        case GateBlocked(reason=reason, state=state):
            return BlockedBeforeBackup(reason=reason, state=state)
        case GatePass():
            return _backup_phase(env)


def _backup_phase(env: dict[str, str]) -> DeployOutcome:
    match create_backup():
        case BackupFailed(reason=reason):
            return BlockedByBackup(reason=reason)
        case BackupCreated(slug=slug):
            return _stop_phase(slug, env)


def _stop_phase(backup: str, env: dict[str, str]) -> DeployOutcome:
    match stop_core():
        case CoreStopFailed(reason=reason):
            return BlockedByStop(reason=reason, backup=backup)
        case CoreStopped():
            return _after_stop(backup, env)
```

`_after_stop` is the bracket. It runs the body, then either performs the final
start (with one retry) or one recovery start. The combination functions are
pure:

```python
def _after_stop(backup: str, env: dict[str, str]) -> DeployOutcome:
    try:
        body = _window_steps(env)
    except BaseException:
        start_core()
        raise

    match body:
        case WindowSucceeded(output=output):
            return _finish_success(backup, output, _start_with_retry())

        case GateBlocked() | PullFailed() | VerifyFailed():
            return _finish_blocked(backup, body, start_core())

        case never:
            assert_never(never)
```

`_start_with_retry` is the only place that retries a failed final start:

```python
def _start_with_retry() -> CoreStartResult:
    match start_core():
        case CoreStarted():
            return CoreStarted()

        case CoreStartFailed(reason=reason):
            _LOGGER.warning(
                "DEPLOY: final start failed, retrying once: %s", reason
            )
            return start_core()

        case never:
            assert_never(never)
```

If the first final start fails and the retry succeeds, the caller sees
`CoreStarted`. If both fail, it sees `CoreStartFailed` carrying the retry's
reason; the first reason is logged.

```python
def _finish_success(
    backup: str,
    output: str,
    start: CoreStartResult,
) -> DeployOutcome:
    match start:
        case CoreStarted():
            return DeploySucceeded(output=output, backup=backup)

        case CoreStartFailed(reason=reason):
            return BlockedByStart(
                reason=reason, output=output, backup=backup
            )

        case never:
            assert_never(never)
```

```python
def _finish_blocked(
    backup: str,
    body: GateBlocked | PullFailed | VerifyFailed,
    restart: CoreStartResult,
) -> DeployOutcome:
    match body:
        case GateBlocked(reason=reason, state=state):
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
```

`_finish_success` and `_finish_blocked` have no IO, no logging, and no state.
They are covered by small matrices: success/failure × start/restart outcome.

## Printing

Presentation is a pure projection, not part of the outcome:

```python
def describe(outcome: DeployOutcome) -> str:
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
```

`_describe_restart` matches `CoreStarted` / `CoreStartFailed`. The route prints
or serializes the result; `describe` stays side-effect free and unit-testable.

## Behavior deltas

This design changes behavior in places that the current tests pin. Decisions
1 and 2 are settled; implement them as described.

1. **One start on blocked exits, not two (decided).** The current code restarts
   explicitly on blocked paths and then restarts again from `finally`. This
   design calls `start_core()` exactly once for a `GateBlocked`, `PullFailed`,
   or `VerifyFailed` body outcome. The corresponding tests move to `count == 1`.

2. **One explicit retry after a failed final start (decided, R).** When
   `_window_steps` succeeds, `_start_with_retry()` calls `start_core()`; if that
   fails, it calls `start_core()` once more. A successful retry returns
   `DeploySucceeded`; a second failure returns `BlockedByStart` with the retry's
   reason. The first failure is logged. Blocked-exit recovery still starts only
   once; the retry applies only to the final start.

3. **Exception path.** An unexpected exception after a successful stop still
   triggers one best-effort `start_core()` and then re-raises. The restart result
   is not returned because the exception path exits through the route's
   last-resort catch.

4. **More error detail in JSON.** `reason` is preserved on
   `BlockedBeforeBackup`, `BlockedAfterStop`, `BlockedByPull`, and
   `BlockedByVerify`; `restart` is added to the blocked-after-stop variants.
   These are additive payload changes.

## Testing strategy

- `_start_with_retry`: first start succeeds; first fails and retry succeeds;
  both fail. Assert exactly two `start_core` calls only when the first fails.
- `_finish_success`: pure matrix of `CoreStarted` / `CoreStartFailed` →
  `DeploySucceeded` / `BlockedByStart`.
- `_finish_blocked`: pure matrix of `GateBlocked` / `PullFailed` /
  `VerifyFailed` × restart success/failure.
- `_after_stop`: body success uses the retrying final start; blocked body uses
  exactly one restart; exception path starts once and re-raises.
- `_window_steps`: exact forwarding of `GateBlocked`, `PullFailed`, and
  `VerifyFailed`; success carries the pull output.
- Phase functions: one test per early-return point; assert no later step runs.
- `describe`: exact strings for success, each blocked variant, and both start
  results.
- Route tests: serialization only; no phase logic in `server.py`.
