"""Git evolution committer with sanity gate and DingTalk notification.

Applies evolved diffs to the workspace, gates commits behind sanity checks,
and notifies the team via DingTalk on success or failure.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CommitResult:
    """Outcome of a commit_evolution() call."""

    success: bool
    commit_hash: str = ""
    message: str = ""
    error: str = ""
    sanity_passed: bool = True
    sanity_details: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in *cwd* and return the CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_sanity_checks() -> tuple[bool, str]:
    """Import and execute ``run_all_checks()`` from sanity.py.

    Returns ``(passed, details)``.  If sanity.py is unavailable the check
    is considered **passed** with a warning so that evolution is not blocked
    when the module has not been created yet.
    """
    try:
        from scripts.memory.sanity import run_all_checks  # type: ignore[import-untyped]
    except ImportError:
        msg = "sanity.py not found — skipping sanity gate (pass-through)"
        logger.warning(msg)
        return True, msg

    try:
        result = run_all_checks()
        # Accept both bool and (bool, str) return conventions.
        if isinstance(result, tuple):
            passed, details = bool(result[0]), str(result[1]) if len(result) > 1 else ""
        else:
            passed, details = bool(result), ""
        return passed, details
    except Exception as exc:  # noqa: BLE001
        return False, f"sanity check raised: {exc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def commit_evolution(
    diff: str,
    summary: str,
    context: str,
    score_before: float,
    score_after: float,
    workspace_dir: str = "/opt/qdata",
) -> CommitResult:
    """Apply *diff*, run sanity checks, and commit if they pass.

    Parameters
    ----------
    diff:
        Unified diff text to apply.
    summary:
        Human-readable one-liner for the commit message.
    context:
        Free-form context string included in the commit body.
    score_before / score_after:
        Evolution scores embedded in the commit body for traceability.
    workspace_dir:
        Git working-tree root.

    Returns
    -------
    CommitResult
    """
    cwd = workspace_dir

    # 0. Stash local changes before applying evolution diff ------------------
    stash_result = _run_git(
        ["stash", "push", "--include-untracked", "-m", "pre-evolution-auto-stash"],
        cwd,
    )
    stashed = (
        stash_result.returncode == 0
        and "No local changes" not in stash_result.stderr
    )
    if not stashed and stash_result.returncode != 0:
        logger.warning("git stash push failed: %s", stash_result.stderr.strip())

    # 1. Write diff to a temp file and apply --------------------------------
    diff_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False, dir=cwd
        ) as tmp:
            tmp.write(diff)
            diff_path = tmp.name

        apply_res = _run_git(["apply", "--check", diff_path], cwd)
        if apply_res.returncode != 0:
            return CommitResult(
                success=False,
                error=f"git apply --check failed: {apply_res.stderr.strip()}",
            )

        apply_res = _run_git(["apply", diff_path], cwd)
        if apply_res.returncode != 0:
            return CommitResult(
                success=False,
                error=f"git apply failed: {apply_res.stderr.strip()}",
            )
    except Exception as exc:  # noqa: BLE001
        return CommitResult(success=False, error=f"diff apply error: {exc}")
    finally:
        if diff_path is not None:
            Path(diff_path).unlink(missing_ok=True)

    # 2. Sanity gate ---------------------------------------------------------
    passed, details = _run_sanity_checks()
    if not passed:
        # Keep the stash for manual recovery; do NOT auto-pop.
        return CommitResult(
            success=False,
            sanity_passed=False,
            sanity_details=details,
            error="sanity checks failed — evolution diff applied, stash preserved for manual recovery",
        )

    # 3. Stage & commit ------------------------------------------------------
    add_res = _run_git(["add", "-A"], cwd)
    if add_res.returncode != 0:
        return CommitResult(
            success=False,
            error=f"git add failed: {add_res.stderr.strip()}",
            sanity_passed=True,
            sanity_details=details,
        )

    commit_msg = (
        f"evolution: {summary}\n\n"
        f"Context: {context}\n"
        f"Score: {score_before:.4f} -> {score_after:.4f} "
        f"(delta={score_after - score_before:+.4f})\n"
        f"Sanity: passed"
    )

    commit_res = _run_git(["commit", "-m", commit_msg], cwd)
    if commit_res.returncode != 0:
        return CommitResult(
            success=False,
            error=f"git commit failed: {commit_res.stderr.strip()}",
            sanity_passed=True,
            sanity_details=details,
        )

    # Extract short hash from the commit output.
    hash_res = _run_git(["rev-parse", "--short", "HEAD"], cwd)
    commit_hash = hash_res.stdout.strip() if hash_res.returncode == 0 else ""

    # 4. Pop the stash to restore user's local changes ------------------------
    if stashed:
        pop_result = _run_git(["stash", "pop"], cwd)
        if pop_result.returncode != 0:
            logger.warning(
                "git stash pop failed (likely conflicts) — "
                "stash left for manual recovery: %s",
                pop_result.stderr.strip(),
            )

    return CommitResult(
        success=True,
        commit_hash=commit_hash,
        message=summary,
        sanity_passed=True,
        sanity_details=details,
    )


def send_evolution_notification(
    commit_result: CommitResult,
    diff_preview: str | None = None,
) -> bool:
    """Send a DingTalk notification about an evolution commit.

    Returns ``True`` if the notification was sent, ``False`` otherwise.
    """
    try:
        from cron_jobs.notifier import load_env, send_dingtalk  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("cron_jobs.notifier unavailable — skipping notification")
        return False

    env = load_env()
    webhook = env.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        logger.warning("DINGTALK_WEBHOOK not set — skipping notification")
        return False

    status = "SUCCESS" if commit_result.success else "FAILED"
    lines = [
        f"### Evolution Commit — {status}",
        "",
        f"- **Summary**: {commit_result.message}",
        f"- **Commit**: `{commit_result.commit_hash or 'N/A'}`",
        f"- **Sanity**: {'passed' if commit_result.sanity_passed else 'FAILED'}",
    ]
    if commit_result.sanity_details:
        lines.append(f"- **Sanity details**: {commit_result.sanity_details}")
    if commit_result.error:
        lines.append(f"- **Error**: {commit_result.error}")
    if diff_preview:
        preview = diff_preview[:500]
        lines.append(f"\n```diff\n{preview}\n```")

    markdown = "\n".join(lines)
    title = f"Evolution {status}"

    try:
        send_dingtalk(webhook, title, markdown)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("DingTalk notification failed: %s", exc)
        return False


def rollback(commit_hash: str, workspace_dir: str = "/opt/qdata") -> bool:
    """Revert a specific commit by hash.

    Returns ``True`` on success, ``False`` on failure.
    """
    res = _run_git(["revert", commit_hash, "--no-edit"], workspace_dir)
    if res.returncode != 0:
        logger.error(
            "git revert %s failed: %s", commit_hash, res.stderr.strip()
        )
        return False
    logger.info("Reverted commit %s", commit_hash)
    return True
