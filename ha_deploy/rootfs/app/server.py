"""HA Deploy app: status check, deploy endpoint, ingress UI.

The live deploy implementation lives in deploy.py; this file keeps the HTTP
server, routes, and the superseded v2b deploy code (removed after the new
module is validated on the server).
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

import deploy as deploy_module

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)

GIT_DIR = "/homeassistant"
TEST_FILE = os.path.join(GIT_DIR, "ha-deploy-test.txt")
SSH_KEY = "/data/id_ed25519"
PORT = 8099
SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# UI served at GET /. Read once at startup; a missing file fails fast here
# rather than 500ing on every page load.
_INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_INDEX_FILE, encoding="utf-8") as f:
        INDEX_HTML = f.read()
except OSError as e:
    raise SystemExit(f"index.html missing next to server.py: {e}") from e


def run_git(args, env=None):
    """Run git in GIT_DIR. Returns (rc, stdout, stderr); never raises."""
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run(
            ["git", "-C", GIT_DIR, *args],
            capture_output=True,
            text=True,
            env=e,
            timeout=60,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return -1, "", str(ex)


def supervisor_request(method, path, data=None, timeout=30):
    """Defensive Supervisor API call: returns (code, parsed body); never raises."""
    url = f"{SUPERVISOR}{path}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return -1, {"error": str(e)}


def wait_for_core_state(target, timeout=60):
    """Poll /core/info until state matches target ('running'/'stopped')."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, info = supervisor_request("GET", "/core/info", timeout=10)
        if code == 200:
            state = info.get("data", {}).get("state", "")
            if state == target:
                return True, state
        time.sleep(2)
    code, info = supervisor_request("GET", "/core/info", timeout=10)
    state = info.get("data", {}).get("state", "unknown") if code == 200 else "unknown"
    return False, state


def git_status_summary():
    """Fetch + status -b; classifies the state (legacy copy; see deploy.py)."""
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    env = {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}

    rc, _out, err = run_git(["fetch", "origin"], env=env)
    fetch_ok = rc == 0
    fetch_error = err if not fetch_ok else ""

    _rc2, branch_out, _ = run_git(["status", "--porcelain=v1", "-b"])
    branch = ""
    ahead = behind = 0
    changes = []
    for line in branch_out.splitlines():
        if line.startswith("##"):
            branch = line[2:].strip()
            m = re.search(r"ahead (\d+)", line)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", line)
            if m:
                behind = int(m.group(1))
        elif line.strip():
            changes.append({"status": line[:2].strip(), "path": line[3:].strip()})

    if not fetch_ok:
        low = fetch_error.lower()
        if (
            "permission" in low
            or "auth" in low
            or "host key" in low
            or "could not read" in low
        ):
            summary = "auth error"
        else:
            summary = "fetch failed"
    elif ahead > 0 and behind > 0:
        summary = "diverged"
    elif behind > 0:
        summary = "behind origin"
    elif ahead > 0:
        summary = "ahead of origin"
    elif not changes:
        summary = "clean"
    else:
        statuses = {c["status"] for c in changes}
        if all(s == "??" for s in statuses):
            summary = "untracked only"
        elif all(s in ("A", "AM", "??") for s in statuses):
            summary = "only additions"
        elif all(s in ("D", "AD") for s in statuses):
            summary = "only deletions"
        else:
            summary = "contains updates"

    # gate: deploy allowed only if fetch_ok, ahead==0, no modified tracked files
    gate_ok = fetch_ok and ahead == 0 and not any(c["status"] != "??" for c in changes)

    return {
        "fetch_ok": fetch_ok,
        "fetch_error": fetch_error,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "summary": summary,
        "gate_ok": gate_ok,
    }


