# import json
# import os
# import re
# import subprocess
# import time
# import urllib.request
# import urllib.error
# from http.server import BaseHTTPRequestHandler, HTTPServer

import subprocess
import os
import json
import urllib.request
import urllib.error

SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


# ToNote: poorly copy-pasted from interactive shell
# SUPERVISOR = "http://supervisor"
# def supervisor_request(method, path, data=None, timeout=30):
#     url = f"{SUPERVISOR}{path}"
#     headers = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
#     body = None
#     if data is not None:
#         body = json.dumps(data).encode()
#         headers["Content-Type"] = "application/json"
#     req = urllib.request.Request(url, data=body, headers=headers, method=me \
#     return resp.status, json.loads(resp.read().decode())
#

# import json
# import urllib.request
# import urllib.error
#

def supervisor_request(method, path, data=None, timeout=30):
    url = f"{SUPERVISOR}{path}"
    headers = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def sureq(met, point, timeout=10):
    start = time.time()
    if met == "GET":
        code, info = supervisor_request(met, point, timeout=timeout)
    else:
        code, info = supervisor_request(met, point, data={}, timeout=timeout)
    end = time.time()
    duration = end - start
    print(code, info)
    print("duration:", duration)
    if code == 200:
        print("  >> SUCCESS!")
        return True
    print("  >> FAILURE!")
    return False


def stop():
    print("Stopping HA core")
    return sureq("POST", "/core/stop", timeout=30)


def start():
    print("Starting HA core")
    return sureq("POST", "/core/start", timeout=30)


def status():
    print("Checking HA core status")
    return sureq("GET", "/core/api/core/state")


def git_fetch_check():
    # ToNote: copy-paste from `#gate_check`
    """Return (ok, reason, status_dict)."""
    st, env = git_status_summary()
    if not st["fetch_ok"]:
        return False, f"fetch failed: {st['fetch_error']}", st, None
    if st["ahead"] > 0:
        return False, "local is ahead of origin (unpushed commits) - resolve manually or publish first", st, None
    if any(c["status"] != "??" for c in st["changes"]):
        return False, "modified tracked files present - resolve manually first", st, None
    return True, "clean", st, env




def git_status_summary():
    if not os.path.exists(SSH_KEY):
        raise ValueError("SSH KEY MISSING")
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    env = {"GIT_SSH_COMMAND": ssh_cmd}
    rc, out, err = run_git(["fetch", "origin"], env=env)
    fetch_ok = (rc == 0)
    fetch_error = err if not fetch_ok else ""

    rc2, branch_out, _ = run_git(["status", "--porcelain=v1", "-b"], env=env)
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
    }, env

GIT_DIR = "/homeassistant"

def run_git(args, env=None):
    if not env:
        raise ValueError("MISSING ENV")

    e = dict(os.environ)
    e.update(env)

    try:
        r = subprocess.run(["git", "-C", GIT_DIR] + args, capture_output=True, text=True, env=e, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return -1, "", str(ex)


def do_deploy():
    # step by step test git pull
    # ToNote: real script logs we are starting
    print("STEP 1: Git fetch check before anything else")
    pre_check = git_fetch_check()
    pc_ok, pc_reason, pc_st, env = pre_check
    if not pc_ok:
        print("ERROR git fetch check is big fail")
        print("  reason:", pc_reason)
        print("  st:", pc_st)
        return "FAIL"

    print("pre_check is OK! (reason: {}, status: {})".format(pc_reason, pc_st))

    print("\nSTEP 2: Here would be an actual, full backup (skipped in this interactive check)")
    print("\nSTEP 3: Stop HA core")
    if not stop():
        print("BIG FAIL!")
        return "FAIL"

    print("\nSTEP 3: Stop HA core")
    after_stop_check = git_fetch_check()
    asc_ok, asc_reason, asc_st, env = after_stop_check
    print("after stop check: (is_ok: {}, reason: {}, status: {})".format(asc_ok, pc_reason, pc_st))
    if not asc_ok:
        print("FAIL FAIL!")
        print("but starting at least")
        if not start():
            print("that also failed, oh noes!!")
            return "FAIL"
        return "FAIL"

    print("after sync check was fine!")
    pull_result, debugerry = git_pull(env)
    print("pull result:", pull_result)
    if not pull_result:
        print("FAIL!!")
        print("Debug:", debugerry)
        return "FAIL"
    print("Pull success!!")

    print("And we start it again")
    if not start():
        print("BUT THAT FAILED!")
        sttat = status()
        print("what even is status?", sttat)
        return "FAIL"
    print("\nEND END END")
    return "OK"

SSH_KEY = "/data/id_ed25519"

def git_pull(env):
    print("DEPLOY: running git pull", flush=True)
    rc, out, err = run_git(["pull"], env=env)
    pull_output = (out + ("\n" + err if err else "")).strip()
    print(f"DEPLOY: git pull rc={rc} output={pull_output!r}", flush=True)
    if rc != 0 or "CONFLICT" in (out + err) or "error:" in (out + err).lower():
        print("DEPLOY: git pull failed, restarting core", flush=True)
        supervisor_request("POST", "/core/start", timeout=30)
        wait_for_core_state("running", timeout=60)
        return False, {"ok": False, "error": "git pull failed", "pull_output": pull_output, "backup": backup_name}

    rc2, status_out, _ = run_git(["status", "--porcelain"], env=env)
    modified = [l for l in status_out.splitlines() if l.strip() and not l.startswith("??")]
    if modified:
        print(f"DEPLOY: unexpected tracked changes after pull: {modified}", flush=True)
        supervisor_request("POST", "/core/start", timeout=30)
        wait_for_core_state("running", timeout=60)
        return False, {"ok": False, "error": "unexpected tracked changes after pull",
                "modified": modified, "pull_output": pull_output, "backup": backup_name}
    print("DEPLOY: post-pull verify ok", flush=True)
    return True, {}


dd_result = do_deploy()
print("deploy resulted in:", dd_result)
