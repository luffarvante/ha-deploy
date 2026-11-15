"""Tests for supervisor_request: HTTP/network outcomes as sum types."""

import email.message
import io
import json
import urllib.error

import deploy


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://supervisor/test",
        code,
        "boom",
        email.message.Message(),
        io.BytesIO(body.encode()),
    )


def ok_200():
    return FakeResponse(200, json.dumps({"result": "ok", "data": {}}))


def test_2xx_json_is_parsed(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request, "urlopen", lambda req, timeout=None: ok_200()
    )
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorOk)
    assert outcome.code == 200
    assert outcome.body == {"result": "ok", "data": {}}


def test_2xx_non_json_body_is_body_mismatch(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: FakeResponse(200, "not json"),
    )
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorBodyMismatch)
    assert outcome.code == 200
    assert outcome.raw == "not json"


def test_2xx_array_body_passes_through_untouched(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: FakeResponse(200, "[]"),
    )
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorOk)
    assert outcome.body == []


def test_post_body_is_sent_as_json(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["content_type"] = req.headers.get("Content-type")
        return ok_200()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    outcome = deploy.supervisor_request("POST", "/test", {"name": "x"}, 30)
    assert isinstance(outcome, deploy.SupervisorOk)
    assert json.loads(captured["data"]) == {"name": "x"}
    assert captured["content_type"] == "application/json"


def test_no_body_sends_no_data_and_no_content_type(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["content_type"] = req.headers.get("Content-type")
        return ok_200()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    deploy.supervisor_request("POST", "/test", None, 30)
    assert captured["data"] is None
    assert captured["content_type"] is None


def test_bearer_header_is_sent(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return ok_200()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    deploy.supervisor_request("GET", "/test", None, 30)
    assert captured["auth"] == f"Bearer {deploy.SUPERVISOR_TOKEN}"


def test_non_2xx_with_json_body_is_error_variant(monkeypatch):
    def raise_http_error(req, timeout=None):
        raise http_error(502, json.dumps({"result": "error", "message": "down"}))

    monkeypatch.setattr(deploy.urllib.request, "urlopen", raise_http_error)
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorError)
    assert outcome.code == 502
    assert outcome.body == {"result": "error", "message": "down"}


def test_non_2xx_with_non_json_body_is_body_mismatch(monkeypatch):
    def raise_http_error(req, timeout=None):
        raise http_error(500, "oops")

    monkeypatch.setattr(deploy.urllib.request, "urlopen", raise_http_error)
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorBodyMismatch)
    assert outcome.code == 500
    assert outcome.raw == "oops"


def test_non_2xx_with_unreadable_body_is_body_mismatch(monkeypatch):
    def raise_on_read():
        raise OSError("no body")

    err = urllib.error.HTTPError(
        "http://supervisor/test", 503, "boom", email.message.Message(), None
    )
    monkeypatch.setattr(err, "read", raise_on_read)

    def raise_http_error(req, timeout=None):
        raise err

    monkeypatch.setattr(deploy.urllib.request, "urlopen", raise_http_error)
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorBodyMismatch)
    assert outcome.code == 503
    assert outcome.raw == ""


def test_network_error_is_unreachable_variant(monkeypatch):
    def refused(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(deploy.urllib.request, "urlopen", refused)
    outcome = deploy.supervisor_request("GET", "/test", None, 30)
    assert isinstance(outcome, deploy.SupervisorUnreachable)
    assert "connection refused" in outcome.reason
