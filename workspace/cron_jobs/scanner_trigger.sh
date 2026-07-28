#!/usr/bin/env bash
# scanner_trigger.sh — deterministic trigger for script-based cron jobs.
# Reads registry.json, runs jobs that have a "script" field directly (no LLM).
# Jobs with only a "prompt" field are logged and skipped.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="$ROOT_DIR/cron_jobs/registry.json"
LOG_DIR="$ROOT_DIR/cron_jobs/logs"
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

mkdir -p "$LOG_DIR"

# --- Log rotation: delete logs older than 30 days ---
find "$LOG_DIR" -name "*.log" -type f -mtime +30 -delete 2>/dev/null || true

if [ ! -f "$REGISTRY" ]; then
    echo "ERROR: registry not found: $REGISTRY" >&2
    exit 1
fi

PYTHON="${CONDA_PREFIX:+$CONDA_PREFIX/bin/python}"
PYTHON="${PYTHON:-python}"

"$PYTHON" - "$REGISTRY" "$LOG_DIR" "$DRY_RUN" "$ROOT_DIR" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

registry_path = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
dry_run = sys.argv[3] == "true"
root_dir = Path(sys.argv[4])

data = json.loads(registry_path.read_text(encoding="utf-8"))
tasks = data.get("tasks", [])

if not tasks:
    print("No tasks registered.")
    sys.exit(0)

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
ran = 0
skipped = 0

for task in tasks:
    task_id = task.get("id", "unknown")
    enabled = task.get("enabled", True)
    script = task.get("script", "")
    prompt = task.get("prompt", "")

    if not enabled:
        if dry_run:
            print(f"  [paused]  {task_id}")
        continue

    if dry_run:
        if script:
            print(f"  [script]  {task_id}  ->  {script}")
        else:
            print(f"  [LLM]     {task_id}  (requires LLM)")
        continue

    if not script:
        if prompt:
            log_path = log_dir / f"{today}_{task_id}.log"
            log_path.write_text(
                f"[{datetime.now(timezone.utc).isoformat()}] requires LLM, skipping\n",
                encoding="utf-8",
            )
            print(f"  skip {task_id} (requires LLM)")
            skipped += 1
        continue

    log_path = log_dir / f"{today}_{task_id}.log"
    cmd_parts = script.split()
    # Resolve script path relative to ROOT_DIR
    script_path = root_dir / cmd_parts[0]
    if script_path.exists():
        cmd = [sys.executable, str(script_path)] + cmd_parts[1:]
    else:
        cmd = cmd_parts

    print(f"  run  {task_id}  ->  {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd=str(root_dir))

    log_lines = [
        f"job: {task_id}",
        f"time: {datetime.now(timezone.utc).isoformat()}",
        f"command: {' '.join(cmd)}",
        f"exit_code: {result.returncode}",
        "",
        "STDOUT:",
        result.stdout,
        "STDERR:",
        result.stderr,
    ]
    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"       exit={result.returncode}  log={log_path}")
    ran += 1

if dry_run:
    print(f"\nDry run: {len(tasks)} task(s) registered.")
else:
    print(f"\nDone: {ran} ran, {skipped} skipped (LLM).")
PY
