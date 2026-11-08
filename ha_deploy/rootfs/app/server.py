import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

GIT_DIR = "/homeassistant"
TEST_FILE = os.path.join(GIT_DIR, "ha-deploy-test.txt")
SSH_KEY = "/data/id_ed25519"
PORT = 8099
SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>HA Deploy</title></head>
<body>
  <h1>HA Deploy</h1>
  <p>Status check runs automatically on load.</p>
  <button id="refresh">Refresh status</button>
  <span id="timer"></span>
  <pre id="out">Loading...</pre>
  <hr>
  <button id="deploy" disabled>Deploy (pull config from Git to server)</button>
  <span id="deploy-status"></span>
  <pre id="deploy-out"></pre>
  <hr>
  <button id="btn">Create test file</button>
  <pre id="out2"></pre>
  <script>
    let lastCheck = null;
    let lastStatus = null;
    function updateTimer() {
      if (lastCheck) {
        const s = Math.floor((Date.now() - lastCheck) / 1000);
        document.getElementById('timer').textContent = ' (checked ' + s + 's ago)';
      }
    }
    setInterval(updateTimer, 1000);
    function setDeployEnabled(status) {
      const btn = document.getElementById('deploy');
      const gateOk = status && status.fetch_ok && status.ahead === 0
        && !status.changes.some(c => c.status !== '??');
      btn.disabled = !gateOk;
      document.getElementById('deploy-status').textContent = gateOk
        ? ' (ready)' : ' (gate not clean - deploy disabled)';
    }
    async function checkStatus() {
      const out = document.getElementById('out');
      out.textContent = 'Checking...';
      try {
        const r = await fetch('status');
        const j = await r.json();
        lastCheck = Date.now();
        lastStatus = j;
        updateTimer();
        out.textContent = JSON.stringify(j, null, 2);
        setDeployEnabled(j);
      } catch (e) {
        out.textContent = 'Error: ' + e;
        document.getElementById('deploy').disabled = true;
      }
    }
    document.getElementById('refresh').onclick = checkStatus;
    document.getElementById('deploy').onclick = async () => {
      if (!confirm('This will restart Home Assistant. Continue?')) return;
      const btn = document.getElementById('deploy');
      const out = document.getElementById('deploy-out');
      btn.disabled = true;
      out.textContent = 'Deploying (backup -> stop -> pull -> start)...';
      try {
        const r = await fetch('deploy', { method: 'POST' });
        const j = await r.json();
        out.textContent = JSON.stringify(j, null, 2);
      } catch (e) {
        out.textContent = 'Error: ' + e;
      }
      await checkStatus();
    };
    document.getElementById('btn').onclick = async () => {
      const out = document.getElementById('out2');
      out.textContent = 'Working...';
      try {
        const r = await fetch('create', { method: 'POST' });
        const j = await r.json();
        out.textContent = JSON.stringify(j, null, 2);
      } catch (e) {
        out.textContent = 'Error: ' + e;
      }
    };
    checkStatus();
  </script>
</body>
</html>
"""


def run_git(args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run(["git", "-C", GIT_DIR] + args, capture_output=True, text=True, env=e, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return -1, "", str(ex)


def supervisor_request(method, path, data=None, timeout=30):
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
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    env = {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}

    rc, out, err = run_git(["fetch", "origin"], env=env)
    fetch_ok = (rc == 0)
    fetch_error = err if not fetch_ok else ""

    rc2, branch_out, _ = run_git(["status", "--porcelain=v1", "-b"])
    branch = ""
    ahead = behind = 0
    changes = []
    for line in branch_out.splitlines():
        if line.startswith("##"):
            branch = line[2:].strip()
            import re
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
        if "permission" in low or "auth" in low or "host key" in low or "could not read" in low:
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
        return False, "local is ahead of origin (unpushed commits) - resolve manually or publish first", st
    if any(c["status"] != "??" for c in st["changes"]):
        return False, "modified tracked files present - resolve manually first", st
    return True, "clean", st


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/debug":
            info = {"root": sorted(os.listdir("/")), "homeassistant_exists": os.path.exists(GIT_DIR),
                    "ssh_key_exists": os.path.exists(SSH_KEY)}
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
        if self.path == "/create":
            try:
                if not os.path.exists(GIT_DIR):
                    raise FileNotFoundError(f"{GIT_DIR} not mounted")
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
            result = self._do_deploy()
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
        # Gate check #1
        ok, reason, st = gate_check()
        if not ok:
            print(f"DEPLOY: gate #1 failed: {reason}", flush=True)
            return {"ok": False, "error": f"gate failed: {reason}", "status": st}
        print("DEPLOY: gate #1 passed", flush=True)

        ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
        env = {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}

        # Trigger full backup before deploy
        backup_name = None
        print("DEPLOY: triggering full backup", flush=True)
        code, info = supervisor_request("POST", "/backups/new/full", {"name": "pre-deploy"}, timeout=300)
        if code == 200:
            backup_name = info.get("data", {}).get("slug") or info.get("data", {}).get("name")
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
                return {"ok": False, "error": f"failed to stop core: {info}", "backup": backup_name}
            ok_stop, state = wait_for_core_state("stopped", timeout=60)
            if not ok_stop:
                print(f"DEPLOY: core did not stop state={state}", flush=True)
                return {"ok": False, "error": f"core did not stop (state={state})", "backup": backup_name}
            core_stopped = True
            print("DEPLOY: core stopped", flush=True)

            # Gate check #2 (re-check after stop)
            ok2, reason2, st2 = gate_check()
            if not ok2:
                print(f"DEPLOY: post-stop gate failed: {reason2}", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=60)
                return {"ok": False, "error": f"post-stop gate failed: {reason2}", "status": st2, "backup": backup_name}
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
                return {"ok": False, "error": "git pull failed", "pull_output": pull_output, "backup": backup_name}

            # Post-pull verify
            rc2, status_out, _ = run_git(["status", "--porcelain"])
            modified = [l for l in status_out.splitlines() if l.strip() and not l.startswith("??")]
            if modified:
                print(f"DEPLOY: unexpected tracked changes after pull: {modified}", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=60)
                return {"ok": False, "error": "unexpected tracked changes after pull",
                        "modified": modified, "pull_output": pull_output, "backup": backup_name}
            print("DEPLOY: post-pull verify ok", flush=True)

            # Start HA core
            print("DEPLOY: starting core", flush=True)
            code, info = supervisor_request("POST", "/core/start", timeout=30)
            if code not in (200, 201):
                print(f"DEPLOY: start failed code={code} info={info}", flush=True)
                return {"ok": False, "error": f"failed to start core: {info}", "pull_output": pull_output, "backup": backup_name}
            ok_start, state = wait_for_core_state("running", timeout=90)
            if not ok_start:
                print(f"DEPLOY: core did not start state={state}", flush=True)
                return {"ok": False, "error": f"core did not start (state={state})", "pull_output": pull_output, "backup": backup_name}
            print("DEPLOY: core started successfully", flush=True)

            core_stopped = False
            return {"ok": True, "pull_output": pull_output, "backup": backup_name, "started": True}

        finally:
            if core_stopped:
                print("DEPLOY: finally - restarting core", flush=True)
                supervisor_request("POST", "/core/start", timeout=30)
                wait_for_core_state("running", timeout=90)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"HA Deploy listening on {PORT}", flush=True)
    httpd.serve_forever()