def gate_check():
    """Return (ok, reason, status_dict)."""
    st = git_status_summary()
    if not st["fetch_ok"]:
        return False, f"fetch failed: {st['fetch_error']}", st
    if st["ahead"] > 0:
        return (
            False,
            "local is ahead of origin (unpushed commits) - resolve manually or publish first",
            st,
        )
    if any(c["status"] != "??" for c in st["changes"]):
        return False, "modified tracked files present - resolve manually first", st
    return True, "clean", st


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
            result = git_status_summary()
            data = json.dumps(result).encode()
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
                result = {"ok": False, "error": f"unexpected server error: {e!r}"}
            data = json.dumps(result).encode()
            self.send_response(200 if result.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def _do_deploy(self):
        """Superseded v2b deploy implementation; /deploy uses deploy.py now."""
        # Gate check #1
        ok, reason, st = gate_check()
        if not ok:
            print(f"DEPLOY: gate #1 failed: {reason}", flush=True)
            return {"ok": False, "error": f"gate failed: {reason}", "status": st}
        print("DEPLOY: gate #1 passed", flush=True)

        ssh_cmd = (
            f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
        )
        env = {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}

        # Trigger full backup before deploy
        backup_name = None
        print("DEPLOY: triggering full backup", flush=True)
        code, info = supervisor_request(
            "POST", "/backups/new/full", {"name": "pre-deploy"}, timeout=300
        )
        if code == 200:
            backup_name = info.get("data", {}).get("slug") or info.get("data", {}).get(
                "name"
            )
            print(f"DEPLOY: backup created slug={backup_name}", flush=True)
        else:
            print(f"DEPLOY: backup failed code={code} info={info}", flush=True)
            return {"ok": False, "error": f"backup failed: {info}", "backup": None}

        core_stopped = False
        try:
            # Stop HA core
            print("DEPLOY: stopping core", flush=True)
            code, info = supervisor_request("POST", "/core/stop", timeout=30)
            if code not in (200, 201):
                print(f"DEPLOY: stop failed code={code} info={info}", flush=True)
                return {
                    "ok": False,
                    "error": f"failed to stop core: {info}",
                    "backup": backup_name,
                }
            ok_stop, state = wait_for_core_state("stopped", timeout=60)
            if not ok_stop:
                print(f"DEPLOY: core did not stop state={state}", flush=True)
                return {
                    "ok": False,
                    "error": f"core did not stop (state={state})",
                    "backup": backup_name,
                }
            core_stopped = True
            print("DEPLOY: core stopped", flush=True)

            # Gate check #2 (re-check after stop)
            ok2, reason2, st2 = gate_check()
            if not ok2:
                print(f"DEPLOY: post-stop gate failed: {reason2}", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=60)
                return {
                    "ok": False,
                    "error": f"post-stop gate failed: {reason2}",
                    "status": st2,
                    "backup": backup_name,
                }
            print("DEPLOY: post-stop gate passed", flush=True)

            # Git pull
            print("DEPLOY: running git pull", flush=True)
            rc, out, err = run_git(["pull"], env=env)
            pull_output = (out + ("\n" + err if err else "")).strip()
            print(f"DEPLOY: git pull rc={rc} output={pull_output!r}", flush=True)
            if rc != 0 or "CONFLICT" in (out + err) or "error:" in (out + err).lower():
                print("DEPLOY: git pull failed, restarting core", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=60)
                return {
                    "ok": False,
                    "error": "git pull failed",
                    "pull_output": pull_output,
                    "backup": backup_name,
                }

            # Post-pull verify
            _rc2, status_out, _ = run_git(["status", "--porcelain"])
            modified = [
                entry
                for entry in status_out.splitlines()
                if entry.strip() and not entry.startswith("??")
            ]
            if modified:
                print(
                    f"DEPLOY: unexpected tracked changes after pull: {modified}",
                    flush=True,
                )
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=60)
                return {
                    "ok": False,
                    "error": "unexpected tracked changes after pull",
                    "modified": modified,
                    "pull_output": pull_output,
                    "backup": backup_name,
                }
            print("DEPLOY: post-pull verify ok", flush=True)

            # Start HA core
            print("DEPLOY: starting core", flush=True)
            code, info = supervisor_request("POST", "/core/start", timeout=30)
            if code not in (200, 201):
                print(f"DEPLOY: start failed code={code} info={info}", flush=True)
                return {
                    "ok": False,
                    "error": f"failed to start core: {info}",
                    "pull_output": pull_output,
                    "backup": backup_name,
                }
            ok_start, state = wait_for_core_state("running", timeout=90)
            if not ok_start:
                print(f"DEPLOY: core did not start state={state}", flush=True)
                return {
                    "ok": False,
                    "error": f"core did not start (state={state})",
                    "pull_output": pull_output,
                    "backup": backup_name,
                }
            print("DEPLOY: core started successfully", flush=True)

            core_stopped = False
            return {
                "ok": True,
                "pull_output": pull_output,
                "backup": backup_name,
                "started": True,
            }

        finally:
            if core_stopped:
                print("DEPLOY: finally - restarting core", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=90)

    def log_message(self, fmt, *args):
        """Silence default per-request access logging."""


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    logging.getLogger(__name__).info("HA Deploy listening on %s", PORT)
    httpd.serve_forever()
