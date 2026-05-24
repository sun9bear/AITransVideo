# AIVideoTrans 快速入门

> 本文档是新协作者的第一入口。最后更新：2026-05-24（文档状态索引整理）。

## 项目定位

多用户视频翻译/配音 SaaS 工作台。Python 后端 + React (Next.js) 前端，通过 FastAPI Gateway 连接。当前主交付目标是剪映草稿工程，不是直接渲染 MP4。

## 当前主后端拓扑

| 服务 | 端口 | 说明 |
|------|------|------|
| Gateway | 8880 | 统一入口：认证、路由、代理、商业规则 |
| Job API | 8877 | 任务 CRUD、状态、日志、产物、review |
| Next.js | 3000 | 前端页面 |

> Web UI (8876) 已在 Phase 4 废弃；`src/services/web_ui/server.py` 和 `handler.py` 已在 2026-04-17 legacy cleanup 彻底删除。所有 HTTP endpoint 功能已迁移到 Job API 和 Gateway。`services.web_ui` 包剩余 library 模块（`project_resolver` / `voice_library` / `translation_review` / `snapshot` / `job_managers` / `config_helpers` 等）仍被 Job API 引用。

Pipeline 运行时架构：

```
用户请求 → Gateway (认证 + job policy) → Job API (创建 job record)
         → ProcessJobRunner (subprocess) → ProcessPipeline
         → OutputDispatcher → editor/Jianying/R2/Pan delivery surfaces
```

当前任务模式：

| `service_mode` | 状态 | 说明 |
|---|---|---|
| `express` | 已上线 | 快速配音交付，输出面经过过滤。 |
| `studio` | 已上线/持续打磨 | 带 review 和 post-edit 的主工作台模式。 |
| `smart` | 正在实施中 | 智能版自动审核与候选音色复用；P2 launch blockers 已闭环，但仍按实施中管理，创建入口受 `AVT_ENABLE_SMART_MODE` 与 admin `smart_mode_enabled` 双层开关控制。 |

## 当前前端主线

`frontend-next/` 是当前唯一活跃的前端目录（Next.js 16 + React 19 + TypeScript + Tailwind v4 + shadcn/ui）。

旧的 `frontend/` 目录（Vite）已在 2026-04-17 legacy cleanup 彻底删除，现在只会看到 `frontend-next/`。

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
2. **`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`** — 当前项目总图
3. **`docs/graphs/README.md`** — 按任务进入对应子图
4. **`docs/plans/README.md`** — 当前方案状态索引
5. **`CLAUDE.md` / `AGENTS.md`** — 协作约束、架构不变量与 review 边界
6. **`docs/specs/2026-04-04-pricing-and-plans-api-contract.md`** — plan catalog/API 契约

### 按需阅读

- `docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md` — Smart 自动审核、candidate-first 音色策略、quality/cost sidecar
- `docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md` — pricing/trial/auth/payment/CSRF/Smart entitlement
- `docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md` — Studio/Smart post-edit、分割、重生成
- `docs/graphs/GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md` — R2、materials pack、Jianying draft 交付
- `docs/graphs/GITNEXUS_PAN_BACKUP_GRAPH.md` — Pan archive/restore 运维面
- `docs/architecture/PROCESS_WORKFLOW_CONVERGENCE.md` — process → workflow 收敛路线

### 历史背景（不要直接当成当前事实）

以下文档记录的是特定时间点的快照或调查记录，部分结论可能已被后续代码修复或架构变更所取代：

| 文档 | 冻结时间 | 说明 |
|------|---------|------|
| `docs/archive/snapshots/CURRENT_PROJECT_STATUS.md` | 2026-03-19 | Phase A 单机自用阶段的快照 |
| `docs/archive/snapshots/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md` | 2026-03-30 | TTS 路由调查记录；`--job-id` 链路已在代码层恢复 |
| `docs/archive/snapshots/COMMERCIALIZATION_HANDOVER_2026-03-30.md` | 2026-03-30 | Phase 0-5 交接文档；核心架构边界和术语仍然有效 |
| `docs/archive/` | 多个日期 | 已完成、被替代、放弃或仅作溯源的历史文档 |

## 关键术语（已锁定）

| 术语 | 含义 |
|------|------|
| `plan_code` | 用户当前套餐：`free` / `plus` / `pro` |
| `role` | 系统权限：`user` / `admin` |
| `service_mode` | 单个任务的运行方案：`express` / `studio` / `smart` |
| `quota_state` | 轻量配额状态机：`none` / `reserved` / `committed` / `released` |
| `smart_state` | Smart pipeline 跨进程状态通道，供 Gateway mirror、settlement、前端解释面读取 |

## 核心架构边界

- Gateway 是唯一商业规则入口
- Pipeline 只消费任务快照，不动态推断套餐/支付状态
- 智能版仍按实施中主线管理，前端和后端都必须消费 Gateway 的可用性与价格事实
- TTS unit 是 `SemanticBlock`，不是 subtitle line
- 对齐保持 DSP-first，rewrite loop 是 fallback
- 字幕重定时保持数学/确定性逻辑
- `process` 仍是最完整的 YouTube 兼容壳，但不是第二套架构中心
- 产物经由 `ProjectBuilder` → `OutputDispatcher` 输出
