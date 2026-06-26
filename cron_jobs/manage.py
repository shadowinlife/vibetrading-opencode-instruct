#!/usr/bin/env python3
"""Cron job registry manager.

Usage:
    python cron_jobs/manage.py list
    python cron_jobs/manage.py add --name NAME --cron CRON --prompt PROMPT [--dingtalk URL] [--email ADDR]
    python cron_jobs/manage.py pause TASK_ID
    python cron_jobs/manage.py resume TASK_ID
    python cron_jobs/manage.py remove TASK_ID
    python cron_jobs/manage.py run TASK_ID
    python cron_jobs/manage.py verify-test TASK_ID
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = Path(__file__).resolve().with_name("registry.json")
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
    return f"task_{len(tasks) + 1:03d}" if tasks else "task_001"


def cmd_list(args: argparse.Namespace) -> None:
    data = load_registry()
    for t in data["tasks"]:
        status = "✅" if t.get("enabled", True) else "⏸"
        print(f"  {status}  {t['id']:20s}  {t.get('cron', ''):20s}  {t.get('name', '')}")


def cmd_add(args: argparse.Namespace) -> None:
    data = load_registry()
    task_id = next_task_id(data["tasks"])
    task: dict[str, Any] = {
        "id": task_id,
        "name": args.name,
        "cron": args.cron,
        "prompt": args.prompt,
        "skills": [],
        "signal_rules": [],
        "notify": {},
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.dingtalk:
        task["notify"]["dingtalk"] = args.dingtalk
    if args.email:
        task["notify"]["email"] = args.email
    data["tasks"].append(task)
    save_registry(data)
    print(f"Added task: {task_id}")


def cmd_pause(args: argparse.Namespace) -> None:
    data = load_registry()
    for t in data["tasks"]:
        if t["id"] == args.task_id:
            t["enabled"] = False
            save_registry(data)
            print(f"Paused: {args.task_id}")
            return
    print(f"Task not found: {args.task_id}")


def cmd_resume(args: argparse.Namespace) -> None:
    data = load_registry()
    for t in data["tasks"]:
        if t["id"] == args.task_id:
            t["enabled"] = True
            save_registry(data)
            print(f"Resumed: {args.task_id}")
            return
    print(f"Task not found: {args.task_id}")


def cmd_remove(args: argparse.Namespace) -> None:
    data = load_registry()
    data["tasks"] = [t for t in data["tasks"] if t["id"] != args.task_id]
    save_registry(data)
    print(f"Removed: {args.task_id}")


def cmd_run(args: argparse.Namespace) -> None:
    trigger = Path(__file__).resolve().with_name("trigger.sh")
    subprocess.run(["bash", str(trigger), args.task_id], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cron job registry manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all tasks")

    p_add = sub.add_parser("add", help="Add a new task")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--cron", required=True)
    p_add.add_argument("--prompt", required=True)
    p_add.add_argument("--dingtalk", default="")
    p_add.add_argument("--email", default="")

    p_pause = sub.add_parser("pause", help="Pause a task")
    p_pause.add_argument("task_id")

    p_resume = sub.add_parser("resume", help="Resume a task")
    p_resume.add_argument("task_id")

    p_remove = sub.add_parser("remove", help="Remove a task")
    p_remove.add_argument("task_id")

    p_run = sub.add_parser("run", help="Run a task now")
    p_run.add_argument("task_id")

    args = parser.parse_args()
    {"list": cmd_list, "add": cmd_add, "pause": cmd_pause,
     "resume": cmd_resume, "remove": cmd_remove, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    main()
