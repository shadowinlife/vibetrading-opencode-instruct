---
name: periodic-execution
description: |
  周期性策略执行和信号提醒管理。Use when: 周期执行、定时运行、cron、自动提醒、钉钉通知、邮件通知、策略跟踪。
argument-hint: "任务名称 + cron表达式 + 分析prompt + 通知方式"
user-invocable: true
---

# Periodic Execution Skill

Use this skill when a user wants an analysis or strategy to run automatically and notify them when signals trigger.

## Files

- `cron_jobs/registry.json`: task registry
- `cron_jobs/manage.py`: CLI for list/add/remove/pause/resume/run
- `cron_jobs/trigger.sh`: cron entrypoint
- `cron_jobs/notifier.py`: DingTalk and SMTP notifications
- `cron_jobs/logs/`: runtime logs

## Registry Shape

```json
{
  "tasks": [
    {
      "id": "task_001",
      "name": "example",
      "cron": "30 15 * * 1-5",
      "prompt": "Analyze 601777.SH",
      "skills": ["stock-analysis-workflow", "analysis-report"],
      "signal_rules": [],
      "notify": {
        "dingtalk": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
        "email": "user@example.com"
      },
      "enabled": true
    }
  ]
}
```

## CLI Examples

```bash
python cron_jobs/manage.py list
python cron_jobs/manage.py add --name "scan" --cron "30 15 * * 1-5" --prompt "Analyze signals"
python cron_jobs/manage.py pause task_001
python cron_jobs/manage.py resume task_001
python cron_jobs/manage.py remove task_001
python cron_jobs/manage.py run task_001
```

## Rules

- Manage tasks through `manage.py`; do not edit crontab manually for normal changes.
- Keep logs under `cron_jobs/logs/`.
- Do not place secrets in Git-tracked files.
- Save generated analysis reports via `analysis-report`.
