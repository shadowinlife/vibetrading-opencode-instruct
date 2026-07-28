#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = Path(__file__).resolve().with_name("registry.json")
TRIGGER = Path(__file__).resolve().with_name("trigger.sh")
LOG_DIR = Path(__file__).resolve().with_name("logs")


def load_registry() -> dict[str, list[dict[str, Any]]]:
    if not REGISTRY.exists():
        return {"tasks": []}
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("registry.json field 'tasks' must be a list")
    return {"tasks": tasks}


def save_registry(data: dict[str, list[dict[str, Any]]]) -> None:
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_task_id(tasks: list[dict[str, Any]]) -> str:
    return f"test_{len(tasks) + 1:03d}" if not tasks else f"task_{len(tasks) + 1:03d}"


def get_crontab_lines() -> list[str]:
    result = subprocess.run(
        ["crontab", "-l"], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def set_crontab_lines(lines: list[str]) -> None:
    content = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(
        ["crontab", "-"], input=content, text=True, check=True
    )


def add_permanent_cron(task_id: str, cron_expr: str, name: str) -> None:
    """Add a permanent cron entry for a task, avoiding duplicates."""
    trigger_path = str(TRIGGER)
    log_path = f"{LOG_DIR}/{task_id}.log"
    cron_line = (
        f"{cron_expr} OPENCODE_API=http://127.0.0.1:4096 "
        f"/usr/bin/bash {trigger_path} {task_id} "
        f">> {log_path} 2>&1  # {name}"
    )
    lines = get_crontab_lines()
    # Remove any existing permanent entry for this task_id (not TEST entries)
    new_lines = []
    for line in lines:
        if trigger_path in line and f" {task_id} " in line and "# TEST:" not in line:
            continue  # skip old permanent entry
        new_lines.append(line)
    new_lines.append(cron_line)
    set_crontab_lines(new_lines)


def remove_permanent_cron(task_id: str) -> None:
    """Remove the permanent cron entry for a task."""
    trigger_path = str(TRIGGER)
    lines = get_crontab_lines()
    new_lines = []
    for line in lines:
        if trigger_path in line and f" {task_id} " in line and "# TEST:" not in line:
            continue  # skip permanent entry for this task
        new_lines.append(line)
    set_crontab_lines(new_lines)


def add_test_cron(task_id: str, run_at: datetime) -> None:
    minute = run_at.minute
    hour = run_at.hour
    day = run_at.day
    month = run_at.month
    cron_expr = f"{minute} {hour} {day} {month} *"
    test_comment = f"# TEST: {task_id} (auto-verify, safe to remove)"
    test_line = (
        f"OPENCODE_API=http://127.0.0.1:4096 "
        f"/usr/bin/bash {TRIGGER} {task_id} "
        f">> {LOG_DIR}/{task_id}.log 2>&1"
    )
    lines = get_crontab_lines()
    lines = [l for l in lines if f"# TEST: {task_id}" not in l]
    lines.append(test_comment)
    lines.append(f"{cron_expr} {test_line}")
    set_crontab_lines(lines)


def remove_test_cron(task_id: str) -> None:
    lines = get_crontab_lines()
    new_lines = []
    skip_next = False
    for line in lines:
        if f"# TEST: {task_id}" in line:
            skip_next = True
            continue
        if skip_next and not line.startswith("#") and task_id in line:
            skip_next = False
            continue
        skip_next = False
        new_lines.append(line)
    set_crontab_lines(new_lines)


def schedule_test_on_create(task_id: str) -> None:
    now = datetime.now()
    run_at = now + timedelta(minutes=5)
    run_at = run_at.replace(second=0, microsecond=0)
    add_test_cron(task_id, run_at)
    time_str = run_at.strftime("%H:%M")
    print(
        f"Test cron scheduled for {time_str}. "
        f"Run `python cron_jobs/manage.py verify-test {task_id}` after {time_str} to check."
    )


def cmd_list(_: argparse.Namespace) -> None:
    data = load_registry()
    if not data["tasks"]:
        print("No periodic tasks registered.")
        return
    for task in data["tasks"]:
        status = "enabled" if task.get("enabled", True) else "paused"
        print(f"{task['id']}\t{status}\t{task['cron']}\t{task['name']}")


def cmd_add(args: argparse.Namespace) -> None:
    data = load_registry()
    task_id = args.id or next_task_id(data["tasks"])
    if any(task.get("id") == task_id for task in data["tasks"]):
        raise SystemExit(f"Task id already exists: {task_id}")
    task = {
        "id": task_id,
        "name": args.name,
        "cron": args.cron,
        "prompt": args.prompt,
        "skills": args.skill,
        "signal_rules": [],
        "notify": {"dingtalk": args.dingtalk or "", "email": args.email or ""},
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data["tasks"].append(task)
    save_registry(data)
    add_permanent_cron(task_id, args.cron, args.name)
    print(f"Added {task_id}")
    schedule_test_on_create(task_id)


def cmd_remove(args: argparse.Namespace) -> None:
    data = load_registry()
    before = len(data["tasks"])
    data["tasks"] = [task for task in data["tasks"] if task.get("id") != args.id]
    if len(data["tasks"]) == before:
        raise SystemExit(f"Task not found: {args.id}")
    save_registry(data)
    remove_permanent_cron(args.id)
    remove_test_cron(args.id)
    print(f"Removed {args.id}")


def set_enabled(task_id: str, enabled: bool) -> None:
    data = load_registry()
    for task in data["tasks"]:
        if task.get("id") == task_id:
            task["enabled"] = enabled
            save_registry(data)
            print(("Resumed" if enabled else "Paused") + f" {task_id}")
            return
    raise SystemExit(f"Task not found: {task_id}")


def cmd_pause(args: argparse.Namespace) -> None:
    set_enabled(args.id, False)


def cmd_resume(args: argparse.Namespace) -> None:
    set_enabled(args.id, True)


def cmd_run(args: argparse.Namespace) -> None:
    task_id = args.id or ""
    command = ["bash", str(TRIGGER)]
    if task_id:
        command.append(task_id)
    subprocess.run(command, cwd=ROOT, check=True)


def cmd_verify_test(args: argparse.Namespace) -> None:
    task_id = args.id
    logs = sorted(LOG_DIR.glob(f"{task_id}_*.log"), key=lambda p: p.stat().st_mtime)

    if not logs:
        print(f"No execution log yet for {task_id}. Wait for the scheduled time and try again.")
        return

    latest_log = logs[-1]
    content = latest_log.read_text(encoding="utf-8")

    has_stdout = False
    has_tokens = False
    has_error = False
    session_id = "UNKNOWN"

    for line in content.splitlines():
        if line.startswith("SESSION_ID:"):
            session_id = line.split(":", 1)[1].strip()
        if line.startswith("STDOUT:") and len(line) > len("STDOUT:"):
            has_stdout = True
        if line.startswith("TOKENS:") and "N/A" not in line:
            token_val = line.split(":", 1)[1].strip()
            if token_val and token_val != "N/A":
                try:
                    tok = json.loads(token_val)
                    if isinstance(tok, dict) and (tok.get("input", 0) > 0 or tok.get("output", 0) > 0):
                        has_tokens = True
                except (json.JSONDecodeError, ValueError):
                    pass
        if line.startswith("EXIT_CODE:"):
            code_str = line.split(":", 1)[1].strip().split()[0]
            try:
                code = int(code_str)
                if code != 0:
                    has_error = True
            except ValueError:
                pass

    stdout_section = content.split("STDOUT:\n", 1)[-1] if "STDOUT:\n" in content else ""
    if stdout_section.strip():
        has_stdout = True

    if has_error:
        print(f"Test FAILED: {task_id} exited with non-zero code. Check {latest_log}")
        return

    if not has_stdout and not has_tokens:
        print(f"Test FAILED: {task_id} produced no output and no token usage. Agent may not have executed. Check {latest_log}")
        return

    print(f"Test PASSED: {task_id} executed successfully (session: {session_id})")
    test_lines = get_crontab_lines()
    has_test = any(f"# TEST: {task_id}" in l for l in test_lines)
    if has_test:
        remove_test_cron(task_id)
        print(f"Test cron entry for {task_id} removed from crontab.")
    else:
        print(f"No test cron entry found for {task_id} (already clean or manually created).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage periodic strategy execution registry")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add")
    p_add.add_argument("--id", default="")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--cron", required=True)
    p_add.add_argument("--prompt", required=True)
    p_add.add_argument("--skill", action="append", default=[])
    p_add.add_argument("--dingtalk", default="")
    p_add.add_argument("--email", default="")
    p_add.set_defaults(func=cmd_add)

    for name, func in (("remove", cmd_remove), ("pause", cmd_pause), ("resume", cmd_resume)):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(func=func)

    p_run = sub.add_parser("run")
    p_run.add_argument("id", nargs="?")
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser("verify-test")
    p_verify.add_argument("id")
    p_verify.set_defaults(func=cmd_verify_test)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
