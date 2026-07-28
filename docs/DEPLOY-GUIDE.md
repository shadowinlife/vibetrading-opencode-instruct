# opencode-serve:v2.0.0-mymain 部署指南

> 镜像: `registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2/opencode-serve:v2.0.0-mymain`

## 1. 拉取镜像

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2/opencode-serve:v2.0.0-mymain
docker tag registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2/opencode-serve:v2.0.0-mymain opencode-serve:v2.0.0-mymain
```

## 2. 创建目录结构

```bash
mkdir -p ~/opencode-serve/volumes/{vt-memory,cron-state,cron-logs}
cd ~/opencode-serve
```

## 3. 配置环境变量

```bash
cat > .env << 'EOF'
# ===== 必须 =====
DASHSCOPE_API_KEY=sk-your-dashscope-api-key
OPENCODE_SERVER_PASSWORD=your-password-here

# ===== ClickHouse =====
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=ashare
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your-clickhouse-password

# ===== 通知（可选）=====
# DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
EOF
```

## 4. 启动

```bash
docker run -d \
  --name opencode-web \
  --restart unless-stopped \
  -p 4096:4096 \
  --env-file .env \
  -v $(pwd)/volumes/vt-memory:/workspace/.vt-memory \
  -v $(pwd)/volumes/cron-state:/workspace/cron_jobs/state \
  -v $(pwd)/volumes/cron-logs:/workspace/cron_jobs/logs \
  --memory 6g \
  --cpus 0.8 \
  opencode-serve:v2.0.0-mymain
```

## 5. 验证

```bash
# 等待启动（约 30-60s）
sleep 30

# 健康检查
curl http://localhost:4096/health

# 查看启动日志
docker logs opencode-web 2>&1 | grep '\[entrypoint\]'
```

预期输出：
```
[entrypoint] opencode.json rendered from template
[entrypoint] ClickHouse OK — warming schema cache
[entrypoint] VT MCP server OK — 59 tools registered
[entrypoint] VT_MEMORY=full, VT_MEMORY_MCP_TOOLS=1 → memory tools enabled
```

## 6. 网络说明

容器默认使用宿主机网络栈（bridge 模式），**外网和 ClickHouse 访问均无需额外配置**：

| 目标 | 说明 |
|------|------|
| 外网（DashScope API, 新浪财经等） | bridge 模式默认 NAT 出站，无需配置 |
| ClickHouse | 直接通过 `CLICKHOUSE_HOST` 连接，容器内可解析宿主机网络可达的任意地址 |

**特殊情况**：

- **ClickHouse 在宿主机 localhost**：`CLICKHOUSE_HOST=host.docker.internal`（macOS/Windows）或 `CLICKHOUSE_HOST=172.17.0.1`（Linux）
- **ClickHouse 在同 VPC 内网**：直接用内网 IP，如 `CLICKHOUSE_HOST=10.0.0.5`
- **需要固定出口 IP**：配置 Docker 网络 + 路由

## 7. 常用操作

```bash
# 查看日志
docker logs -f opencode-web

# 重启
docker restart opencode-web

# 停止
docker stop opencode-web

# 进入容器
docker exec -it opencode-web bash
source /opt/venv/bin/activate

# 检查记忆目录
docker exec opencode-web ls -la /workspace/.vt-memory/

# 测试 ClickHouse 连通性
docker exec opencode-web /opt/venv/bin/python3 -c "
from clickhouse_driver import Client
c = Client(host='$CLICKHOUSE_HOST', port=$CLICKHOUSE_PORT, user='$CLICKHOUSE_USER', password='$CLICKHOUSE_PASSWORD')
print(c.execute('SELECT 1'))
"
```