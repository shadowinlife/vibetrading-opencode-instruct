# Nginx Reports Setup Guide

> Remote server: `root@your-server.example.com`

## One-time Setup

### 1. Install nginx (if not already installed)

```bash
ssh root@your-server.example.com "which nginx || yum install -y nginx"
```

### 2. Create reports directory

```bash
ssh root@your-server.example.com "mkdir -p /opt/reports"
```

### 3. Deploy nginx configuration

```bash
# Copy config to remote server
scp docs/nginx-reports.conf root@your-server.example.com:/etc/nginx/conf.d/reports.conf

# Test configuration
ssh root@your-server.example.com "nginx -t"

# Start/reload nginx
ssh root@your-server.example.com "systemctl enable --now nginx && systemctl reload nginx"
```

### 4. Verify

```bash
# Check nginx is running
ssh root@your-server.example.com "systemctl is-active nginx"
# Expected: active

# Check reports endpoint
curl -s -o /dev/null -w "%{http_code}" http://your-server.example.com/reports/
# Expected: 200 or 403 (empty directory)

# Health check
curl http://your-server.example.com/reports/health
# Expected: OK
```

## Report Deployment

Reports are deployed via `scripts/reports/deploy_report.py`:

```bash
# Deploy to remote
python scripts/reports/deploy_report.py report.html --stock 588000.SH

# Deploy locally (for testing)
python scripts/reports/deploy_report.py report.html --stock 588000.SH --local

# Dry run (print commands without executing)
python scripts/reports/deploy_report.py report.html --stock 588000.SH --dry-run
```

## URL Format

Deployed reports are accessible at:
```
http://your-server.example.com/reports/<stock_code>/<report_name>.html
```

Example:
```
http://your-server.example.com/reports/588000.SH/2026-06-04_dual_ma_momentum.html
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 80 port already in use | Change `listen 80` to `listen 8080` in nginx-reports.conf, update deploy_report.py `--host` |
| Permission denied on /opt/reports | `ssh root@your-server.example.com "chmod 755 /opt/reports"` |
| nginx not starting | Check `ssh root@your-server.example.com "nginx -t"` for config errors |
| Reports not accessible from outside | Check cloud provider security group allows port 80 inbound |