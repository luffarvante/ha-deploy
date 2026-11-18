"""Tests for core start/stop outcome mapping."""

import ha_core
import supervisor


def fake_supervisor(outcome):
    def handler(method, path, body, timeout):
        return outcome

    return handler


# --- stop / start -------------------------------------------------------------


def test_stop_ok(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorOk(code=200, body={"result": "ok"})),
    )
    assert ha_core.stop_core() == ha_core.CoreStopped()


def test_stop_api_error(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorError(code=502, body={"result": "error"})),
    )
    outcome = ha_core.stop_core()
    assert isinstance(outcome, ha_core.CoreStopFailed)
    assert "502" in outcome.reason


def test_stop_2xx_unexpected_body_fails(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorBodyMismatch(code=200, raw="weird")),
    )
    outcome = ha_core.stop_core()
    assert isinstance(outcome, ha_core.CoreStopFailed)
    assert "non-JSON" in outcome.reason


def test_stop_unreachable(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorUnreachable(reason="timeout")),
    )
    outcome = ha_core.stop_core()
    assert isinstance(outcome, ha_core.CoreStopFailed)
    assert "unreachable" in outcome.reason


def test_start_ok(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorOk(code=200, body={"result": "ok"})),
    )
    assert ha_core.start_core() == ha_core.CoreStarted()


def test_start_api_error(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorError(code=500, body={"result": "error"})),
    )
    outcome = ha_core.start_core()
    assert isinstance(outcome, ha_core.CoreStartFailed)
    assert "500" in outcome.reason


def test_start_unreachable(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "supervisor_request",
        fake_supervisor(supervisor.SupervisorUnreachable(reason="timeout")),
    )
    outcome = ha_core.start_core()
    assert isinstance(outcome, ha_core.CoreStartFailed)
    assert "unreachable" in outcome.reason
