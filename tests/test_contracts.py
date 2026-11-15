"""Pydantic contract models for the step result shapes (dev-only, D decision).

These validate the *result contracts* that deploy() and the /deploy route rely
on, and the Supervisor envelope bodies the flow consumes. extra="forbid"
catches contract drift: a step that grows or renames a result field fails here
instead of quietly changing what the route answers with.
"""

import deploy
from pydantic import BaseModel, ConfigDict
import pytest


def forbid():
    return ConfigDict(extra="forbid")


class BackupResult(BaseModel):
    model_config = forbid()
    ok: bool
    slug: str | None
    code: int
    error: str | None


class CoreControlResult(BaseModel):
    model_config = forbid()
    ok: bool
    code: int
    error: str | None
    info: object | None


class PullResult(BaseModel):
    model_config = forbid()
    ok: bool
    rc: int
    pull_output: str
    error: str | None


class VerifyResult(BaseModel):
    model_config = forbid()
    ok: bool
    modified: list[str]
    error: str | None


class GateResult(BaseModel):
    model_config = forbid()
    ok: bool
    reason: str


class Change(BaseModel):
    model_config = forbid()
    status: str
    path: str


class GitStatusSummary(BaseModel):
    model_config = forbid()
    fetch_ok: bool
    fetch_error: str
    status_ok: bool
    status_error: str
    branch: str
    ahead: int
    behind: int
    changes: list[Change]
    summary: str
    gate_ok: bool


class SupervisorEnvelope(BaseModel):
    """The envelope most Supervisor endpoints answer with (data optional)."""

    model_config = ConfigDict(extra="ignore")
    result: str
    data: dict[str, object] | None = None
    message: str | None = None
    error_key: str | None = None
    job_id: str | None = None


# --- Supervisor envelope bodies the flow consumes ---------------------------


def test_backup_success_body_is_valid_envelope():
    body = {"result": "ok", "data": {"job_id": "e2b7", "slug": "e2b7a9c4"}}
    envelope = SupervisorEnvelope.model_validate(body)
    assert envelope.data is not None
    assert envelope.data["slug"] == "e2b7a9c4"


def test_backup_error_body_is_valid_envelope():
    body = {
        "result": "error",
        "message": "Backup already running",
        "error_key": "backup_running",
    }
    envelope = SupervisorEnvelope.model_validate(body)
    assert envelope.result == "error"
    assert envelope.message == "Backup already running"


def test_control_body_is_valid_envelope():
    SupervisorEnvelope.model_validate({"result": "ok", "data": {}})


# --- step result contracts (real functions, mocked low level) ---------------


def fake_supervisor(code, info):
    return lambda *a, **k: (code, info)


def fake_git(mapping):
    def _run_git(args, env=None):
        for prefix, result in mapping.items():
            if tuple(args[: len(prefix)]) == prefix:
                return result
        raise AssertionError(f"unexpected git call: {args}")

    return _run_git


def test_backup_result_contract_happy(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "supervisor_request",
        fake_supervisor(200, {"result": "ok", "data": {"slug": "e2b7a9c4"}}),
    )
    BackupResult.model_validate(deploy.create_backup())


def test_backup_result_contract_error(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(500, {"result": "error"})
    )
    result = deploy.create_backup()
    BackupResult.model_validate(result)
    assert result["ok"] is False and result["error"]


def test_stop_result_contract_happy(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"result": "ok", "data": {}})
    )
    CoreControlResult.model_validate(deploy.stop_core())


def test_stop_result_contract_error(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(502, {"result": "error"})
    )
    result = deploy.stop_core()
    CoreControlResult.model_validate(result)
    assert result["ok"] is False


def test_start_result_contract(monkeypatch):
    monkeypatch.setattr(
        deploy, "supervisor_request", fake_supervisor(200, {"result": "ok", "data": {}})
    )
    CoreControlResult.model_validate(deploy.start_core())


def test_pull_result_contract(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("pull",): (0, "Already up to date.", "")}),
    )
    PullResult.model_validate(deploy.pull({}))


def test_verify_result_contract(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git({("status", "--porcelain"): (0, "?? a.txt", "")}),
    )
    VerifyResult.model_validate(deploy.verify({}))


def test_git_status_summary_contract(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git(
            {
                ("fetch", "origin"): (0, "", ""),
                ("status", "--porcelain=v1", "-b"): (
                    0,
                    "## main...origin/main\n?? t.txt",
                    "",
                ),
            }
        ),
    )
    summary = deploy.git_status_summary({"GIT_SSH_COMMAND": "x"})
    GitStatusSummary.model_validate(summary)


def test_gate_check_contract(monkeypatch):
    monkeypatch.setattr(
        deploy,
        "run_git",
        fake_git(
            {
                ("fetch", "origin"): (0, "", ""),
                ("status", "--porcelain=v1", "-b"): (0, "## main...origin/main", ""),
            }
        ),
    )
    ok, reason, st = deploy.gate_check({})
    GateResult.model_validate({"ok": ok, "reason": reason})
    GitStatusSummary.model_validate(st)


def test_contract_drift_is_caught():
    # A result field renamed/added must fail the contract, not drift silently.
    with pytest.raises(ValueError):
        BackupResult.model_validate(
            {"ok": True, "slug": "x", "code": 200, "error": None, "surprise": 1}
        )
