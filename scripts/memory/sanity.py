"""Post-mutation sanity checks for workspace AGENTS.md and skill files.

Run after any automated edit to AGENTS.md or .opencode/skills/ to catch
regressions before they propagate.

Usage::

    from scripts.memory.sanity import run_all_checks
    result = run_all_checks("/opt/qdata")
    print(result.passed_count, "/", result.total_count)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """Outcome of a single sanity check."""

    name: str
    passed: bool
    details: str
    files_checked: list = field(default_factory=list)


@dataclass
class SanityResult:
    """Aggregated outcome of all sanity checks."""

    passed: bool
    checks: list
    details: str

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_line_count(
    workspace_dir: str | Path,
    max_lines: int = 200,
) -> CheckResult:
    """Fail if any AGENTS.md exceeds *max_lines*."""
    root = Path(workspace_dir)
    agents_files = list(root.rglob("AGENTS.md"))
    violations: list[str] = []

    for af in agents_files:
        try:
            content = af.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Cannot read %s, skipping", af)
            continue
        count = len(content.splitlines())
        if count > max_lines:
            violations.append(f"{af.relative_to(root)}: {count} lines")

    passed = len(violations) == 0
    details = (
        "All AGENTS.md within limit"
        if passed
        else f"Over {max_lines} lines: " + "; ".join(violations)
    )
    return CheckResult(
        name="line_count",
        passed=passed,
        details=details,
        files_checked=[str(a.relative_to(root)) for a in agents_files],
    )


def _jaccard_words(text_a: str, text_b: str) -> float:
    """Word-level Jaccard similarity between two texts."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def check_skill_dedup(
    workspace_dir: str | Path,
    threshold: float = 0.6,
) -> CheckResult:
    """Fail if any pair of SKILL.md files has Jaccard word overlap > *threshold*."""
    root = Path(workspace_dir)
    skills_dir = root / ".opencode" / "skills"
    skill_files = sorted(skills_dir.rglob("SKILL.md")) if skills_dir.is_dir() else []
    violations: list[str] = []

    for fa, fb in combinations(skill_files, 2):
        try:
            text_a = fa.read_text(encoding="utf-8")
            text_b = fb.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Cannot read %s or %s, skipping pair", fa, fb)
            continue
        sim = _jaccard_words(text_a, text_b)
        if sim > threshold:
            violations.append(
                f"{fa.parent.name} vs {fb.parent.name}: {sim:.2f}"
            )

    passed = len(violations) == 0
    details = (
        "No duplicate skills detected"
        if passed
        else "High overlap: " + "; ".join(violations)
    )
    return CheckResult(
        name="skill_dedup",
        passed=passed,
        details=details,
        files_checked=[str(f.relative_to(root)) for f in skill_files],
    )


# Regex that captures backtick-quoted paths resembling file/dir references.
# Matches paths starting with ./ or a known top-level directory followed by /.
_PATH_RE = re.compile(
    r"`(\./[\w./_-]+(?:/[\w./_-]*)*"  # ./duckdb/ashare.duckdb, ./docs/tables
    r"|"
    r"\.env"  # .env file
    r"|"
    r"(?:analysis|cron_jobs|scripts|policy|duckdb|docs|sync)/[\w./_-]*)`"  # analysis/, cron_jobs/registry.json
)

# Paths containing template placeholders or glob wildcards are not verifiable.
_SKIP_RE = re.compile(r"[<>*?{}]")


def check_reference_integrity(
    workspace_dir: str | Path,
) -> CheckResult:
    """Parse root AGENTS.md for file-path references and verify each exists."""
    root = Path(workspace_dir)
    agents_md = root / "AGENTS.md"
    if not agents_md.is_file():
        return CheckResult(
            name="reference_integrity",
            passed=False,
            details="Root AGENTS.md not found",
            files_checked=[],
        )

    try:
        text = agents_md.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read %s", agents_md)
        return CheckResult(
            name="reference_integrity",
            passed=False,
            details="Cannot read root AGENTS.md",
            files_checked=[],
        )
    raw_refs = _PATH_RE.findall(text)

    missing: list[str] = []
    checked: list[str] = []

    for ref in raw_refs:
        # Strip leading ./ prefix (not individual chars)
        clean = ref[2:] if ref.startswith("./") else ref
        if _SKIP_RE.search(clean):
            continue
        target = root / clean
        checked.append(ref)
        if not target.exists():
            missing.append(ref)

    passed = len(missing) == 0
    details = (
        f"All {len(checked)} references exist"
        if passed
        else f"Missing: {', '.join(missing)}"
    )
    return CheckResult(
        name="reference_integrity",
        passed=passed,
        details=details,
        files_checked=checked,
    )


