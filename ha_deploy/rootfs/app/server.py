"""HA Deploy app: status check, deploy endpoint, ingress UI.

The live deploy implementation lives in deploy.py; this file keeps the HTTP
server and routes only. /deploy answers with a DeployOutcome (or RouteCrash
from the last-resort catch); /status answers with a StatusView.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os

import deploy as deploy_module
from typing_extensions import assert_never, override

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

GIT_DIR = "/homeassistant"
TEST_FILE = os.path.join(GIT_DIR, "ha-deploy-test.txt")
SSH_KEY = "/data/id_ed25519"
PORT = 8099

# UI served at GET /. Read once at startup; a missing file fails fast here
# rather than 500ing on every page load.
_INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_INDEX_FILE, encoding="utf-8") as f:
        INDEX_HTML = f.read()
except OSError as e:
    raise SystemExit(f"index.html missing next to server.py: {e}") from e


class Handler(BaseHTTPRequestHandler):
    """HTTP routes for the UI, status checks, and the deploy trigger."""

    def do_GET(self):
        """Route GET: /health, /debug, /status, /."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/debug":
            info = {
                "root": sorted(os.listdir("/")),
                "homeassistant_exists": os.path.exists(GIT_DIR),
                "ssh_key_exists": os.path.exists(SSH_KEY),
            }
            if os.path.exists(GIT_DIR):
                info["homeassistant_contents"] = sorted(os.listdir(GIT_DIR))
            data = json.dumps(info, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/status":
            result = deploy_module.status_view(deploy_module.derive_git_env())
            data = result.model_dump_json().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/":
            data = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """Route POST: /create, /deploy."""
        if self.path == "/create":
            if not os.path.exists(GIT_DIR):
                result = {"ok": False, "error": f"{GIT_DIR} not mounted"}
            else:
                try:
                    with open(TEST_FILE, "w") as f:
                        f.write("OHAI\n")
                    result = {"ok": True, "path": TEST_FILE}
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/deploy":
            # deploy() never lets errors escape its steps; this catch is the
            # last-resort guarantee that /deploy always answers with JSON.
            try:
                result = deploy_module.deploy()
            except Exception as e:
                logging.getLogger(__name__).exception(
                    "DEPLOY: unexpected error (last-resort catch)"
                )
                payload = (
                    deploy_module.RouteCrash(reason=repr(e)).model_dump_json().encode()
                )
                status = 500
            else:
                payload = result.model_dump_json().encode()
                match result:
                    case deploy_module.DeploySucceeded():
                        status = 200
                    case (
                        deploy_module.BlockedBeforeBackup()
                        | deploy_module.BlockedByBackup()
                        | deploy_module.BlockedByStop()
                        | deploy_module.BlockedAfterStop()
                        | deploy_module.BlockedByPull()
                        | deploy_module.BlockedByVerify()
                        | deploy_module.BlockedByStart()
                    ):
                        status = 500
                    case never:
                        assert_never(never)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    @override
    def log_message(self, format: str, *args: object) -> None:
        """Silence default per-request access logging."""


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    logging.getLogger(__name__).info("HA Deploy listening on %s", PORT)
    httpd.serve_forever()
