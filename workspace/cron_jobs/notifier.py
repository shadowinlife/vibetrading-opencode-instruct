from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping
from urllib import request


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or ROOT / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _config_value(env_file: Mapping[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env_file.get(key, default)


def send_dingtalk(webhook: str, title: str, markdown: str) -> None:
    if not webhook:
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        resp.read()


def send_email(to_addr: str, subject: str, html: str) -> None:
    if not to_addr:
        return
    env_file = load_env()
    host = _config_value(env_file, "SMTP_HOST")
    port = int(_config_value(env_file, "SMTP_PORT", "465"))
    user = _config_value(env_file, "SMTP_USER") or _config_value(env_file, "SMTP_FROM")
    password = _config_value(env_file, "SMTP_PASSWORD") or _config_value(env_file, "SMTP_AUTH_CODE")
    sender = _config_value(env_file, "SMTP_FROM", user)
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM/SMTP_USER are required for email notification")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content(html)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


def notify(task: Mapping[str, object], signal: Mapping[str, object]) -> None:
    notify_cfg = task.get("notify")
    if not isinstance(notify_cfg, Mapping):
        return
    title = f"Strategy signal: {task.get('name', task.get('id', 'unknown'))}"
    markdown = "\n".join(
        [
            f"## {title}",
            f"- Task: {task.get('id', '')}",
            f"- Signal: {signal.get('signal', signal.get('type', 'triggered'))}",
            f"- Reason: {signal.get('reason', '')}",
        ]
    )
    dingtalk = str(notify_cfg.get("dingtalk") or "")
    email = str(notify_cfg.get("email") or "")
    send_dingtalk(dingtalk, title, markdown)
    send_email(email, title, markdown.replace("\n", "<br>"))


if __name__ == "__main__":
    print("notifier module OK")