def check_no_empty_skills(
    workspace_dir: str | Path,
    min_chars: int = 50,
) -> CheckResult:
    """Fail if any SKILL.md body is shorter than *min_chars* characters."""
    root = Path(workspace_dir)
    skills_dir = root / ".opencode" / "skills"
    skill_files = sorted(skills_dir.rglob("SKILL.md")) if skills_dir.is_dir() else []
    violations: list[str] = []

    for sf in skill_files:
        try:
            body = sf.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Cannot read %s, skipping", sf)
            continue
        body_len = len(body)
        if body_len < min_chars:
            violations.append(f"{sf.parent.name}: {body_len} chars")

    passed = len(violations) == 0
    details = (
        "All skill files have sufficient content"
        if passed
        else f"Too short (<{min_chars} chars): " + "; ".join(violations)
    )
    return CheckResult(
        name="no_empty_skills",
        passed=passed,
        details=details,
        files_checked=[str(f.relative_to(root)) for f in skill_files],
    )


def check_skill_count(
    workspace_dir: str | Path,
    max_skills: int = 15,
) -> CheckResult:
    """Fail if the number of skill directories exceeds *max_skills*."""
    root = Path(workspace_dir)
    skills_dir = root / ".opencode" / "skills"
    if not skills_dir.is_dir():
        return CheckResult(
            name="skill_count",
            passed=True,
            details="No .opencode/skills/ directory found",
            files_checked=[],
        )

    skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
    count = len(skill_dirs)
    passed = count <= max_skills
    details = (
        f"{count} skill(s) within limit ({max_skills})"
        if passed
        else f"{count} skills exceeds limit ({max_skills})"
    )
    return CheckResult(
        name="skill_count",
        passed=passed,
        details=details,
        files_checked=[d.name for d in skill_dirs],
    )


_SEED_SECTIONS = ("环境", "数据采集能力", "增量同步速查", "关键约束速查")


def check_seed_identity(
    workspace_dir: str | Path,
) -> CheckResult:
    """Verify root AGENTS.md contains the required seed sections."""
    root = Path(workspace_dir)
    agents_md = root / "AGENTS.md"
    if not agents_md.is_file():
        return CheckResult(
            name="seed_identity",
            passed=False,
            details="Root AGENTS.md not found",
            files_checked=[],
        )

    try:
        text = agents_md.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read %s", agents_md)
        return CheckResult(
            name="seed_identity",
            passed=False,
            details="Cannot read root AGENTS.md",
            files_checked=[],
        )
    missing = [s for s in _SEED_SECTIONS if s not in text]
    passed = len(missing) == 0
    details = (
        "All seed sections present"
        if passed
        else f"Missing sections: {', '.join(missing)}"
    )
    return CheckResult(
        name="seed_identity",
        passed=passed,
        details=details,
        files_checked=["AGENTS.md"],
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

_ALL_CHECKS = (
    check_line_count,
    check_skill_dedup,
    check_reference_integrity,
    check_no_empty_skills,
    check_skill_count,
    check_seed_identity,
)


def run_all_checks(workspace_dir: str | Path) -> SanityResult:
    """Execute every sanity check and return an aggregated result."""
    results = [check(workspace_dir) for check in _ALL_CHECKS]
    all_passed = all(r.passed for r in results)
    failed_names = [r.name for r in results if not r.passed]
    details = (
        "All checks passed"
        if all_passed
        else f"Failed: {', '.join(failed_names)}"
    )
    return SanityResult(passed=all_passed, checks=results, details=details)
