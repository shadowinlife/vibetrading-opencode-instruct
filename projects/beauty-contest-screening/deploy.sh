#!/bin/bash
# deploy.sh — Deploy beauty-contest-screening to an OpenCode project
# Usage: ./deploy.sh [TARGET_DIR]
# TARGET_DIR defaults to the parent of this script's location

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="${1:-$(dirname "$SCRIPT_DIR")}"

echo "=== Beauty Contest Screening — Deploy ==="
echo "Source: $SCRIPT_DIR"
echo "Target: $TARGET_DIR"
echo ""

# 1. Copy scripts/screening/
echo "[1/5] Copying scripts/screening/ ..."
mkdir -p "$TARGET_DIR/scripts/screening"
cp -r "$SCRIPT_DIR/scripts/screening/"*.py "$TARGET_DIR/scripts/screening/"
echo "  ✓ $(ls "$TARGET_DIR/scripts/screening/"*.py | wc -l) Python files copied"

# 2. Copy SKILL.md
echo "[2/5] Copying SKILL.md ..."
mkdir -p "$TARGET_DIR/.opencode/skills/beauty-contest-screening"
cp "$SCRIPT_DIR/skills/beauty-contest-screening/SKILL.md" "$TARGET_DIR/.opencode/skills/beauty-contest-screening/"
echo "  ✓ SKILL.md copied"

# 3. Copy HTML templates
echo "[3/5] Copying HTML templates ..."
mkdir -p "$TARGET_DIR/.opencode/skills/html-report/scripts/reports/templates"
cp "$SCRIPT_DIR/html-report/templates/base.html" "$TARGET_DIR/.opencode/skills/html-report/scripts/reports/templates/"
cp "$SCRIPT_DIR/html-report/templates/screening.html" "$TARGET_DIR/.opencode/skills/html-report/scripts/reports/templates/"
echo "  ✓ 2 HTML templates copied"

# 4. Copy render function
echo "[4/5] Copying render_screening_html.py ..."
mkdir -p "$TARGET_DIR/.opencode/skills/html-report/scripts/reports"
cp "$SCRIPT_DIR/html-report/render_screening_html.py" "$TARGET_DIR/.opencode/skills/html-report/scripts/reports/"
echo "  ✓ render_screening_html.py copied"

# 5. Apply AGENTS.md patches
echo "[5/5] Applying AGENTS.md patches ..."
if [ -f "$TARGET_DIR/AGENTS.md" ]; then
    echo "  Appending to AGENTS.md ..."
    echo "" >> "$TARGET_DIR/AGENTS.md"
    cat "$SCRIPT_DIR/agents-patches/AGENTS.md.append.md" >> "$TARGET_DIR/AGENTS.md"
    echo "  ✓ AGENTS.md patched"
else
    echo "  ⚠ AGENTS.md not found in $TARGET_DIR — skipping patch"
    echo "  Copy agents-patches/AGENTS.md.append.md manually"
fi

if [ -f "$TARGET_DIR/scripts/AGENTS.md" ]; then
    echo "  Appending to scripts/AGENTS.md ..."
    echo "" >> "$TARGET_DIR/scripts/AGENTS.md"
    cat "$SCRIPT_DIR/agents-patches/scripts-AGENTS.md.append.md" >> "$TARGET_DIR/scripts/AGENTS.md"
    echo "  ✓ scripts/AGENTS.md patched"
else
    echo "  ⚠ scripts/AGENTS.md not found — skipping patch"
fi

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "Next steps:"
echo "  1. Install Python deps: pip install -r $SCRIPT_DIR/requirements.txt"
echo "  2. Ensure DuckDB exists at: \$PROJECT_DIR/duckdb/ashare.duckdb"
echo "  3. Test: cd $TARGET_DIR && python -m scripts.screening.cli --strategy beauty-contest --top-n 10"
echo "  4. Review AGENTS.md patches in: $SCRIPT_DIR/agents-patches/"
