# Cron Jobs 目录约定

本目录管理周期策略执行任务。

## 核心文件

| 文件 | 用途 |
|------|------|
| `manage.py` | CLI 管理工具（注册/暂停/恢复/删除/验证） |
| `notifier.py` | 钉钉/邮件通知 |
| `trigger.sh` | Cron 调用入口 |
| `registry.json` | 任务注册表 |
| `watchlist.json` | 监控标的列表 |
| `signal_rules.schema.json` | 信号规则 Schema |

## 关键规则

1. **每次执行必须通知**：所有周期任务每次执行都必须发送钉钉通知，无论结果如何。
2. **通知正文必须包含执行日期**（YYYY-MM-DD）。
3. **使用 `opencode run --attach`**：`curl POST /session` 只创建空壳 session，不触发 agent 执行。
4. **新任务必须验证**：创建后系统自动调度 5 分钟测试 cron，验证通过后正式部署。
5. **日志完整性**：每个执行日志必须包含 PROMPT、SESSION_ID、EXIT_CODE、STDOUT、STDERR。

## 管理命令

```bash
python cron_jobs/manage.py list              # 列出所有任务
python cron_jobs/manage.py add --name "..." --cron "..." --prompt "..."  # 注册新任务
python cron_jobs/manage.py pause <task_id>   # 暂停任务
python cron_jobs/manage.py resume <task_id>  # 恢复任务
python cron_jobs/manage.py remove <task_id>  # 删除任务
python cron_jobs/manage.py verify-test <task_id>  # 验证测试执行
```