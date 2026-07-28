# =============================================================================
# Dockerfile: opencode-serve — ClickHouse 迁移版
# 基于: registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2/opencode-serve:latest
# 目标: OpenCode 1.18.5 + OMO 1.18.5 + ClickHouse + nano-search-mcp
# =============================================================================
# 构建: docker build --platform linux/amd64 -t opencode-serve:latest .
# 运行: docker run --platform linux/amd64 -d \
#         --name opencode-web \
#         -p 4096:4096 \
#         -e DASHSCOPE_API_KEY=sk-xxx \
#         -e OPENCODE_SERVER_PASSWORD=your-password \
#         -e DINGTALK_WEBHOOK=https://... \
#         -e SMTP_HOST=smtp.163.com \
#         -e SMTP_AUTH_CODE=xxx \
#         opencode-serve:latest
# =============================================================================

FROM registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2/opencode-serve:latest

# 基础镜像默认 USER 是 opencode，构建阶段需要 root 权限
USER root

# ---------------------------------------------------------------------------
# 0. 阿里云 PyPI 内网源 (加速构建)
# ---------------------------------------------------------------------------
RUN mkdir -p /root/.config/pip && \
    printf '[global]\nindex-url=https://mirrors.aliyun.com/pypi/simple/\n\n[install]\ntrusted-host=mirrors.aliyun.com\n' \
    > /root/.config/pip/pip.conf

# ---------------------------------------------------------------------------
# 1. 修复 venv python 符号链接 (基础镜像中 python 可能指向构建机路径, 已断裂)
# ---------------------------------------------------------------------------
RUN PYTHON_BIN=/root/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11 && \
    if [ -f "$PYTHON_BIN" ]; then \
        ln -sf "$PYTHON_BIN" /opt/venv/bin/python && \
        ln -sf "$PYTHON_BIN" /opt/venv/bin/python3 && \
        ln -sf "$PYTHON_BIN" /opt/venv/bin/python3.11; \
    fi

# ---------------------------------------------------------------------------
# 2. 升级 OpenCode CLI 到 1.18.5
# ---------------------------------------------------------------------------
RUN npm install -g opencode-ai@1.18.5

# ---------------------------------------------------------------------------
# 3. 安装 OMO 插件 1.18.5
# ---------------------------------------------------------------------------
RUN opencode plugin oh-my-openagent@latest

# ---------------------------------------------------------------------------
# 4. Vibe-Trading (mymain branch, 从 vendor/ COPY + editable install)
#    mymain 分支独有: ClickHouse 数据源 + Memory Lifecycle (5 MCP tools)
# ---------------------------------------------------------------------------
COPY vendor/Vibe-Trading/ /opt/vibe-trading/
RUN /opt/venv/bin/pip install --no-cache-dir -e /opt/vibe-trading/

# ---------------------------------------------------------------------------
# 5. 补装 Python 包
#    注意: nano-search-mcp 不在 PyPI, 通过 COPY 源码 + editable install
# ---------------------------------------------------------------------------
RUN /opt/venv/bin/pip install --no-cache-dir \
    playwright \
    plotly \
    kaleido \
    ta \
    pyharmonics \
    loguru \
    markdownify \
    pycryptodome \
    binance-connector \
    alpaca-trade-api \
    lz4 \
    logistro \
    pytest \
    typer \
    tzdata \
    asyncio-nats-client

# ---------------------------------------------------------------------------
# 6. nano-search-mcp (不在 PyPI, 从源码 COPY + editable install)
# ---------------------------------------------------------------------------
COPY nano-search-mcp/ /opt/nano-search-mcp/
RUN /opt/venv/bin/pip install --no-cache-dir -e /opt/nano-search-mcp

# ---------------------------------------------------------------------------
# 7. Playwright 浏览器 (chromium, 用于网页抓取)
# ---------------------------------------------------------------------------
RUN /opt/venv/bin/playwright install chromium --with-deps 2>/dev/null || true

# ---------------------------------------------------------------------------
# 8. 配置文件
# ---------------------------------------------------------------------------
COPY config/opencode.json.tmpl /workspace/.opencode/opencode.json.tmpl
COPY config/oh-my-openagent.json /workspace/.opencode/oh-my-openagent.json
COPY config/tui.json /workspace/.opencode/tui.json
COPY config/package.json /workspace/.opencode/package.json
COPY config/vibe-trading-tools.json /workspace/.opencode/vibe-trading-tools.json

COPY AGENTS.md /workspace/AGENTS.md

COPY skills/ /workspace/.opencode/skills/

# ---------------------------------------------------------------------------
# 9. 工作区文件
# ---------------------------------------------------------------------------
COPY workspace/scripts/ /workspace/scripts/
COPY workspace/cron_jobs/ /workspace/cron_jobs/

# ---------------------------------------------------------------------------
# 10. 入口脚本
# ---------------------------------------------------------------------------
COPY entrypoint.sh /workspace/entrypoint.sh
RUN chmod +x /workspace/entrypoint.sh

# ---------------------------------------------------------------------------
# 11. 运行时目录
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/analysis \
             /workspace/reports \
             /workspace/runs \
             /workspace/tmp \
             /workspace/cron_jobs/logs \
             /workspace/cron_jobs/state

# ---------------------------------------------------------------------------
# 12. 环境变量 (VT Memory 全量开启)
# ---------------------------------------------------------------------------
ENV LANGCHAIN_PROVIDER=dashscope \
    LANGCHAIN_MODEL_NAME=qwen3.7-max \
    DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    LANGCHAIN_TEMPERATURE=0.3 \
    TZ=Asia/Shanghai \
    VT_MEMORY=full \
    VT_MEMORY_MCP_TOOLS=1 \
    VT_MEMORY_BASE_DIR=/workspace/.vt-memory

# ---------------------------------------------------------------------------
# 13. 暴露端口 + 健康检查
# ---------------------------------------------------------------------------
EXPOSE 4096

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf -o /dev/null http://localhost:4096/health || exit 1

# ---------------------------------------------------------------------------
# 14. 运行时用户 + 入口点
# ---------------------------------------------------------------------------
USER opencode

ENTRYPOINT ["/workspace/entrypoint.sh"]