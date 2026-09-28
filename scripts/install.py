"""Install Hall Monitor into a repository that Bob works on.

Usage:
  python scripts/install.py <path-to-repo>           full install
  python scripts/install.py --probe <path-to-repo>   probe install: record raw hook payloads (see PROBE.md)

Writes into <repo>/.bob/:
  settings.json      lifecycle hooks (merged with existing settings)
  mcp.json           the hall-monitor MCP server (merged)
  custom_modes.yaml  🛂 Supervised and 🔎 Receipts Auditor modes (appended if the file exists)
  skills/            hall-monitor-protocol, extract-decisions, certified-plan, claim-audit
  commands/          /decisions, /receipts, /audit, /hall-pass, /export-ledger
  rules-plan/        Plan mode writes certified plans
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ASSETS = HERE / "bob"
# spawn_subagent is matched so every subagent's brief is checked before it starts and its summary
# when it returns, whether or not hooks fire inside subagents.
TOOLS = "^(write_file|write_to_file|apply_diff|search_and_replace|insert_content|execute_command|spawn_subagent)$"
MARKER = "# hall-monitor modes"
MCP_TOOLS = ["declare_intent", "explain_block", "record_decision", "list_decisions", "list_evidence",
             "submit_claims", "hall_pass"]


def shell_path(p):
    """A path safe inside a hook command. Bob runs hooks via `cmd /c` on Windows, which mangles
    commands with several quoted parts, so use the space-free 8.3 short path there."""
    p = str(p)
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
            p = buf.value
    p = p.replace("\\", "/")
    return f'"{p}"' if " " in p else p


def merge_json(path, update):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key, value in update.items():
        data.setdefault(key, {}).update(value)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def hooks(script, matcher, pre_timeout=180):
    cmd = f"{shell_path(sys.executable)} {shell_path(script)}"

    def handler(timeout):
        return [{"type": "command", "command": cmd, "timeout": timeout}]
    return {"hooks": {
        "SessionStart": [{"hooks": handler(30)}],
        "UserPromptSubmit": [{"hooks": handler(30)}],
        "PreToolUse": [{"matcher": matcher, "hooks": handler(pre_timeout)}],
        "PostToolUse": [{"matcher": matcher, "hooks": handler(60)}],
        "Stop": [{"hooks": handler(180)}],
    }}


def main(repo, probe=False):
    sys.stdout.reconfigure(encoding="utf-8")
    repo = Path(repo).resolve()
    bob = repo / ".bob"
    bob.mkdir(exist_ok=True)

    if probe:
        merge_json(bob / "settings.json", hooks(HERE / "scripts" / "probe_hook.py", ".*", pre_timeout=10))
    else:
        merge_json(bob / "settings.json", hooks(HERE / "hm_hook.py", TOOLS))
    # Only fields listed for .bob/mcp.json in the rulesync Bob target (#3011). No timeout: its unit is
    # unverified, and a wrong unit would cut off Jev calls.
    # Bob starts the server without the user's environment (probe, Sept 27; Linux container, Sept 27), but
    # expands ${env:NAME} in mcp.json from its own (Bob Shell 2.0.5 docs). A reference, never the key.
    merge_json(bob / "mcp.json", {"mcpServers": {"hall-monitor": {
        "command": Path(sys.executable).as_posix(), "args": [(HERE / "hm_mcp.py").as_posix()],
        "cwd": repo.as_posix(), "env": {"HM_ROOT": repo.as_posix(), "TYPESAFE_API_KEY": "${env:TYPESAFE_API_KEY}",
                                        "BOB_API_KEY": "${env:BOB_API_KEY}", "OPENJEV_API_KEY": "${env:OPENJEV_API_KEY}"},  # the Receipts auditor's `bob run`
        "alwaysAllow": MCP_TOOLS}}})

    # Our modes sit at the end of the file after MARKER, so reinstalling replaces them in place.
    modes = bob / "custom_modes.yaml"
    ours = (ASSETS / "custom_modes.yaml").read_text(encoding="utf-8")
    items = ours[ours.index(MARKER):]
    text = modes.read_text(encoding="utf-8") if modes.exists() else ""
    if MARKER in text:
        text = text[:text.index(MARKER)]
    if not text.strip() or text.strip() == "customModes:":
        modes.write_text(ours, encoding="utf-8")
    else:
        modes.write_text(text.rstrip() + "\n  " + items, encoding="utf-8")

    for sub in ("skills", "commands", "rules-plan"):
        shutil.copytree(ASSETS / sub, bob / sub, dirs_exist_ok=True)

    ignore = repo / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    if ".hallmonitor/" not in lines:
        ignore.write_text("\n".join(lines + [".hallmonitor/"]) + "\n", encoding="utf-8")

    if probe:
        print(f"PROBE hooks installed in {bob} (they record raw payloads to .hallmonitor/probe.jsonl and "
              "allow everything). Follow PROBE.md, then run install.py again without --probe.")
    else:
        print(f"Hall Monitor installed in {bob}\n"
              "- Open the repo as a TRUSTED workspace (hooks respect workspace trust).\n"
              "- Check Settings > Hooks lists the five hooks; approve the hall-monitor MCP server.\n"
              "- Pick the '🛂 Supervised' mode.\n"
              "- TYPESAFE_API_KEY must be set in the environment Bob runs in (never commit it).")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--probe"]
    if len(args) != 1:
        sys.exit(__doc__)
    main(args[0], probe="--probe" in sys.argv)
