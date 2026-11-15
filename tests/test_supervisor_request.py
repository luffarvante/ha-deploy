"""Mapping tests for supervisor_request (defensive wrapper, never raises)."""

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
        "http://supervisor/test", code, "boom", None, io.BytesIO(body.encode())
    )


def ok_200():
    return FakeResponse(200, json.dumps({"result": "ok", "data": {}}))


def test_2xx_json_is_parsed(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request, "urlopen", lambda req, timeout=None: ok_200()
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == 200
    assert info == {"result": "ok", "data": {}}


def test_post_data_is_sent_as_json(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["content_type"] = req.headers.get("Content-type")
        return ok_200()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    code, _info = deploy.supervisor_request("POST", "/test", {"name": "x"})
    assert code == 200
    assert json.loads(captured["data"]) == {"name": "x"}
    assert captured["content_type"] == "application/json"


def test_non_2xx_with_json_body_is_parsed(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            http_error(502, json.dumps({"result": "error", "message": "down"}))
        ),
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == 502
    assert info == {"result": "error", "message": "down"}


def test_non_2xx_with_non_json_body_maps_to_error_value(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(http_error(500, "oops")),
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == 500
    assert info["error"] == "non-JSON error body"
    assert info["raw"] == "oops"


def test_non_2xx_with_unreadable_body_maps_to_error_value(monkeypatch):
    def raise_on_read():
        raise OSError("no body")

    err = urllib.error.HTTPError("http://supervisor/test", 503, "boom", None, None)
    monkeypatch.setattr(err, "read", raise_on_read)
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(err),
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == 503
    assert info["error"] == "non-JSON error body"


def test_network_error_maps_to_code_minus_one(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.URLError("connection refused")
        ),
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == -1
    assert "connection refused" in info["error"]


def test_2xx_with_non_json_body_maps_to_body_mismatch_value(monkeypatch):
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda req, timeout=None: FakeResponse(200, "<html>not json</html>"),
    )
    code, info = deploy.supervisor_request("GET", "/test")
    assert code == 200
    assert info["error"] == "non-JSON response body"
    assert info["raw"] == "<html>not json</html>"


def test_bearer_token_header_is_set(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return ok_200()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake_urlopen)
    deploy.supervisor_request("GET", "/test")
    assert captured["auth"] == f"Bearer {deploy.SUPERVISOR_TOKEN}"
