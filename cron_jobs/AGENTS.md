# cron_jobs/ Directory Guide

## 职责
管理周期性策略执行、OpenCode 触发和信号通知。

## 文件结构
```text
cron_jobs/
├── registry.json   # 任务注册表
├── manage.py       # CLI 管理工具
├── trigger.sh      # cron 调用入口
├── notifier.py     # 钉钉/邮件通知
└── logs/           # 运行日志
```

## 关键约定
- 所有任务通过 `python cron_jobs/manage.py` 管理。
- `registry.json` 不存放敏感密钥。
- SMTP、Webhook 密钥等敏感值从环境变量或 `.env` 读取。
- 日志只写入 `cron_jobs/logs/`。
- **每次触发必须通知**: 所有周期任务每次执行都必须发送通知。

## 关联 Skill
- `periodic-execution`: 周期任务创建、执行和通知。
