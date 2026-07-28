"""Deploy HTML reports to local nginx or remote server via SCP."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


# Local-first deployment: reports served by local nginx
DEFAULT_LOCAL_DIR = "/workspace/reports"
DEFAULT_LOCAL_URL_BASE = "http://localhost:8088/reports"

# Remote deployment (optional, for multi-server setups)
DEFAULT_REMOTE_HOST = "your-server.example.com"
DEFAULT_REMOTE_DIR = "/opt/reports"
DEFAULT_REMOTE_USER = "root"

# Strict allowlist for path components to prevent command injection and path traversal
_PATH_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

def _validate_path_component(value: str, label: str) -> None:
    """Validate that a string is safe for use in file paths and shell commands."""
    if not _PATH_SAFE_RE.match(value):
        raise ValueError(
            f"Invalid characters in {label}: {value!r}. "
            f"Only alphanumeric, dots, underscores, and hyphens are allowed."
        )


def deploy_report(
    html_path: str,
    stock_code: str,
    report_name: str = "",
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    remote_user: str = DEFAULT_REMOTE_USER,
) -> str:
    """SCP HTML report to remote nginx directory, return public URL.
    
    Args:
        html_path: Local path to the HTML file.
        stock_code: Stock code (e.g. '588000.SH').
        report_name: Optional report name. Defaults to the filename stem.
        remote_host: Remote server hostname/IP.
        remote_dir: Remote directory for reports.
        remote_user: SSH user.
    
    Returns:
        Public URL of the deployed report.
    
    Raises:
        FileNotFoundError: If html_path does not exist.
        RuntimeError: If SCP or SSH fails.
    """
    _validate_path_component(stock_code, "stock_code")
    if report_name:
        _validate_path_component(report_name, "report_name")

    local = Path(html_path)
    if not local.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")
    
    name = report_name or local.stem
    remote_subdir = f"{remote_dir}/{stock_code}"
    remote_file = f"{remote_subdir}/{name}.html"
    
    # Ensure remote directory exists
    mkdir_cmd = ["ssh", f"{remote_user}@{remote_host}", f"mkdir -p {remote_subdir}"]
    result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SSH mkdir failed (exit {result.returncode}). Check SSH connectivity.")
    
    # SCP the file
    scp_cmd = ["scp", str(local), f"{remote_user}@{remote_host}:{remote_file}"]
    result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"SCP failed (exit {result.returncode}). Check SSH connectivity and disk space.")
    
    url = f"http://{remote_host}/reports/{stock_code}/{name}.html"
    return url


def deploy_local(
    html_path: str,
    stock_code: str,
    report_name: str = "",
    local_dir: str = DEFAULT_LOCAL_DIR,
    url_base: str = DEFAULT_LOCAL_URL_BASE,
) -> str:
    """Copy HTML report to local nginx-served directory.
    
    Args:
        html_path: Local path to the HTML file.
        stock_code: Stock code (e.g. '588000.SH').
        report_name: Optional report name. Defaults to the filename stem.
        local_dir: Local reports directory (served by nginx).
        url_base: Base URL for the reports endpoint.
    
    Returns:
        Public URL of the deployed report.
    """
    _validate_path_component(stock_code, "stock_code")
    if report_name:
        _validate_path_component(report_name, "report_name")

    local = Path(html_path)
    if not local.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")
    
    name = report_name or local.stem
    dest_dir = Path(local_dir) / stock_code
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.html"
    shutil.copy2(local, dest)
    url = f"{url_base}/{stock_code}/{name}.html"
    return url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy HTML reports to local nginx or remote server."
    )
    parser.add_argument("html_path", help="Path to the HTML file to deploy")
    parser.add_argument("--stock", required=True, help="Stock code (e.g. 588000.SH)")
    parser.add_argument("--name", default="", help="Report name (default: filename stem)")
    parser.add_argument("--remote", action="store_true", help="Deploy to remote server via SCP (default: local)")
    parser.add_argument("--host", default=DEFAULT_REMOTE_HOST, help=f"Remote host (default: {DEFAULT_REMOTE_HOST})")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR, help=f"Remote directory (default: {DEFAULT_REMOTE_DIR})")
    parser.add_argument("--user", default=DEFAULT_REMOTE_USER, help=f"SSH user (default: {DEFAULT_REMOTE_USER})")
    parser.add_argument("--local-dir", default=DEFAULT_LOCAL_DIR, help=f"Local reports directory (default: {DEFAULT_LOCAL_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    
    args = parser.parse_args()
    
    name = args.name or Path(args.html_path).stem
    
    if args.dry_run:
        if args.remote:
            remote_subdir = f"{args.remote_dir}/{args.stock}"
            remote_file = f"{remote_subdir}/{name}.html"
            print(f"[DRY RUN] ssh {args.user}@{args.host} mkdir -p {remote_subdir}")
            print(f"[DRY RUN] scp {args.html_path} {args.user}@{args.host}:{remote_file}")
            print(f"[DRY RUN] URL: http://{args.host}/reports/{args.stock}/{name}.html")
        else:
            dest = Path(args.local_dir) / args.stock / f"{name}.html"
            url = f"{DEFAULT_LOCAL_URL_BASE}/{args.stock}/{name}.html"
            print(f"[DRY RUN] cp {args.html_path} {dest}")
            print(f"[DRY RUN] URL: {url}")
        return
    
    try:
        if args.remote:
            url = deploy_report(args.html_path, args.stock, args.name, args.host, args.remote_dir, args.user)
            print(f"Deployed (remote): {url}")
        else:
            url = deploy_local(args.html_path, args.stock, args.name, args.local_dir)
            print(f"Deployed (local): {url}")
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
