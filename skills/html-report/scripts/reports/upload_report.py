"""Deliver an HTML report to the user through the Resource Manager (RM).

Flow (RM private-OSS egress contract):
  1. POST /api/v1/agent/report-upload-credential with the per-box agent
     token -> RM books a report row and returns a short-lived STS triple
     scoped to PutObject on the caller's own prefix, plus the internal
     OSS endpoint, bucket and preallocated object key.
  2. PUT the HTML bytes to OSS through the internal endpoint with
     oss2.StsAuth (the sandbox VPC reaches the internal endpoint; the
     public endpoint is not usable for uploads).
  3. POST /api/v1/agent/report-uploaded {report_id, sha256, size} -> RM
     HEAD-verifies the object and flips the report row to ready.

The owner then views or shares the report from the RM web report list;
there is no sandbox-local URL. Credentials (agent token, STS triple) are
read once, held in memory only, and never printed or persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import oss2

# Preset in the sandbox by the RM provisioning template.
DEFAULT_TOKEN_FILE = Path("/home/opencode/.config/rm/agent-token")
RM_URL_ENV = "RM_AGENT_API_URL"
HTTP_TIMEOUT_S = 30

# Upload headers required by the egress contract: inline HTML rendering,
# no CDN caching of the privately served object.
UPLOAD_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "private, no-store",
    "Content-Disposition": "inline",
}

# The report renders under CSP "sandbox allow-scripts" on an opaque origin:
# any external fetch (CDN script, stylesheet, font, image) is dead weight or
# blocked outright, so the HTML must carry everything inline.
_EXTERNAL_SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_EXTERNAL_LINK_RE = re.compile(r"<link\b[^>]*\bhref\s*=\s*[\"']?https?://", re.IGNORECASE)
_EXTERNAL_CSS_URL_RE = re.compile(r"url\(\s*[\"']?https?://", re.IGNORECASE)


def assert_self_contained(html: str) -> None:
    """Reject HTML that would need external fetches to render.

    Raises:
        ValueError: Naming the first external reference found.
    """
    for pattern, what in (
        (_EXTERNAL_SCRIPT_RE, "external <script src> (inline the JS instead)"),
        (_EXTERNAL_LINK_RE, "external <link href> (inline the CSS instead)"),
        (_EXTERNAL_CSS_URL_RE, "external CSS url() (use system fonts / inline assets)"),
    ):
        match = pattern.search(html)
        if match:
            raise ValueError(f"report HTML is not self-contained: {what}")


def read_agent_token(token_file: Path) -> str:
    """Read the per-box agent token; never log or echo it."""
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"agent token file is empty: {token_file}")
    return token


def _post_json(url: str, token: str, payload: dict | None = None) -> dict[str, Any]:
    """POST to an RM agent endpoint and return its JSON response.

    Error bodies from RM carry only a safe ``detail`` string; anything
    else is reduced to the HTTP status so no response content can leak.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("detail", ""))
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"RM answered HTTP {exc.code}: {detail}".rstrip(": ")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"RM unreachable at {url}: {exc.reason}") from exc


def put_report_html(credential: dict[str, Any], payload: bytes) -> None:
    """PUT the report bytes to OSS via the internal endpoint using STS."""
    auth = oss2.StsAuth(
        credential["access_key_id"],
        credential["access_key_secret"],
        credential["security_token"],
    )
    bucket = oss2.Bucket(auth, "https://" + credential["endpoint"], credential["bucket"])
    try:
        bucket.put_object(credential["object_key"], payload, headers=UPLOAD_HEADERS)
    except oss2.exceptions.OssError as exc:
        # str(OssError) can echo the access key id; surface only safe fields.
        raise RuntimeError(
            f"OSS upload failed: status={exc.status} code={exc.code} "
            f"request_id={exc.request_id}"
        ) from exc


def upload_report(html_path: Path, rm_url: str, token_file: Path) -> dict[str, Any]:
    """Run the full credential -> PUT -> confirm flow for one HTML file.

    Returns:
        The RM confirmation document {report_id, status, size_bytes}.

    Raises:
        FileNotFoundError: If html_path does not exist.
        ValueError: If the HTML is not self-contained or the token is empty.
        RuntimeError: On RM or OSS failure (message carries no credentials).
    """
    payload = html_path.read_bytes()
    try:
        html_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"report HTML must be UTF-8: {html_path}") from exc
    assert_self_contained(html_text)

    token = read_agent_token(token_file)
    base = rm_url.rstrip("/")
    credential = _post_json(f"{base}/api/v1/agent/report-upload-credential", token)
    put_report_html(credential, payload)
    return _post_json(
        f"{base}/api/v1/agent/report-uploaded",
        token,
        {
            "report_id": credential["report_id"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload an HTML report to the user via the Resource Manager."
    )
    parser.add_argument("html_path", help="Path to the self-contained HTML file")
    parser.add_argument(
        "--rm-url",
        default="",
        help=f"RM base URL (default: ${RM_URL_ENV} preset in the sandbox)",
    )
    parser.add_argument(
        "--token-file",
        default=str(DEFAULT_TOKEN_FILE),
        help=f"Per-box agent token file (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the HTML and print the upload plan without any network call",
    )
    args = parser.parse_args()

    html_path = Path(args.html_path)
    try:
        if args.dry_run:
            payload = html_path.read_bytes()
            assert_self_contained(payload.decode("utf-8"))
            print(f"[DRY RUN] {html_path}: {len(payload)} bytes, self-contained OK")
            print("[DRY RUN] POST /api/v1/agent/report-upload-credential")
            print("[DRY RUN] oss2 PUT via internal endpoint (STS, PutObject-only)")
            print("[DRY RUN] POST /api/v1/agent/report-uploaded {report_id, sha256, size}")
            return
        rm_url = args.rm_url or os.environ.get(RM_URL_ENV, "")
        if not rm_url:
            raise ValueError(f"RM base URL missing: pass --rm-url or set ${RM_URL_ENV}")
        result = upload_report(html_path, rm_url, Path(args.token_file))
        print(
            f"Uploaded report {result['report_id']} "
            f"(status={result['status']}, {result['size_bytes']} bytes). "
            "The owner can view or share it from the RM report list."
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
