#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="$ROOT_DIR/cron_jobs/registry.json"
LOG_DIR="$ROOT_DIR/cron_jobs/logs"
TASK_ID="${1:-}"
OPENCODE_API="${OPENCODE_API:-http://127.0.0.1:4096}"
OPENCODE_USER="${OPENCODE_USER:-opencode}"
OPENCODE_PASS="${OPENCODE_SERVER_PASSWORD:-}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-600}"

mkdir -p "$LOG_DIR"

/usr/bin/env python3 - "$REGISTRY" "$TASK_ID" "$OPENCODE_API" "$LOG_DIR" "$OPENCODE_USER" "$OPENCODE_PASS" "$ROOT_DIR" "$AGENT_TIMEOUT" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

registry_path = Path(sys.argv[1])
task_id = sys.argv[2]
api = sys.argv[3].rstrip("/")
log_dir = Path(sys.argv[4])
auth_user = sys.argv[5] if len(sys.argv) > 5 else "opencode"
auth_pass = sys.argv[6] if len(sys.argv) > 6 else ""
root_dir = sys.argv[7] if len(sys.argv) > 7 else "/workspace"
timeout_secs = int(sys.argv[8]) if len(sys.argv) > 8 else 600

data = json.loads(registry_path.read_text(encoding="utf-8"))
tasks = data.get("tasks", [])
if task_id:
    tasks = [task for task in tasks if task.get("id") == task_id]

for task in tasks:
    if not task.get("enabled", True):
        continue

    tid = task.get("id", "task")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{tid}_{timestamp}.log"
    prompt = task.get("prompt", "")

    command = [
        "timeout", str(timeout_secs),
        "opencode", "run",
        "--attach", api,
        "-u", auth_user,
        "-p", auth_pass,
        "--dir", root_dir,
        "--format", "json",
        "--title", f"cron:{tid}",
        "--", prompt,
    ]

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    session_id = "UNKNOWN"
    token_info = "N/A"
    total_input = 0
    total_output = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            for key in ("sessionID", "session_id", "sessionId"):
                sid = event.get(key, "")
                if sid and session_id == "UNKNOWN":
                    session_id = sid
                    break
            part = event.get("part", {})
            if isinstance(part, dict):
                for key in ("sessionID", "session_id", "sessionId"):
                    sid = part.get(key, "")
                    if sid and session_id == "UNKNOWN":
                        session_id = sid
                        break
                tokens = part.get("tokens", {})
                if isinstance(tokens, dict):
                    total_input += tokens.get("input", 0) or 0
                    total_output += tokens.get("output", 0) or 0
            usage = event.get("usage") or event.get("tokens") or {}
            if isinstance(usage, dict):
                total_input += usage.get("input", 0) or 0
                total_output += usage.get("output", 0) or 0
    if total_input > 0 or total_output > 0:
        token_info = json.dumps({"input": total_input, "output": total_output})

    exit_code = result.returncode
    if exit_code == 124:
        exit_label = "124 (TIMEOUT after {}s)".format(timeout_secs)
    else:
        exit_label = str(exit_code)

    log_content = "\n".join([
        "PROMPT:",
        prompt,
        "",
        "SESSION_ID: " + session_id,
        "EXIT_CODE: " + exit_label,
        "TOKENS: " + token_info,
        "",
        "STDOUT:",
        result.stdout,
        "",
        "STDERR:",
        result.stderr,
        "",
    ])
    log_path.write_text(log_content, encoding="utf-8")
    print(f"ran {tid} -> {log_path}")
PY
