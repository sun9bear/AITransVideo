# Phase 2: 多用户系统 + Next.js 迁移

## Context

Phase A 已完成：核心翻译配音流程可用，音色确认/翻译审核/发言人审核前端原生化。当前系统是单用户、无认证、文件系统存储。Phase 2 目标：转变为可商用的多用户产品。

**决策：** standalone + Node 容器 | 邮箱+密码认证 | 先后端再前端

---

## Step 1: FastAPI Gateway 层（3-5 天）

现有 web_ui.py (8876) 和 jobs/api.py (8877) **零改动**，在前面加一层 FastAPI 网关。

- 新建 `gateway/` 目录：`main.py`、`proxy.py`、`config.py`、`Dockerfile`、`requirements.txt`
- 路由透传：`/api/*` → 8876，`/job-api/*` → 8877
- 预留 `/auth/*` 路由组
- Gateway 监听 8880
- 修改 `docker-compose.yml` 新增 gateway 服务
- 修改 Caddy 配置代理到 8880

**验证：** 所有现有功能通过 Gateway 不变。

---

## Step 2: PostgreSQL + 用户注册登录（5-7 天）

- Docker Compose 新增 `postgres:16-alpine`
- SQLAlchemy async + Alembic
- `users` 表（id, email, display_name, password_hash, is_active, created_at）
- `sessions` 表（id, user_id, token, expires_at）
- Gateway 实现：register / login / logout / me
- 密码 bcrypt，session HttpOnly cookie，7 天过期
- `AUTH_REQUIRED=false` 开关（此阶段不强制）

**验证：** 注册、登录、获取用户信息正常。

---

## Step 3: Job 绑定 user_id + 隔离（5-7 天）

- `jobs` 表（job_id, user_id, source_ref, status, current_stage, project_dir, review_gate JSONB, error_summary JSONB, created_at）
- Gateway 拦截：创建 job 注入 user_id，查询 job 按 user_id 过滤，文件下载校验归属
- 全局队列：同时只有一个 job running，其他排队
- 迁移脚本：现有 jobs/*.json → DB，归属 admin 用户
- voice_registry.json 保持全局共享
- 文件系统是 source of truth，DB 是索引层

**验证：** 两个用户分别提交任务，只能看到自己的。

---

## Step 4: Next.js + shadcn/ui 前端迁移（10-14 天）

新建 `frontend-next/` 目录，平行开发一次切换。

### 4.1 脚手架（0.5 天）
`create-next-app` + TS + Tailwind + App Router + shadcn/ui

### 4.2 复用代码（1 天）
直接复制：`lib/api/`、`types/`、`features/`、`lib/cost/`、`lib/react/`
适配：环境变量 `import.meta.env` → `process.env.NEXT_PUBLIC_*`

### 4.3 组件替换（2-3 天）
- AppShell → `app/layout.tsx` + shadcn Sidebar（窄导航+顶栏）
- Toast → shadcn Sonner
- StatusBadge → shadcn Badge
- 表单 → shadcn Form + react-hook-form

### 4.4 页面迁移（5-7 天）

| 现有路由 | Next.js App Router |
|---|---|
| `/translations/new` | `app/translations/new/page.tsx` |
| `/tasks/current` | `app/tasks/current/page.tsx` |
| `/projects` | `app/projects/page.tsx` |
| `/projects/:jobId` | `app/projects/[jobId]/page.tsx` |
| `/voices` | `app/voices/page.tsx` |
| `/settings` | `app/settings/page.tsx` |
| `/reviews/:jobId/*` (4个) | `app/reviews/[jobId]/*/page.tsx` |

### 4.5 Auth 页面（1-2 天）
- `app/auth/login/page.tsx`、`app/auth/register/page.tsx`
- `middleware.ts` 保护路由（standalone 支持 server middleware）
- 顶栏用户信息/登出

### 4.6 部署
- Docker Compose 新增 `next` 服务（Node.js 容器，`next start`，端口 3000）
- Caddy：`/*` → Next.js (3000)，`/api/*` `/auth/*` → Gateway (8880)

**验证：** 11 个页面功能等价 + 登录注册正常。

---

## Step 5: 启用强制认证（2-3 天）

- `AUTH_REQUIRED=true`
- Caddy 移除 basic_auth
- Next.js middleware 未登录跳转 `/auth/login`
- 创建初始 admin 账户

**验证：** 未登录无法访问，登录后完整流程正常。

---

## 最终部署架构

```
用户 → Caddy (HTTPS)
    → /api/*, /auth/* → FastAPI Gateway (8880) → web_ui (8876) / jobs_api (8877)
                                               → PostgreSQL (5432)
    → /*             → Next.js standalone (3000)
```

Docker Compose: app + gateway + postgres + next + caddy

---

## 风险应对

| 风险 | 应对 |
|---|---|
| web_ui.py 改动 | Gateway 纯透传，零改动 |
| DB 与文件系统不一致 | 文件系统为准，可重建 DB |
| 前端功能回归 | 逐页迁移+走查，保留旧代码 |
| 单机单进程限制 | Gateway 全局队列，串行执行 |
| PG 运维 | Docker volume + 每日 pg_dump |
