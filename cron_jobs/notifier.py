"""DingTalk and email notification utilities."""
from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib import request


def send_dingtalk(webhook: str, title: str, markdown: str) -> dict:
    """Send a DingTalk markdown message. Returns API response dict."""
    if not webhook:
        return {"errcode": -1, "errmsg": "empty webhook"}
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def send_email(
    to: str, subject: str, body: str,
    smtp_host: str = "", smtp_user: str = "", smtp_pass: str = "",
) -> None:
    """Send a plain-text email via SMTP."""
    if not all([smtp_host, smtp_user, to]):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL(smtp_host, 465) as s:
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
