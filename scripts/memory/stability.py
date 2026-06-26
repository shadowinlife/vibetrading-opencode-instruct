"""Stability detector and release branch manager.

Checks if the last N evolution commits qualify as 'stable':
- No new SKILL added
- No new AGENTS module (top-level section)
- Each commit diff < 100 lines
- Score change < 0.02 between consecutive commits
- Error notebook new entries < 5 in the period

If stable, creates a release branch + tag + DingTalk notification.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvolutionEntry:
    """A single evolution commit parsed from git log."""

    commit_hash: str
    summary: str
    score_before: float
    score_after: float
    timestamp: str  # ISO-8601


@dataclass
class StabilityResult:
    """Outcome of a stability check."""

    stable: bool
    reasons: list = field(default_factory=list)
    history: list = field(default_factory=list)
    commits_checked: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Match both "evolve:" and "evolution:" prefixes (git_committer.py uses
# "evolution:" while the spec references "evolve:").
_EVOLVE_PREFIX_RE = re.compile(r"^(?:evolve|evolution):\s*(.+)", re.IGNORECASE)

# Score line: "Score: 0.1234 -> 0.5678" or "Score: 0.1234 → 0.5678"
_SCORE_RE = re.compile(
    r"Score:\s*([\d.]+)\s*(?:->|→)\s*([\d.]+)"
)

_SKILLS_DIR = ".opencode/skills/"
_AGENTS_MD = "AGENTS.md"
_MISTAKES_GLOB_PATTERNS = [
    "**/mistakes.md",
]


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in *cwd* and return the CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _get_evolve_commits(
    workspace_dir: str, limit: int = 10
) -> list[dict[str, str]]:
    """Return raw git log entries for evolution commits.

    Each entry is a dict with keys: hash, subject, body, date.
    """
    # Use a custom format to separate fields reliably.
    # %H = full hash, %s = subject, %b = body, %aI = author date ISO
    fmt = "---COMMIT---%n%H%n%s%n%b%n%aI"
    res = _run_git(
        ["log", f"--format={fmt}", f"-{limit * 3}", "--all"],
        workspace_dir,
    )
    if res.returncode != 0:
        logger.warning("git log failed: %s", res.stderr.strip())
        return []

    commits: list[dict[str, str]] = []
    blocks = res.stdout.split("---COMMIT---")
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 4:
            continue
        commit_hash = lines[0].strip()
        subject = lines[1].strip()
        # Body is everything between subject and the last line (date)
        body = "\n".join(lines[2:-1]).strip()
        ts = lines[-1].strip()

        # Only keep evolution commits
        if not _EVOLVE_PREFIX_RE.match(subject):
            continue

        commits.append(
            {
                "hash": commit_hash,
                "subject": subject,
                "body": body,
                "date": ts,
            }
        )
        if len(commits) >= limit:
            break

    return commits


def _parse_evolution_entry(raw: dict[str, str]) -> EvolutionEntry:
    """Parse a raw git log entry into an EvolutionEntry."""
    subject = raw["subject"]
    m = _EVOLVE_PREFIX_RE.match(subject)
    summary = m.group(1).strip() if m else subject

    # Parse score from body
    score_before = 0.0
    score_after = 0.0
    full_text = f"{subject}\n{raw['body']}"
    sm = _SCORE_RE.search(full_text)
    if sm:
        score_before = float(sm.group(1))
        score_after = float(sm.group(2))

    return EvolutionEntry(
        commit_hash=raw["hash"],
        summary=summary,
        score_before=score_before,
        score_after=score_after,
        timestamp=raw["date"],
    )


def _check_no_new_skill(
    commit_hash: str, workspace_dir: str
) -> tuple[bool, str]:
    """Check that no new skill file was added in this commit.

    Returns (passed, reason).
    """
    # Detect first commit: no parent via rev-parse
    parent_check = _run_git(
        ["rev-parse", "--verify", f"{commit_hash}~1"], workspace_dir
    )
    if parent_check.returncode != 0:
        return True, "no parent commit (first commit)"

    res = _run_git(
        ["diff", "--name-only", "--diff-filter=A", f"{commit_hash}~1", commit_hash],
        workspace_dir,
    )
    if res.returncode != 0:
        return False, f"git diff failed: {res.stderr.strip()}"

    added_files = res.stdout.strip().splitlines()
    new_skills = [f for f in added_files if f.startswith(_SKILLS_DIR)]
    if new_skills:
        return False, f"new skill(s) added: {', '.join(new_skills)}"
    return True, "no new skills"


def _check_no_new_agents_module(
    commit_hash: str, workspace_dir: str
) -> tuple[bool, str]:
    """Check that no new top-level ``## `` section was added to AGENTS.md.

    Returns (passed, reason).
    """
    # Get the diff for AGENTS.md only
    res = _run_git(
        ["diff", f"{commit_hash}~1", commit_hash, "--", _AGENTS_MD],
        workspace_dir,
    )
    if res.returncode != 0:
        return True, "AGENTS.md not changed or no parent"

    diff_text = res.stdout
    if not diff_text.strip():
        return True, "AGENTS.md unchanged"

    # Look for added lines starting with "## " (new top-level sections)
    added_sections: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+## ") and not line.startswith("+++"):
            section_name = line[1:].strip()  # strip the leading '+'
            added_sections.append(section_name)

    if added_sections:
        return False, f"new AGENTS.md section(s): {', '.join(added_sections)}"
    return True, "no new AGENTS.md sections"


def _check_diff_size(
    commit_hash: str, workspace_dir: str, max_lines: int = 100
) -> tuple[bool, str]:
    """Check that the commit diff is under *max_lines* insertions.

    Returns (passed, reason).
    """
    res = _run_git(
        ["diff", "--stat", f"{commit_hash}~1", commit_hash],
        workspace_dir,
    )
    if res.returncode != 0:
        return True, "no parent commit"

    stat_text = res.stdout.strip()
    if not stat_text:
        return True, "empty diff"

    # The last line of --stat looks like:
    # " 3 files changed, 42 insertions(+), 10 deletions(-)"
    last_line = stat_text.splitlines()[-1]
    ins_match = re.search(r"(\d+)\s+insertion", last_line)
    insertions = int(ins_match.group(1)) if ins_match else 0

    if insertions >= max_lines:
        return (
            False,
            f"diff too large: {insertions} insertions (limit {max_lines})",
        )
    return True, f"diff size OK ({insertions} insertions)"


def _check_score_change(
    entry: EvolutionEntry, max_delta: float = 0.02
) -> tuple[bool, str]:
    """Check that score change between consecutive commits is small.

    Returns (passed, reason).
    """
    delta = abs(entry.score_after - entry.score_before)
    if delta >= max_delta:
        return (
            False,
            f"score change too large: {delta:.4f} (limit {max_delta})",
        )
    return True, f"score change OK ({delta:.4f})"


def _count_new_mistakes(
    workspace_dir: str, since_commit: str
) -> int:
    """Count new entries in mistakes.md files since *since_commit*.

    Scans both global and per-stock mistakes files under
    ``~/.vibe-trading/memory/``.
    """
    memory_base = Path.home() / ".vibe-trading" / "memory"
    if not memory_base.exists():
        return 0

    # Get the timestamp of the since_commit
    res = _run_git(
        ["log", "-1", "--format=%aI", since_commit],
        workspace_dir,
    )
    if res.returncode != 0:
        return 0

    try:
        since_dt = datetime.fromisoformat(res.stdout.strip())
    except (ValueError, TypeError):
        return 0

    # Find all mistakes.md files
    mistake_files: list[Path] = []
    for pattern in _MISTAKES_GLOB_PATTERNS:
        mistake_files.extend(memory_base.glob(pattern))
    # Deduplicate
    mistake_files = list(set(mistake_files))

    new_count = 0
    # Count entries with created date after since_dt
    date_re = re.compile(r"created:\s*(\S+)")
    for mf in mistake_files:
        try:
            text = mf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for dm in date_re.finditer(text):
            try:
                entry_dt = datetime.fromisoformat(dm.group(1))
                if entry_dt > since_dt:
                    new_count += 1
            except (ValueError, TypeError):
                continue

    return new_count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_evolution_history(
    workspace_dir: str = "/opt/qdata", limit: int = 10
) -> list[EvolutionEntry]:
    """Parse git log for evolution commits and return structured history.

    Parameters
    ----------
    workspace_dir:
        Git working-tree root.
    limit:
        Maximum number of entries to return.

    Returns
    -------
    list[EvolutionEntry]
        Parsed evolution history, newest first.
    """
    raw_commits = _get_evolve_commits(workspace_dir, limit=limit)
    return [_parse_evolution_entry(c) for c in raw_commits]


def check_stability(
    workspace_dir: str = "/opt/qdata", last_n: int = 3
) -> StabilityResult:
    """Check if the last *last_n* evolution commits qualify as stable.

    Stability criteria (all must pass):
    1. No new SKILL added in any commit
    2. No new AGENTS module (top-level ``## `` section) in any commit
    3. Each commit diff < 100 lines of insertions
    4. Score change < 0.02 between consecutive commits
    5. Error notebook new entries < 5 in the period

    Parameters
    ----------
    workspace_dir:
        Git working-tree root.
    last_n:
        Number of recent evolution commits to check.

    Returns
    -------
    StabilityResult
    """
    history = get_evolution_history(workspace_dir, limit=last_n)

    if len(history) < last_n:
        return StabilityResult(
            stable=False,
            reasons=[
                f"insufficient evolution commits: "
                f"{len(history)} found, {last_n} required"
            ],
            history=history,
            commits_checked=len(history),
        )

    reasons: list[str] = []
    all_passed = True

    # Check each commit
    for entry in history:
        short = entry.commit_hash[:8]

        # Criterion 1: No new SKILL
        passed, reason = _check_no_new_skill(entry.commit_hash, workspace_dir)
        if not passed:
            all_passed = False
            reasons.append(f"[{short}] {reason}")
        else:
            logger.debug("[%s] %s", short, reason)

        # Criterion 2: No new AGENTS module
        passed, reason = _check_no_new_agents_module(
            entry.commit_hash, workspace_dir
        )
        if not passed:
            all_passed = False
            reasons.append(f"[{short}] {reason}")
        else:
            logger.debug("[%s] %s", short, reason)

        # Criterion 3: Diff < 100 lines
        passed, reason = _check_diff_size(entry.commit_hash, workspace_dir)
        if not passed:
            all_passed = False
            reasons.append(f"[{short}] {reason}")
        else:
            logger.debug("[%s] %s", short, reason)

        # Criterion 4: Score change < 0.02
        passed, reason = _check_score_change(entry)
        if not passed:
            all_passed = False
            reasons.append(f"[{short}] {reason}")
        else:
            logger.debug("[%s] %s", short, reason)

    # Criterion 5: Error entries < 5 since the oldest checked commit
    oldest_commit = history[-1].commit_hash
    new_mistakes = _count_new_mistakes(workspace_dir, oldest_commit)
    if new_mistakes >= 5:
        all_passed = False
        reasons.append(
            f"too many new error entries: {new_mistakes} (limit 5)"
        )
    else:
        reasons.append(f"error entries OK ({new_mistakes} new)")

    if all_passed:
        reasons.insert(0, "all stability criteria passed")

    return StabilityResult(
        stable=all_passed,
        reasons=reasons,
        history=history,
        commits_checked=len(history),
    )


def create_release_branch(
    workspace_dir: str = "/opt/qdata",
) -> str:
    """Create a release branch and tag for today if stability criteria pass.

    Returns
    -------
    str
        Branch name on success, ``"duplicate"`` if a release for today
        already exists, or ``"unstable"`` if stability check fails.
    """
    today = date.today().isoformat()
    branch_name = f"release/{today}"
    tag_name = f"stable-{today}"

    # 1. Check for duplicate
    res = _run_git(["branch", "--list", branch_name], workspace_dir)
    if res.returncode == 0 and branch_name in res.stdout:
        logger.info("Release branch %s already exists", branch_name)
        return "duplicate"

    # Also check tags
    res = _run_git(["tag", "--list", tag_name], workspace_dir)
    if res.returncode == 0 and tag_name in res.stdout:
        logger.info("Release tag %s already exists", tag_name)
        return "duplicate"

    # 2. Stability gate
    stability = check_stability(workspace_dir)
    if not stability.stable:
        logger.warning(
            "Stability check failed — release branch not created: %s",
            "; ".join(stability.reasons),
        )
        return "unstable"

    # 3. Create branch
    res = _run_git(["branch", branch_name], workspace_dir)
    if res.returncode != 0:
        logger.error("Failed to create branch: %s", res.stderr.strip())
        return f"error: {res.stderr.strip()}"

    # 4. Create tag
    res = _run_git(
        ["tag", "-a", tag_name, "-m", f"Stable release {today}"],
        workspace_dir,
    )
    if res.returncode != 0:
        logger.error("Failed to create tag: %s", res.stderr.strip())
        # Rollback the branch if tag creation fails
        _run_git(["branch", "-D", branch_name], workspace_dir)
        return f"error: {res.stderr.strip()}"

    logger.info("Created release branch %s and tag %s", branch_name, tag_name)

    # 5. Send notification (best-effort)
    _send_release_notification(branch_name, tag_name, stability)

    return branch_name


def _send_release_notification(
    branch_name: str,
    tag_name: str,
    stability: StabilityResult,
) -> bool:
    """Send a DingTalk notification about a new release branch.

    Returns ``True`` if the notification was sent, ``False`` otherwise.
    """
    try:
        from cron_jobs.notifier import load_env, send_dingtalk  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "cron_jobs.notifier unavailable — skipping release notification"
        )
        return False

    env = load_env()
    webhook = env.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        logger.warning("DINGTALK_WEBHOOK not set — skipping release notification")
        return False

    lines = [
        f"### Stable Release — {branch_name}",
        "",
        f"- **Branch**: `{branch_name}`",
        f"- **Tag**: `{tag_name}`",
        f"- **Commits checked**: {stability.commits_checked}",
        f"- **History**: {len(stability.history)} evolution entries",
        "",
        "**Stability reasons:**",
    ]
    for r in stability.reasons:
        lines.append(f"- {r}")

    markdown = "\n".join(lines)
    title = f"Stable Release {branch_name}"

    try:
        send_dingtalk(webhook, title, markdown)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("DingTalk release notification failed: %s", exc)
        return False
