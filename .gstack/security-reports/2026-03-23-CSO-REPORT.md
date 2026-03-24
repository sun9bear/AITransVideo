# CSO 安全审计报告 — AIVideoTrans_Codex_web_mvp

**日期:** 2026-03-23
**模式:** Daily (8/10 置信度门槛)
**范围:** 全量审计 (Phase 0-14)
**工具:** gstack /cso v2.0.0

---

## 架构概要

```
[用户] → [Caddy HTTPS :443] → [Gateway :8880] → [Job API :8877]
                                                → [Web UI API :8876]
                              → [Next.js :3000]
[Gateway] → [PostgreSQL :5432] (认证、任务归属)
```

**技术栈:** Python (FastAPI gateway) + Next.js 16 (React 19) + PostgreSQL 16 + Caddy
**部署:** Docker Compose, 全容器 network_mode: host

---

## 攻击面清单

| 类别 | 数量 |
|------|------|
| 公开端点 (无需认证) | 5 |
| 认证端点 | 8 |
| API 端点总计 | 13 |
| 文件上传点 | 1 |
| 外部集成 (AI API) | 4 |
| 后台任务 | 1 |
| 容器配置 | 4 |
| CI/CD 流水线 | 0 |

---

## 发现汇总

| # | 严重性 | 置信度 | 状态 | 类别 | 发现 | 文件 | 修复状态 |
|---|--------|--------|------|------|------|------|----------|
| 1 | CRITICAL | 9/10 | VERIFIED | OWASP A05 | CORS allow_origins=["*"] + credentials | gateway/main.py:54 | **已修复** |
| 2 | CRITICAL | 9/10 | VERIFIED | OWASP A01 | auth_required 默认 False | gateway/config.py:21 | **已修复** |
| 3 | CRITICAL | 10/10 | VERIFIED | 秘密泄露 | 生产凭证明文存磁盘 (test_login.py) | test_login.py | **已缓解** (.gitignore) |
| 4 | HIGH | 9/10 | VERIFIED | 秘密泄露 | PG 默认密码 avt_dev_2026 | docker-compose.yml:48 | **已修复** |
| 5 | HIGH | 10/10 | VERIFIED | 基础设施 | 容器以 root 运行 | gateway/Dockerfile | 待修复 |
| 6 | HIGH | 10/10 | VERIFIED | CI/CD | 无任何 CI/CD 流水线 | (仓库根目录) | 待修复 |
| 7 | HIGH | 9/10 | VERIFIED | 供应链 | package-lock.json 未提交 | frontend-next/ | 待修复 |
| 8 | HIGH | 9/10 | VERIFIED | OWASP A07 | 密码最低 6 位，无频率限制 | gateway/auth.py:124 | 待修复 |
| 9 | HIGH | 9/10 | VERIFIED | LLM 安全 | 用户可注入任意翻译提示词 | web_ui/handler.py:681 | 待修复 |
| 10 | HIGH | 9/10 | VERIFIED | OWASP A10 | yt-dlp SSRF — youtube_url 无域名白名单 | downloader.py:158 | **已修复** |
| 11 | HIGH | 9/10 | VERIFIED | OWASP A01 | /api/project-file 权限 fail-open | job_intercept.py:193 | **已修复** |
| 12 | HIGH | 10/10 | VERIFIED | OWASP A01 | Auth 关闭时 Swagger UI 暴露 | gateway/main.py:48 | **已修复** (auth 现默认开启) |
| 13 | HIGH | 9/10 | VERIFIED | OWASP A07 | 登录/注册无频率限制 | gateway/auth.py:114 | 待修复 |
| 14 | MEDIUM | 8/10 | VERIFIED | 基础设施 | 无 .dockerignore | gateway/Dockerfile:8 | 待修复 |
| 15 | MEDIUM | 8/10 | VERIFIED | 基础设施 | network_mode: host 绕过网关 | docker-compose.yml:8 | 待修复 |
| 16 | MEDIUM | 9/10 | VERIFIED | OWASP A07 | Next.js middleware 跳过 /api/* 认证 | middleware.ts:13 | 待修复 |

---

## 已修复详情 (P0)

### Finding 1: CORS 通配符 + 凭证 — 已修复

**修复前:** `allow_origins=["*"]` + `allow_credentials=True`
**修复后:** CORS origins 从 `settings.cors_origins` 读取，默认 `https://aivideotrans.site`
**可配置:** 环境变量 `AVT_CORS_ORIGINS="https://aivideotrans.site,https://other.com"`
**文件:** `gateway/main.py`, `gateway/config.py`

### Finding 2: Auth 默认关闭 — 已修复

**修复前:** `auth_required: bool = False`
**修复后:** `auth_required: bool = True`
**本地开发:** 设 `AVT_AUTH_REQUIRED=false` 关闭认证
**文件:** `gateway/config.py`

### Finding 3: 生产凭证泄露 — 已缓解

**修复:** `test_login.py` 和 `ssh_upload.py` 已加入 `.gitignore`
**仍需手动操作:**
1. 登录生产环境，修改 `sun9bear@126.com` 的密码
2. 可选：删除本地 `test_login.py` 文件

### Finding 12: Swagger UI 暴露 — 已修复 (附带)

auth_required 改为默认 True 后，`docs_url` 自动为 None，Swagger UI 不再暴露。

---

## 已修复详情 (P1)

### Finding 4: PG 默认密码 — 已修复 + 已部署

**修复前:** `POSTGRES_PASSWORD: ${PG_PASSWORD:-avt_dev_2026}` — 未设置时静默使用弱密码
**修复后:**
- `docker-compose.yml`: 密码改为 `${PG_PASSWORD:?必须设置}`，未设置时拒绝启动
- `gateway/config.py`: 新增 `AVT_PG_PASSWORD` 环境变量，自动 URL 编码后拼接 DATABASE_URL（解决密码含 `@` 等特殊字符破坏 URL 解析的问题）
- 两台远程主机 PG 实际密码已通过 `ALTER USER` 修改
- 两台远程主机 `.env` 已添加 `PG_PASSWORD`
**部署状态:** SG (5.223.84.82) healthy | US (5.78.122.220) healthy

### Finding 10: yt-dlp SSRF — 已修复

**修复前:** `youtube_url` 直接传给 yt-dlp，无任何校验，可访问 `file://`、内网地址等
**修复后:** 添加 `validate_video_url()` 函数，强制校验:
- 仅允许 `http`/`https` 协议
- 域名白名单: youtube.com, youtu.be, bilibili.com 及其子域名
- 不在白名单内的域名直接拒绝
**文件:** `src/modules/ingestion/youtube/downloader.py`
**扩展:** 如需支持更多视频站，在 `_ALLOWED_DOMAINS` 集合中添加

### Finding 11: project-file fail-open — 已修复

**修复前:** 如果请求路径中没有匹配的 job_id 段，请求直接放行 (fail-open)
**修复后:** fail-closed — 必须在路径中找到一个属于当前用户的 job_id 才放行，否则返回 403
**文件:** `gateway/job_intercept.py`

---

## 修复进度

**P0 (3/3 已修复):** CORS 通配符、auth 默认关闭、凭证泄露
**P1 (3/3 已修复 + 已部署):** PG 默认密码、yt-dlp SSRF、project-file fail-open
**部署:** 两台主机 gateway 已重建并 healthy (2026-03-23 23:44 UTC)

## 剩余待修复

| 优先级 | # | 修复项 | 预估 (CC+gstack) |
|--------|---|--------|-------------------|
| P2 本周 | 8,13 | 密码策略 8+ 字符 + 登录频率限制 | 30 min |
| P2 本周 | 5 | Dockerfile 添加 USER 指令 | 15 min |
| P2 本周 | 14 | 创建 .dockerignore | 10 min |
| P2 本周 | 7 | 提交 package-lock.json | 5 min |
| P2 本周 | 6 | 添加基础 CI (GitHub Actions) | 30 min |
| P2 本周 | 9 | LLM 提示词注入 — 添加内容过滤 | 30 min |
| P3 规划 | 15 | Docker bridge 网络隔离 | 1 hour |
| P3 规划 | 16 | Next.js middleware 认证加固 | 30 min |

---

## 安全态势评分

```
初始:      ██░░░░░░░░ 3/10
修复 P0 后: ████░░░░░░ 5/10
修复 P1 后: ██████░░░░ 7/10  ← 当前
全部修复后: █████████░ 9/10
```

---

## 未发现问题 (Good)

- SQL 注入：所有查询使用 SQLAlchemy ORM 参数化 — 安全
- XSS：React 默认转义，无 dangerouslySetInnerHTML — 安全
- 硬编码 API key：源码中无硬编码密钥 — 安全
- eval/exec：无 LLM 输出被 eval 执行 — 安全
- Webhook：无 webhook 端点 — 不存在攻击面
- TLS：httpx 默认验证 TLS — 安全

---

## 免责声明

本工具不能替代专业安全审计。/cso 是 AI 辅助扫描，捕获常见漏洞模式——不全面、不保证、不替代聘请专业安全公司。对于处理敏感数据、支付或 PII 的生产系统，请聘请专业渗透测试公司。
