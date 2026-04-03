# AIVideoTrans 快速入门

> 本文档是新协作者的第一入口。最后更新：2026-04-03。

## 项目定位

多用户视频翻译/配音 SaaS 工作台。Python 后端 + React (Next.js) 前端，通过 FastAPI Gateway 连接。

## 当前主后端拓扑

| 服务 | 端口 | 说明 |
|------|------|------|
| Gateway | 8880 | 统一入口：认证、路由、代理、商业规则 |
| Job API | 8877 | 任务 CRUD、状态、日志、产物、review |
| Next.js | 3000 | 前端页面 |

> Web UI (8876) 已在 Phase 4 废弃。所有功能已迁移到 Job API 和 Gateway。

Pipeline 运行时架构：

```
用户请求 → Gateway (认证 + job policy) → Job API (创建 job record)
         → ProcessJobRunner (subprocess) → ProcessPipeline
         → OutputDispatcher → editor output
```

## 当前前端主线

`frontend-next/` 是当前唯一活跃的前端目录（Next.js 16 + React 19 + TypeScript + Tailwind v4 + shadcn/ui）。

旧的 `frontend/` 目录如果存在，不是当前主线。

## 安装与运行

### 安装 Python 依赖

根目录 Python 依赖声明在 `pyproject.toml` 中：

```bash
pip install -e ".[dev]"
```

`gateway/` 的依赖独立管理，见 `gateway/requirements.txt`。

### 查看 CLI

```bash
python main.py --help
```

### 运行测试

```bash
python -m pytest -q
```

### Docker Compose 部署

```bash
docker-compose up -d
```

包含 `app`（Python）、`postgres`、`gateway`（FastAPI）、`caddy`（HTTPS）等服务。

## 推荐阅读顺序

新协作者建议按以下顺序阅读：

1. **本文档**（`docs/QUICKSTART.md`）— 当前入口
2. **`CLAUDE.md`** — Claude Code 协作约束
3. **`AGENTS.md`** — 架构规则与 sprint 约束
4. **`docs/specs/2026-03-29-commercialization-foundation-design.md`** — 商业化唯一业务依据

### 按需阅读

- `docs/acceptance/` — 各阶段验收记录
- `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md` — process → workflow 收敛路线

### 历史背景（不要直接当成当前事实）

以下文档记录的是特定时间点的快照或调查记录，部分结论可能已被后续代码修复或架构变更所取代：

| 文档 | 冻结时间 | 说明 |
|------|---------|------|
| `docs/CURRENT_PROJECT_STATUS.md` | 2026-03-19 | Phase A 单机自用阶段的快照 |
| `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md` | 2026-03-30 | TTS 路由调查记录；`--job-id` 链路已在代码层恢复 |
| `docs/COMMERCIALIZATION_HANDOVER_2026-03-30.md` | 2026-03-30 | Phase 0-5 交接文档；核心架构边界和术语仍然有效 |

## 关键术语（已锁定）

| 术语 | 含义 |
|------|------|
| `plan_code` | 用户当前套餐：`free` / `plus` / `pro` |
| `role` | 系统权限：`user` / `admin` |
| `service_mode` | 单个任务的运行方案：`express` / `studio` |
| `quota_state` | 轻量配额状态机：`none` / `reserved` / `committed` / `released` |

## 核心架构边界

- Gateway 是唯一商业规则入口
- Pipeline 只消费任务快照，不动态推断套餐/支付状态
- `process` 仍是最完整的 YouTube 兼容壳，但不是第二套架构中心
- 产物经由 `ProjectBuilder` → `OutputDispatcher` 输出
