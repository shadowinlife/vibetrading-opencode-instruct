# =============================================================================
# Dockerfile: opencode-serve app image
# Base:  opencode-serve-base (Ubuntu 22.04 + Python 3.12 + Node 20 + OpenCode +
#        playwright + pip packages)
# 构筑: docker build --platform linux/amd64 -t opencode-serve:v2.0.0 .
# =============================================================================

FROM opencode-serve-base:latest

USER root

RUN mkdir -p /root/.config/pip && \
    printf '[global]\nindex-url=https://mirrors.aliyun.com/pypi/simple/\n\n[install]\ntrusted-host=mirrors.aliyun.com\n' \
    > /root/.config/pip/pip.conf

RUN npm install -g opencode-ai@latest

RUN opencode plugin oh-my-openagent@latest

COPY vendor/Vibe-Trading/ /opt/vibe-trading/
RUN pip install --no-cache-dir -e /opt/vibe-trading/

COPY nano-search-mcp/ /opt/nano-search-mcp/
RUN pip install --no-cache-dir -e /opt/nano-search-mcp

COPY config/opencode.json.tmpl /workspace/.opencode/opencode.json.tmpl
COPY config/oh-my-openagent.json /workspace/.opencode/oh-my-openagent.json
COPY config/tui.json /workspace/.opencode/tui.json
COPY config/package.json /workspace/.opencode/package.json
COPY config/vibe-trading-tools.json /workspace/.opencode/vibe-trading-tools.json

COPY AGENTS.md /workspace/AGENTS.md

COPY skills/ /workspace/.opencode/skills/

COPY workspace/scripts/ /workspace/scripts/
COPY workspace/cron_jobs/ /workspace/cron_jobs/

COPY entrypoint.sh /workspace/entrypoint.sh
RUN chmod +x /workspace/entrypoint.sh

RUN mkdir -p /workspace/analysis \
             /workspace/reports \
             /workspace/runs \
             /workspace/tmp \
             /workspace/cron_jobs/logs \
             /workspace/cron_jobs/state

ENV LANGCHAIN_PROVIDER=dashscope \
    LANGCHAIN_MODEL_NAME=qwen3.7-max \
    DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    LANGCHAIN_TEMPERATURE=0.3 \
    TZ=Asia/Shanghai \
    VT_MEMORY=full \
    VT_MEMORY_MCP_TOOLS=1 \
    VT_MEMORY_BASE_DIR=/workspace/.vt-memory

EXPOSE 4096

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf -o /dev/null http://localhost:4096/health || exit 1

USER opencode

ENTRYPOINT ["/workspace/entrypoint.sh"]