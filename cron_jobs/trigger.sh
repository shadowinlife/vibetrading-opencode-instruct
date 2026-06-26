#!/usr/bin/env bash
# trigger.sh — Trigger an OpenCode agent task via CLI.
# Usage: trigger.sh [TASK_ID]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="$ROOT_DIR/cron_jobs/registry.json"
LOG_DIR="$ROOT_DIR/cron_jobs/logs"
TASK_ID="${1:-}"
OPENCODE_API="${OPENCODE_API:-http://127.0.0.1:4096}"
OPENCODE_USER="${OPENCODE_USER:-}"
OPENCODE_PASS="${OPENCODE_SERVER_PASSWORD:-}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-600}"

mkdir -p "$LOG_DIR"

python3 - "$REGISTRY" "$TASK_ID" "$OPENCODE_API" "$LOG_DIR" "$OPENCODE_USER" "$OPENCODE_PASS" "$ROOT_DIR" "$AGENT_TIMEOUT" <<'PY'
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

registry_path = Path(sys.argv[1])
task_id = sys.argv[2]
api = sys.argv[3].rstrip("/")
log_dir = Path(sys.argv[4])
auth_user = sys.argv[5] if len(sys.argv) > 5 else ""
auth_pass = sys.argv[6] if len(sys.argv) > 6 else ""
root_dir = sys.argv[7] if len(sys.argv) > 7 else "."
timeout_secs = int(sys.argv[8]) if len(sys.argv) > 8 else 600

data = json.loads(registry_path.read_text(encoding="utf-8"))
tasks = data.get("tasks", [])
if task_id:
    tasks = [t for t in tasks if t.get("id") == task_id]

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
        "--dir", root_dir,
        "--format", "json",
        "--title", f"cron:{tid}",
        "--", prompt,
    ]
    if auth_user:
        command.insert(4, "-u")
        command.insert(5, auth_user)
    if auth_pass:
        command.insert(6, "-p")
        command.insert(7, auth_pass)

    result = subprocess.run(command, text=True, capture_output=True, check=False)
    exit_code = result.returncode
    log_path.write_text(
        f"PROMPT:\n{prompt}\n\nEXIT_CODE: {exit_code}\n\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n",
        encoding="utf-8",
    )
    print(f"ran {tid} -> {log_path}")
PY
