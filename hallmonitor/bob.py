"""Bob Shell integration (`bob run`, non-interactive).

`bob run --format json` returns one object with `status`, `last_message` and `stats` (task_id, token
counts, duration_ms, session_costs, tool_calls). Under `bob run` every tool is pre-approved, so no
human approves anything: Hall Monitor's hooks and the no-edit auditor mode are the only gate.
Bob Shell reads its key from BOB_API_KEY. HM_BOB_ACCEPT_LICENSE=1 and HM_BOB_TEAM_ID=<id> are opt-ins for a
fresh machine; a failure before the task runs comes back as status "unparsed" with Bob's `error`.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _run(root, mode, prompt, max_cost, max_turns, timeout):
    exe = None if os.environ.get("HM_DISABLE_BOB_SHELL") else shutil.which("bob")
    if not exe:
        return None
    # The resolved path, not "bob": on Windows Bob Shell is npm's bob.cmd, and CreateProcess won't find a .cmd
    # from a bare name, so every local `bob run` failed as "bob not on PATH" (Sept 27).
    cmd = [exe, "run", "--mode", mode, "--format", "json", "--max-cost", str(max_cost),
           "--max-turns", str(max_turns), "--workspace", str(root)]
    # On a fresh machine (a CI runner) `bob run` stops at IBM's license, and a "general" API key needs a
    # team id. Both are the operator's to give: Hall Monitor never accepts the license on its own.
    if os.environ.get("HM_BOB_ACCEPT_LICENSE"):
        cmd.append("--accept-license")
    if os.environ.get("HM_BOB_TEAM_ID"):
        cmd += ["--team-id", os.environ["HM_BOB_TEAM_ID"]]
    cmd.append(prompt)
    try:
        # stdin closed: a Bob Shell run that inherited an open stdin once hung before starting (Sept 27)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except OSError:
        return None
    except subprocess.TimeoutExpired:  # Bob Shell's own --max-cost/--max-turns still bound what it spends
        return {"status": "timeout", "stats": {}, "error": f"no result within {timeout}s"}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        data = _last_result(r.stdout)
    if data is None:
        return {"status": "unparsed", "last_message": r.stdout[-3000:], "stats": {}, "exit_code": r.returncode,
                "error": (r.stderr or "").strip()[-1000:]}
    data["exit_code"] = r.returncode
    return data


def _last_result(stdout):
    """Real Bob Shell 2.0.5, Sept 27: when a task reaches its cost cap, `--format json` prints an error object
    on one line and the result on the next, so the output isn't one JSON value. Take the last result line and
    keep the lines before it as `error`."""
    rows = []
    for line in (stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    results = [x for x in rows if isinstance(x, dict) and x.get("type") == "result"]
    if not results:
        return None
    notes = [str(x.get("message")) for x in rows if isinstance(x, dict) and x.get("type") == "error"]
    return {**results[-1], **({"error": " | ".join(notes)} if notes else {})}


def _record(root, mode, prompt, data):
    from .store import Store
    store = Store(root)
    stats = data.get("stats") or {}
    row = {"t": time.time(), "mode": mode, "prompt": prompt[:300], "status": data.get("status"), "stats": stats}
    if data.get("error"):  # why a run failed. Real Bob, Sept 27: every audit was "unparsed", and nothing said why
        error = str(data["error"])[-300:]
        for name in ("BOB_API_KEY", "TYPESAFE_API_KEY", "OPENJEV_API_KEY"):
            if len(os.environ.get(name) or "") >= 8:
                error = error.replace(os.environ[name], f"<{name}>")
        row["error"] = error
    store._append("bob_runs.jsonl", row)
    store.log({"stage": "bob_run", "action": data.get("status") or "done", "target": f"bob run --mode {mode}",
               "bob_stats": {k: stats.get(k) for k in ("session_costs", "tool_calls", "total_tokens", "duration_ms")
                             if k in stats}})


def shell_audit(root, brief, max_cost="0.30", max_turns="8", timeout=40):
    """Audit one claim with the read-only Receipts Auditor mode. Returns the auditor's final message, or None
    when there's no audit to use: a run that failed before its task ran ("unparsed") leaves only its error
    output, and one that ran past `timeout` left none. Real Bob, Sept 27: Bob starts the MCP server without
    BOB_API_KEY, so every audit ended with "Bob API key is required" and the supervised Bob audited instead.

    The auditor works in a scratch copy of the repo that has its mode and skills (.bob/) but not Hall Monitor's
    hooks (.bob/settings.json) or MCP server (.bob/mcp.json): a `bob run` in the supervised workspace would load
    them, and its hooks would share and rewrite the supervised session's state while that session waits for
    this audit. The copy also means the auditor can't change the evidence it audits."""
    from . import gitutil
    with tempfile.TemporaryDirectory(prefix="hm-audit-", ignore_cleanup_errors=True) as tmp:
        gitutil.copy_tree(root, tmp)
        if (Path(root) / ".bob").is_dir():  # the auditor's mode and skills, even where .bob/ is git-ignored
            shutil.copytree(Path(root) / ".bob", Path(tmp) / ".bob", dirs_exist_ok=True)
        for name in ("settings.json", "mcp.json"):
            (Path(tmp) / ".bob" / name).unlink(missing_ok=True)
        data = _run(tmp, "hm-auditor", brief, max_cost, max_turns, timeout)
    if not data:
        return None
    _record(root, "hm-auditor", brief, data)
    if data.get("status") in ("unparsed", "timeout"):
        return None
    return str(data.get("last_message") or "")[:3000] or None


def run_supervised(root, task, max_cost="2.00", max_turns="60", timeout=1800):
    """Headless supervised run (CI): Bob works in the Supervised mode; Hall Monitor's hooks gate it."""
    data = _run(root, "supervised", task, max_cost, max_turns, timeout)
    if data:
        _record(root, "supervised", task, data)
    return data
