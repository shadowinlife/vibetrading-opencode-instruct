# 报告交付契约（RM 私有 OSS 直传）

HTML 报告不再部署到沙箱内 nginx 或远端服务器。沙箱是短生命周期实例，
本地 URL 到不了用户浏览器；交付改走 Resource Manager（RM）的私有 OSS
出口：**RM 签发窄权限 STS → 沙箱直传 OSS → RM 校验后就绪**。

## 三步流程

1. **取上传凭据**
   `POST {RM}/api/v1/agent/report-upload-credential`
   - 认证：per-box agent token（Bearer），文件位于
     `/home/opencode/.config/rm/agent-token`（置备时写入，永不外传）。
   - RM 地址来自沙箱预置环境变量 `RM_AGENT_API_URL`。
   - 返回：`report_id`、`object_key`（`reports/{user_id}/{report_id}.html`）、
     `bucket`、内网 `endpoint`、STS 三元组
     （`access_key_id` / `access_key_secret` / `security_token`）、`expiration`。
   - 凭据寿命约 900 秒，权限仅 `PutObject` 于本用户前缀——读不到、列不出、
     删不了任何对象（含他人数据）。

2. **内网直传 OSS**
   `oss2.StsAuth(id, secret, token)` +
   `oss2.Bucket(auth, "https://" + endpoint, bucket)` →
   `put_object(object_key, html_bytes, headers=...)`。
   - 必须走**内网 endpoint**（沙箱同地域 VPC 可达；公网默认 endpoint 对
     新账号的数据类 API 受限）。
   - 上传头固定：`Content-Type: text/html; charset=utf-8`、
     `Cache-Control: private, no-store`、`Content-Disposition: inline`。

3. **确认就绪**
   `POST {RM}/api/v1/agent/report-uploaded`，body
   `{report_id, sha256, size}`（sha256/size 为上传字节的摘要与长度）。
   RM 用自有凭据 HEAD 校验对象存在与大小一致后，报告行 `uploading → ready`。
   确认是一次性的：重复调用返回 409。

就绪后，用户在 RM Web 报告列表查看（独立报告域内联渲染）或签发 12 小时
无账户分享链。沙箱侧没有可分享 URL；`report_id` 是唯一的交付回执。

## 自包含 HTML（硬约束）

报告在报告域以 `CSP: sandbox allow-scripts`（不透明源）渲染：脚本可执行，
但**任何外部请求都不可用**。因此：

- ECharts 以内联方式打进每个报告（技能内置 vendored 构建，Apache-2.0）。
- 字体用系统 CJK 字体栈，禁止 base64 内联字体与 CDN 字体。
- CSS 全部内联；禁止外部 `<script src>`、`<link href>`、CSS `url(http…)`。
- 主题切换等脚本必须容忍不透明源下 `localStorage` 抛错（会话内降级）。

`upload_report.py` 在取凭据前做自包含检查，不合格直接拒绝上传。

## 用法

```bash
# 沙箱内（oss2 需先安装，见 requirements.txt；VPC 内可用内网 PyPI 镜像）
python scripts/reports/upload_report.py report.html

# 显式指定 RM 地址 / token 文件
python scripts/reports/upload_report.py report.html \
  --rm-url http://rm.internal:8900 --token-file /home/opencode/.config/rm/agent-token

# 只做自包含校验与流程预览（零网络调用）
python scripts/reports/upload_report.py report.html --dry-run
```

成功输出只含 `report_id`、状态与字节数。凭据（agent token、STS 三元组）
只读一次、仅存内存，任何输出与错误信息都不携带凭据。

## 错误语义

| 现象 | 含义 | 处置 |
|------|------|------|
| HTTP 404（取凭据） | RM 报告出口开关关闭或路由不存在 | 告知用户出口未启用，勿重试 |
| HTTP 401 | agent token 缺失/失效 | 检查 token 文件；勿把 token 内容写入日志 |
| HTTP 502 | RM 侧存储暂不可用 | 可稍后重试一次；仍失败则如实上报 |
| 确认 409 | 对象缺失/大小不符/重复确认 | 不重复确认；重新走完整三步 |
| OSS 403 | STS 过期或越权 | 重新取凭据；绝不尝试他人前缀 |