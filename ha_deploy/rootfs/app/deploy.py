"""HA Deploy - clean deploy module.

Implements the validated deploy flow:
  gate #1 -> backup -> stop core -> gate #2 -> pull -> verify -> start core
with a try/finally restart safety net.

Design decisions (from the plan):
  - supervisor_request is defensive: it never raises. Network/timeout errors
    return (code=-1, {"error": ...}). HTTPError bodies are parsed so a 502
    body reaches the log. A non-JSON body is mapped to a body-mismatch value
    instead of raising.
  - One function per step. Each step catches its own exceptions and maps them
    into a structured result: {"ok": bool, "error": str|None, ...} plus
    step-specific fields. Errors never escape a step function.
  - deploy() is the single entrypoint: it owns core_stopped (set immediately
    after a successful stop) and the recovery decisions, and wraps everything
    after a successful stop in try/finally.
  - The /deploy route adds one last-resort catch: guarantee a JSON response,
    log, no recovery.

Outcome mapping references (see plan):
  - Supervisor API calls: 2xx -> ok; non-2xx / network (code=-1) -> fail;
    2xx-but-unexpected-body -> fail.
  - Git calls: rc=0 -> ok (pull additionally requires no CONFLICT / error: in
    the output); rc!=0 or run_git exception -> fail.
  - SSH key missing -> silent fallback to env={} (decided); the fetch then
    fails and surfaces as an ordinary fetch/auth error.
"""

import json
import os
import re
import subprocess
import urllib.request
import urllib.error
from typing import Any, TypedDict

GIT_DIR = "/homeassistant"
SSH_KEY = "/data/id_ed25519"
SUPERVISOR = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

BACKUP_NAME = "pre-deploy"


# --- result contracts (mirror tests/test_contracts.py) ----------------------


class ChangeDict(TypedDict):
    status: str
    path: str


class StatusSummary(TypedDict):
    fetch_ok: bool
    fetch_error: str
    status_ok: bool
    status_error: str
    branch: str
    ahead: int
    behind: int
    changes: list[ChangeDict]
    summary: str
    gate_ok: bool


class BackupResult(TypedDict):
    ok: bool
    slug: str | None
    code: int
    error: str | None


class ControlResult(TypedDict):
    ok: bool
    code: int
    error: str | None
    info: object | None


class PullResult(TypedDict):
    ok: bool
    rc: int
    pull_output: str
    error: str | None


class VerifyResult(TypedDict):
    ok: bool
    modified: list[str]
    error: str | None


# --- infrastructure ---------------------------------------------------------


def supervisor_request(
    method: str, path: str, data: dict[str, Any] | None = None, timeout: float = 30
) -> tuple[int, dict[str, Any]]:
    """Defensive Supervisor API call: returns (code, parsed_body); never raises.

    code=-1 on network/timeout errors (counts as fail). HTTPError bodies are
    parsed so a non-2xx body reaches the log. A non-JSON body is returned as
    a body-mismatch value.
    """
    url = f"{SUPERVISOR}{path}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw: str = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode(errors="replace")
        except Exception:
            pass
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except Exception:
            return e.code, {"error": "non-JSON error body", "raw": raw[:500]}
        return e.code, parsed
    except Exception as e:
        return -1, {"error": str(e)}
    try:
        parsed = json.loads(raw)
    except Exception:
        return status, {"error": "non-JSON response body", "raw": raw[:500]}
    return status, parsed


