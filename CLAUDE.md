# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## CRITICAL: Do NOT create worktrees or new branches

- **直接在当前目录工作**，不要创建 git worktree
- **直接在 main 分支上修改**，不要新建分支
- 这是一个单人开发项目，不需要分支隔离

## Project Overview

多用户视频翻译/配音 SaaS 工作台。React (Next.js) 前端 + Python 后端，通过 FastAPI Gateway 连接。

## Common Commands

Frontend (Next.js) — 在 `frontend-next/` 目录下运行:

```bash
cd frontend-next
npm run dev          # Next.js dev server at http://localhost:3000
npm run build        # next build (standalone output)
npm run lint         # eslint
```

Python backend tests (from repo root):

```bash
python -m pytest tests/
```

## Architecture

### Backend API (proxied by Gateway)

| API | Gateway Route | Backend Port | Purpose |
|-----|--------------|-------------|---------|
| Job API | `/job-api/*` | 8877 | Job CRUD, status, logs, artifacts, review state, voice library, downloads |
| Gateway | all routes | 8880 | Auth, job ownership, proxy, native upload |

> **Note:** Web UI API (port 8876) 已在 Phase 4 下线。所有功能已迁移到 Job API 和 Gateway。

### Frontend: `frontend-next/src/`

- `app/` — Next.js App Router pages
- `components/` — Shared UI components (shadcn/ui in `ui/`)
- `features/` — Business logic, presentation helpers
- `lib/api/` — Fetch-based API client
- `lib/react/` — Custom hooks (`usePollingTask`)
- `types/` — TypeScript interfaces

### State Management

No Redux. Each page manages state via `useState` + API fetch. Job status polling via `usePollingTask()`.

### Design System

- **Theme**: Dark-first (Synthetix Dark), with light mode toggle
- **Colors**: Purple #8B5CF6 (primary) + Cyan #06B6D4 (secondary)
- **Fonts**: Space Grotesk (headings) + Inter (body) + JetBrains Mono (code)
- **CSS**: Tailwind v4, configured in `globals.css` via `@theme inline`
- **Components**: shadcn/ui

### Deployment

Docker Compose: `app` (Python) + `postgres` + `gateway` (FastAPI) + `caddy` (HTTPS).
Production frontend: Next.js standalone build served by Caddy.

两台远程主机统一通过 `D:\daili\scripts\` 下的 `*-Via-154.cmd` 脚本部署。

#### ⚠️ 容器代码部署注意

`aivideotrans-app` 容器的 `/opt/aivideotrans/app/` **不是 bind mount**。
主机上修改该路径下的文件**对容器不可见**。只有以下目录是 bind mount：
- `/opt/aivideotrans/config` → config
- `/opt/aivideotrans/data/projects` → projects
- `/opt/aivideotrans/data/jobs` → jobs

**部署 Python 代码到容器必须用 `docker cp` + `docker restart`：**
```bash
docker cp <file> aivideotrans-app:/opt/aivideotrans/app/<path>
docker restart aivideotrans-app
```

**做任何应用层结论前，先验证容器内运行态代码来源：**
```bash
docker exec aivideotrans-app python -c "import inspect; from <module> import <cls>; print(inspect.getsource(<cls>.<method>))"
```

**开发期代码热更新模式（2026-03-30 启用）：**
docker-compose.yml 已配置 `src/`、`main.py`、`scripts/` 的 bind mount。
主机修改代码后只需 `docker restart aivideotrans-app`。

**⚠️ 项目接近完成时，必须切回镜像不可变模式：**
删除 docker-compose.yml 中标注"开发期代码热更新 bind mount"的 3 个 volume 条目，
改为 `docker-compose build app` + `docker-compose up -d app`。

## Key Conventions

- 所有 UI 文本和沟通用中文
- Next.js 16 + React 19 + TypeScript strict + Tailwind v4 + shadcn/ui
- API client is a thin `fetch` wrapper — no axios, no react-query
- 响应式设计：桌面 + 手机 web 通用
