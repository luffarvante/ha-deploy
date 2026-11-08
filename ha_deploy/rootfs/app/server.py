import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

GIT_DIR = "/homeassistant"
TEST_FILE = os.path.join(GIT_DIR, "ha-deploy-test.txt")
SSH_KEY = "/data/id_ed25519"
PORT = 8099

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
  <button id="btn">Create test file</button>
  <pre id="out2"></pre>
  <script>
    let lastCheck = null;
    function updateTimer() {
      if (lastCheck) {
        const s = Math.floor((Date.now() - lastCheck) / 1000);
        document.getElementById('timer').textContent = ' (checked ' + s + 's ago)';
      }
    }
    setInterval(updateTimer, 1000);
    async function checkStatus() {
      const out = document.getElementById('out');
      out.textContent = 'Checking...';
      try {
        const r = await fetch('status');
        const j = await r.json();
        lastCheck = Date.now();
        updateTimer();
        out.textContent = JSON.stringify(j, null, 2);
      } catch (e) {
        out.textContent = 'Error: ' + e;
      }
    }
    document.getElementById('refresh').onclick = checkStatus;
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
        r = subprocess.run(["git", "-C", GIT_DIR] + args, capture_output=True, text=True, env=e, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return -1, "", str(ex)


def git_status_summary():
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    env = {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}

    # fetch
    rc, out, err = run_git(["fetch", "origin"], env=env)
    fetch_ok = (rc == 0)
    fetch_error = err if not fetch_ok else ""

    # branch + ahead/behind
    rc2, branch_out, _ = run_git(["status", "--porcelain=v1", "-b"])
    branch = ""
    ahead = behind = 0
    for line in branch_out.splitlines():
        if line.startswith("##"):
            branch = line[2:].strip()
            # parse "main...origin/main [ahead 1, behind 2]"
            import re
            m = re.search(r"ahead (\d+)", line)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", line)
            if m:
                behind = int(m.group(1))
            break

    # changed files
    changes = []
    for line in branch_out.splitlines():
        if line.startswith("##"):
            continue
        if not line.strip():
            continue
        status = line[:2].strip()
        path = line[3:].strip()
        changes.append({"status": status, "path": path})

    # summary classification
    if not fetch_ok:
        summary = "auth error" if ("permission" in fetch_error.lower() or "auth" in fetch_error.lower() or "host key" in fetch_error.lower()) else "fetch failed"
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
        # untracked files show as "??"
        if all(s == "??" for s in statuses):
            summary = "untracked only"
        elif all(s in ("A", "AM", "??") for s in statuses):
            summary = "only additions"
        elif all(s in ("D", "AD") for s in statuses):
            summary = "only deletions"
        else:
            summary = "contains updates"

    return {
        "fetch_ok": fetch_ok,
        "fetch_error": fetch_error,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "summary": summary,
    }


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
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"HA Deploy listening on {PORT}", flush=True)
    httpd.serve_forever()
