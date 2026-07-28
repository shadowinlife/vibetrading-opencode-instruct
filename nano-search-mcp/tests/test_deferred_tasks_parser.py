"""tests/test_deferred_tasks_parser.py — load_deferred_topics 解析器单元测试"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nano_search_mcp.tools.deferred_search import load_deferred_topics


# ── 正常路径 ───────────────────────────────────────────────


def test_parse_single_entry(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ```yaml
        - id: m2-sina-announcement
          milestone: M2
          reason: 新浪偶尔 403
          retry_condition: 稳定后重试
          search_query_template: "{ts_code} 临时公告"
          status: deferred
          created_at: "2026-04-22"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert "m2-sina-announcement" in topics
    t = topics["m2-sina-announcement"]
    assert t["milestone"] == "M2"
    assert t["search_query_template"] == "{ts_code} 临时公告"


def test_parse_multiple_entries(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ```yaml
        - id: entry-one
          milestone: M2
          reason: reason1
          retry_condition: cond1
          search_query_template: "query1"
          status: deferred
          created_at: "2026-01-01"
        - id: entry-two
          milestone: M3b
          reason: reason2
          retry_condition: cond2
          search_query_template: "query2 {industry}"
          status: deferred
          created_at: "2026-02-01"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert len(topics) == 2
    assert "entry-one" in topics
    assert "entry-two" in topics


def test_parse_skips_resolved_status(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ```yaml
        - id: resolved-entry
          milestone: M2
          reason: was blocked
          retry_condition: n/a
          search_query_template: "query"
          status: resolved
          created_at: "2026-01-01"
        - id: active-entry
          milestone: M3b
          reason: still blocked
          retry_condition: cond
          search_query_template: "active query"
          status: deferred
          created_at: "2026-02-01"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert "resolved-entry" not in topics
    assert "active-entry" in topics


def test_parse_skips_placeholder_ids(tmp_path: Path) -> None:
    """schema 示例块（含 # 注释行或 <placeholder> id）应被忽略。"""
    md = textwrap.dedent(
        """\
        ## Schema 示例

        ```yaml
        # id:                    全局唯一
        # milestone:             M2
        ```

        ## 活跃任务

        ```yaml
        - id: real-entry
          milestone: M2
          reason: real reason
          retry_condition: real cond
          search_query_template: "real query"
          status: deferred
          created_at: "2026-04-22"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert "real-entry" in topics


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    topics = load_deferred_topics(tmp_path / "does-not-exist.md")
    assert topics == {}


def test_empty_yaml_block(tmp_path: Path) -> None:
    md = "```yaml\n```\n"
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert topics == {}


def test_entry_without_id_is_skipped(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ```yaml
        - milestone: M2
          reason: no id field
          retry_condition: n/a
          search_query_template: "query"
          status: deferred
          created_at: "2026-04-22"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert len(topics) == 0


def test_duplicate_ids_last_wins(tmp_path: Path) -> None:
    """重复 id 时后者覆盖前者（解析器行为文档化）。"""
    md = textwrap.dedent(
        """\
        ```yaml
        - id: dup-entry
          milestone: M2
          reason: first
          retry_condition: cond
          search_query_template: "query-first"
          status: deferred
          created_at: "2026-01-01"
        - id: dup-entry
          milestone: M3b
          reason: second
          retry_condition: cond
          search_query_template: "query-second"
          status: deferred
          created_at: "2026-02-01"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    # 只有 1 个条目（重复被覆盖）
    assert len(topics) == 1
    assert topics["dup-entry"]["search_query_template"] == "query-second"
