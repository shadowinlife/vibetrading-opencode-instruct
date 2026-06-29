# scripts/AGENTS.md Patch for beauty-contest-screening

Append the following to your project's `scripts/AGENTS.md` file.

---

## Add to file structure section

**Insert location**: After the existing directory listing in the "文件结构" block (around line 13).

```text
├── screening/             # 三层选股筛选 (基本面/叙事/资金流)
```

---

## Add to "关联 Skill" section

**Insert location**: Append to the existing skill list (around line 28).

```markdown
- `beauty-contest-screening`: 选美博弈三层筛选策略。
```

---

## Full screening module reference

The `scripts/screening/` directory contains:

| File | Purpose |
|------|---------|
| `__init__.py` | Package init, exports `run_screening()` |
| `sql_templates.py` | DuckDB SQL templates for each layer |
| `layer1_fundamental.py` | Layer 1: ROE, revenue, profit, OCF, ST filter |
| `layer2_narrative.py` | Layer 2: Concept heat, research coverage, narrative stage |
| `layer3_flow.py` | Layer 3: Main force, northbound, margin, chip concentration |
| `composite.py` | Composite scoring and tier classification |
| `ablation.py` | Ablation study: per-layer contribution analysis |
| `cli.py` | CLI entry point: `python -m scripts.screening.cli` |

### CLI Usage

```bash
# Run beauty-contest screening
python -m scripts.screening.cli --strategy beauty-contest --top-n 20

# Run with custom date
python -m scripts.screening.cli --strategy beauty-contest --date 2026-06-28 --top-n 10

# Dry run (show SQL only)
python -m scripts.screening.cli --strategy beauty-contest --dry-run
```