def run_git(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run git in GIT_DIR. Returns (rc, stdout, stderr); never raises."""
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run(
            ["git", "-C", GIT_DIR] + args,
            capture_output=True, text=True, env=e, timeout=60,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as ex:
        return -1, "", str(ex)


def derive_git_env() -> dict[str, str]:
    """GIT_SSH_COMMAND env for all git calls in this deploy, derived once.

    SSH key missing -> silent fallback to {} (decided, D3): the fetch then
    fails and surfaces as an ordinary fetch/auth error.
    """
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    return {"GIT_SSH_COMMAND": ssh_cmd} if os.path.exists(SSH_KEY) else {}


# --- git state --------------------------------------------------------------


def git_status_summary(env: dict[str, str]) -> StatusSummary:
    """git fetch origin + git status --porcelain=v1 -b; classifies the state."""
    rc, out, err = run_git(["fetch", "origin"], env=env)
    fetch_ok = (rc == 0)
    fetch_error = err if not fetch_ok else ""

    rc2, branch_out, err2 = run_git(["status", "--porcelain=v1", "-b"], env=env)
    status_ok = (rc2 == 0)
    status_error = err2 if not status_ok else ""
    branch = ""
    ahead = 0
    behind = 0
    changes: list[ChangeDict] = []
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

    summary: str
    if not fetch_ok:
        low = fetch_error.lower()
        if "permission" in low or "auth" in low or "host key" in low or "could not read" in low:
            summary = "auth error"
        else:
            summary = "fetch failed"
    elif not status_ok:
        summary = "status failed"
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

    # gate: deploy allowed only if fetch_ok, status_ok, ahead==0, no modified tracked files
    gate_ok = (
        fetch_ok and status_ok
        and ahead == 0
        and not any(c["status"] != "??" for c in changes)
    )

    return {
        "fetch_ok": fetch_ok,
        "fetch_error": fetch_error,
        "status_ok": status_ok,
        "status_error": status_error,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "summary": summary,
        "gate_ok": gate_ok,
    }


def gate_check(env: dict[str, str]) -> tuple[bool, str, StatusSummary]:
    """Gate rule: fetch_ok AND status_ok AND ahead==0 AND no non-?? changes.

    Returns (ok, reason, status_dict).
    """
    st = git_status_summary(env)
    if not st["fetch_ok"]:
        return False, f"fetch failed: {st['fetch_error']}", st
    if not st["status_ok"]:
        return False, f"status failed: {st['status_error']}", st
    if st["ahead"] > 0:
        return False, "local is ahead of origin (unpushed commits) - resolve manually or publish first", st
    if any(c["status"] != "??" for c in st["changes"]):
        return False, "modified tracked files present - resolve manually first", st
    return True, "clean", st


# --- steps ------------------------------------------------------------------


def create_backup() -> BackupResult:
    """POST /backups/new/full. Blocks until the backup is done (decided, D2).

    Returns {"ok", "slug", "code", "error"}.
    """
    result: BackupResult = {"ok": False, "slug": None, "code": -1, "error": None}
    try:
        print("DEPLOY: creating full backup", flush=True)
        code, info = supervisor_request(
            "POST", "/backups/new/full", {"name": BACKUP_NAME}, timeout=300
        )
        result["code"] = code
        if not (200 <= code < 300):
            result["error"] = f"backup API returned {code}: {info}"
            print(f"DEPLOY: backup failed code={code} info={info}", flush=True)
            return result
        data = info.get("data") if isinstance(info, dict) else None
        slug = (
            (data.get("slug") or data.get("name"))
            if isinstance(data, dict)
            else None
        )
        if not slug:
            result["error"] = f"backup API returned unexpected body (no slug): {info}"
            print(f"DEPLOY: backup body mismatch: {info}", flush=True)
            return result
        result["ok"] = True
        result["slug"] = slug
        print(f"DEPLOY: backup created slug={slug}", flush=True)
        return result
    except Exception as ex:
        result["error"] = f"unexpected error in create_backup: {ex!r}"
        print(f"DEPLOY: create_backup unexpected error: {ex!r}", flush=True)
        return result


def stop_core() -> ControlResult:
    """POST /core/stop - synchronous control call (2xx == stopped, no polling).

    Returns {"ok", "code", "error", "info"}.
    """
    result: ControlResult = {"ok": False, "code": -1, "error": None, "info": None}
    try:
        code, info = supervisor_request("POST", "/core/stop", timeout=30)
        result["code"] = code
        result["info"] = info
        if not (200 <= code < 300):
            result["error"] = f"core stop returned {code}: {info}"
            print(f"DEPLOY: stop failed code={code} info={info}", flush=True)
            return result
        result["ok"] = True
        return result
    except Exception as ex:
        result["error"] = f"unexpected error in stop_core: {ex!r}"
        print(f"DEPLOY: stop_core unexpected error: {ex!r}", flush=True)
        return result


def start_core() -> ControlResult:
    """POST /core/start - synchronous control call (2xx == started, no polling).

    Returns {"ok", "code", "error", "info"}.
    """
    result: ControlResult = {"ok": False, "code": -1, "error": None, "info": None}
    try:
        code, info = supervisor_request("POST", "/core/start", timeout=30)
        result["code"] = code
        result["info"] = info
        if not (200 <= code < 300):
            result["error"] = f"core start returned {code}: {info}"
            print(f"DEPLOY: start failed code={code} info={info}", flush=True)
            return result
        result["ok"] = True
        return result
    except Exception as ex:
        result["error"] = f"unexpected error in start_core: {ex!r}"
        print(f"DEPLOY: start_core unexpected error: {ex!r}", flush=True)
        return result


def pull(env: dict[str, str]) -> PullResult:
    """git pull. rc=0 AND no CONFLICT AND no error: in output -> ok.

    Returns {"ok", "rc", "pull_output", "error"}.
    """
    result: PullResult = {"ok": False, "rc": -1, "pull_output": "", "error": None}
    try:
        print("DEPLOY: running git pull", flush=True)
        rc, out, err = run_git(["pull"], env=env)
        pull_output = (out + ("\n" + err if err else "")).strip()
        result["rc"] = rc
        result["pull_output"] = pull_output
        print(f"DEPLOY: git pull rc={rc} output={pull_output!r}", flush=True)
        if rc != 0 or "CONFLICT" in (out + err) or "error:" in (out + err).lower():
            result["error"] = "git pull failed"
            return result
        result["ok"] = True
        return result
    except Exception as ex:
        result["error"] = f"unexpected error in pull: {ex!r}"
        print(f"DEPLOY: pull unexpected error: {ex!r}", flush=True)
        return result


def verify(env: dict[str, str]) -> VerifyResult:
    """Post-pull verify: git status --porcelain, zero non-?? lines -> ok.

    Returns {"ok", "modified", "error"}.
    """
    result: VerifyResult = {"ok": False, "modified": [], "error": None}
    try:
        rc, out, err = run_git(["status", "--porcelain"], env=env)
        if rc != 0:
            result["error"] = f"git status failed rc={rc}: {err}"
            print(f"DEPLOY: verify failed: {result['error']}", flush=True)
            return result
        modified = [l for l in out.splitlines() if l.strip() and not l.startswith("??")]
        if modified:
            result["modified"] = modified
            result["error"] = "unexpected tracked changes after pull"
            print(f"DEPLOY: unexpected tracked changes after pull: {modified}", flush=True)
            return result
        print("DEPLOY: post-pull verify ok", flush=True)
        result["ok"] = True
        return result
    except Exception as ex:
        result["error"] = f"unexpected error in verify: {ex!r}"
        print(f"DEPLOY: verify unexpected error: {ex!r}", flush=True)
        return result


# --- orchestrator -----------------------------------------------------------


def deploy() -> dict[str, Any]:
    """Single entrypoint. Straight-line steps; owns core_stopped + recovery."""
    print("DEPLOY: ===== deploy started =====", flush=True)
    env = derive_git_env()

    # Step 1: pre-stop gate check (gate #1). Fail -> return. Nothing stopped
    # yet, so no core action is needed.
    print("DEPLOY: running pre-check (gate #1)", flush=True)
    ok, reason, st = gate_check(env)
    print(f"DEPLOY: gate #1 result: ok={ok} reason={reason}", flush=True)
    if not ok:
        print("DEPLOY: gate #1 failed - returning", flush=True)
        return {"ok": False, "error": f"gate failed: {reason}", "status": st}
    print("DEPLOY: pre-check OK", flush=True)

    # Step 2: create HA backup. Fail -> return.
    b = create_backup()
    print(f"DEPLOY: backup result: ok={b['ok']} slug={b['slug']} code={b['code']}", flush=True)
    if not b["ok"]:
        return {"ok": False, "error": f"backup failed: {b['error']}", "backup": None}
    backup_slug = b["slug"]

    core_stopped = False
    try:
        # Step 3: stop HA core. Fail -> return with core_stopped=False.
        # Do NOT start: the core is presumably still running.
        print("DEPLOY: stopping ha core", flush=True)
        s = stop_core()
        print(f"DEPLOY: stop result: ok={s['ok']} code={s['code']} error={s['error']}", flush=True)
        if not s["ok"]:
            return {"ok": False, "error": f"failed to stop core: {s['error']}", "backup": backup_slug}
        # Set immediately after a successful stop, BEFORE anything else, so any
        # exception further down still triggers the finally restart.
        core_stopped = True

        # Step 4: after-stop gate check (gate #2). Fail -> start, return.
        print("DEPLOY: running after-stop check (gate #2)", flush=True)
        ok2, reason2, st2 = gate_check(env)
        print(f"DEPLOY: after-stop check result: ok={ok2} reason={reason2}", flush=True)
        if not ok2:
            print("DEPLOY: after-stop check was not fine, will return error but first will start ha core", flush=True)
            r = start_core()
            print(f"DEPLOY: restart result: ok={r['ok']} code={r['code']} error={r['error']}", flush=True)
            return {"ok": False, "error": f"post-stop gate failed: {reason2}", "status": st2, "backup": backup_slug}

        # Step 5: git pull. Fail -> start, return.
        p = pull(env)
        print(f"DEPLOY: pull result: ok={p['ok']} rc={p['rc']}", flush=True)
        if not p["ok"]:
            print("DEPLOY: pull was not fine, will return error but first will start ha core", flush=True)
            r = start_core()
            print(f"DEPLOY: restart result: ok={r['ok']} code={r['code']} error={r['error']}", flush=True)
            return {"ok": False, "error": "git pull failed", "pull_output": p["pull_output"], "backup": backup_slug}

        # Step 6: post-pull verify. Fail -> start, return.
        v = verify(env)
        print(f"DEPLOY: verify result: ok={v['ok']} modified={v['modified']}", flush=True)
        if not v["ok"]:
            print("DEPLOY: verify was not fine, will return error but first will start ha core", flush=True)
            r = start_core()
            print(f"DEPLOY: restart result: ok={r['ok']} code={r['code']} error={r['error']}", flush=True)
            return {"ok": False, "error": "unexpected tracked changes after pull",
                    "modified": v["modified"], "pull_output": p["pull_output"], "backup": backup_slug}

        # Step 7: start HA core. Fail -> return; core stays stopped and the
        # finally will still attempt a start.
        print("DEPLOY: starting ha core", flush=True)
        f = start_core()
        print(f"DEPLOY: start result: ok={f['ok']} code={f['code']} error={f['error']}", flush=True)
        if not f["ok"]:
            return {"ok": False, "error": f"failed to start core: {f['error']}",
                    "pull_output": p["pull_output"], "backup": backup_slug}
        core_stopped = False
        print("DEPLOY: ===== deploy finished OK =====", flush=True)
        return {"ok": True, "pull_output": p["pull_output"], "backup": backup_slug, "started": True}

    finally:
        # Safety net for ANY exception after a successful stop (e.g. a stray
        # broken-pipe from /health) - this exact gap is what broke the old
        # code, because core_stopped stayed False there.
        if core_stopped:
            print("DEPLOY: finally - core was stopped, restarting", flush=True)
            r = start_core()
            print(f"DEPLOY: finally restart result: ok={r['ok']} code={r['code']} error={r['error']}", flush=True)
