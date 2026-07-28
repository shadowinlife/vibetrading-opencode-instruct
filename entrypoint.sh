#!/usr/bin/env bash
set -euo pipefail

# ── Cleanup trap ──────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    rm -f /tmp/entrypoint_render_err 2>/dev/null || true
    exit $exit_code
}
trap cleanup EXIT

# ── Activate Python virtual environment ───────────────────────────────────────
source /opt/venv/bin/activate 2>/dev/null || true

# ── Fix broken venv symlinks (base image uv Python at /root/ is inaccessible) ──
if [ -f /usr/bin/python3 ] && [ ! -x /opt/venv/bin/python3 ]; then
    ln -sf /usr/bin/python3 /opt/venv/bin/python
    ln -sf /usr/bin/python3 /opt/venv/bin/python3
    ln -sf /usr/bin/python3.12 /opt/venv/bin/python3.11 2>/dev/null || true
    ln -sf /usr/bin/python3.12 /opt/venv/bin/python3.12 2>/dev/null || true
    echo "[entrypoint] Fixed venv python symlinks → /usr/bin/python3"
fi

# ── Read environment variables with defaults ──────────────────────────────────
CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-8123}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-ashare}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"

# ── Ensure target directory exists ────────────────────────────────────────────
mkdir -p /home/opencode/.opencode

# ── Render opencode.json from Jinja2 template ─────────────────────────────────
TEMPLATE="/workspace/.opencode/opencode.json.tmpl"
TARGET="/home/opencode/.opencode/opencode.json"
FALLBACK="/workspace/.opencode/opencode.json.fallback"

render_config() {
    /opt/venv/bin/python3 -c "
import os, sys, json
from jinja2 import Template

try:
    with open('$TEMPLATE') as f:
        tmpl = Template(f.read())
    ctx = {
        'CLICKHOUSE_HOST':     os.environ.get('CLICKHOUSE_HOST', ''),
        'CLICKHOUSE_PORT':     os.environ.get('CLICKHOUSE_PORT', '8123'),
        'CLICKHOUSE_USER':     os.environ.get('CLICKHOUSE_USER', 'default'),
        'CLICKHOUSE_PASSWORD': os.environ.get('CLICKHOUSE_PASSWORD', ''),
        'CLICKHOUSE_DATABASE': os.environ.get('CLICKHOUSE_DATABASE', 'ashare'),
    }
    rendered = tmpl.render(**ctx)
    # Validate JSON
    json.loads(rendered)
    with open('$TARGET', 'w') as f:
        f.write(rendered)
    print('OK')
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
"
}

if render_config 2>/tmp/entrypoint_render_err; then
    echo "[entrypoint] opencode.json rendered from template → $TARGET"
    rm -f /tmp/entrypoint_render_err
else
    echo "[entrypoint] WARNING: Jinja2 render failed: $(tr '\n' ' ' < /tmp/entrypoint_render_err 2>/dev/null)"
    rm -f /tmp/entrypoint_render_err
    if [ -f "$FALLBACK" ]; then
        cp "$FALLBACK" "$TARGET"
        echo "[entrypoint] Using fallback config: $FALLBACK"
    else
        echo "[entrypoint] ERROR: No fallback config at $FALLBACK, writing minimal config"
        cat > "$TARGET" << 'EOFMIN'
{
  "model": "alibaba-cn/qwen3.7-max",
  "plugin": ["oh-my-openagent@latest"]
}
EOFMIN
    fi
fi

# ── Probe ClickHouse connectivity ─────────────────────────────────────────────
if [ -n "$CLICKHOUSE_HOST" ]; then
    if command -v clickhouse-client &>/dev/null; then
        echo "[entrypoint] Probing ClickHouse at $CLICKHOUSE_HOST:$CLICKHOUSE_PORT ..."
        if clickhouse-client \
            --host "$CLICKHOUSE_HOST" \
            --port "$CLICKHOUSE_PORT" \
            --user "$CLICKHOUSE_USER" \
            ${CLICKHOUSE_PASSWORD:+--password "$CLICKHOUSE_PASSWORD"} \
            --query "SELECT 1" \
            --connect_timeout 5 \
            --max_execution_time 5 \
            2>/dev/null; then
            echo "[entrypoint] ClickHouse OK — warming schema cache"
            clickhouse-client \
                --host "$CLICKHOUSE_HOST" \
                --port "$CLICKHOUSE_PORT" \
                --user "$CLICKHOUSE_USER" \
                ${CLICKHOUSE_PASSWORD:+--password "$CLICKHOUSE_PASSWORD"} \
                --query "SELECT count() FROM system.tables WHERE database='$CLICKHOUSE_DATABASE'" \
                --connect_timeout 5 \
                2>/dev/null || true
        else
            echo "[entrypoint] WARNING: ClickHouse unreachable at $CLICKHOUSE_HOST:$CLICKHOUSE_PORT"
        fi
    else
        echo "[entrypoint] WARNING: clickhouse-client not found, skipping ClickHouse probe"
    fi
else
    echo "[entrypoint] INFO: CLICKHOUSE_HOST not set, skipping ClickHouse probe"
fi

# ── Symlink pre-built plugin cache to runtime config location ──────────────────
# The OMO plugin is installed during build at /workspace/.opencode/node_modules/
# but opencode reads config from /home/opencode/.opencode/ at runtime.
# Without this symlink, opencode re-downloads the plugin on first startup (~30s).
if [ -d /workspace/.opencode/node_modules ] && [ ! -e /home/opencode/.opencode/node_modules ]; then
    ln -sf /workspace/.opencode/node_modules /home/opencode/.opencode/node_modules
    echo "[entrypoint] Plugin cache symlinked: /workspace/.opencode/node_modules → /home/opencode/.opencode/node_modules"
fi

# ── Start opencode serve ──────────────────────────────────────────────────────
exec opencode serve --port 4096 --hostname 0.0.0.0