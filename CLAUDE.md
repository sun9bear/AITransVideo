# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## CRITICAL: Git 协作模型（多 agent，2026-06-02 更新）

> 旧约定"在当前目录工作 / 不建 worktree / 在 main 上改 / 单人不需要分支隔离"已**过时**——它只适合单人顺序开发。现在本项目由项目主（单一所有者）+ 多个 AI agent（Claude Code / CodeX 等）协作、常并行改动相邻代码。多个 agent 共用**同一个工作树**会互相覆盖 git 状态（曾发生：HEAD 被切走、WIP 被 stash、提交落错分支），必须改用隔离工作树。

**给每个 agent 的行为底线：**

- **`main` 是集成分支**，只接收已审核的 commit；不要在 main 上堆并行进行中的 WIP。
- **当前确认只有你一个 actor 在动这个仓库**：可以直接在 main 上小步提交。
- **多 actor 并行时**：每个 agent 必须在**自己的 git worktree + feature 分支**里干活（分支命名按编排约定，如 `codex/<feature>` / `claude/<feature>`），完成后由项目主 review 合并回 main，再删分支/worktree。
- **绝对禁止**多个 agent 同时对**同一个工作树**做改变状态的 git 操作（切分支 `checkout` / `stash` / `cherry-pick` / `reset`）——这是此前所有 git 事故的根因。
- 提交只用**显式 pathspec**（`git commit -- <files>`），永远不要 `git add .`（会误纳入 `.codegraph/`、`.codex_worktrees/` 等未跟踪目录）。
- worktree 由**编排层 / 项目主在启动 agent 时建好并指向**，agent 不在共享目录里自行造 worktree。

机制细节（worktree 布局、分支命名、任务所有权分配、合并流程）以 [`docs/plans/2026-05-25-ai-agent-collaboration-orchestration-plan.md`](docs/plans/2026-05-25-ai-agent-collaboration-orchestration-plan.md) 为准；本节是底线行为约束。

## CRITICAL: 付费 API 不能自动调用

**硬性约束：任何涉及付费外部 API 的代码路径都必须由用户显式触发，禁止在 fallback / 兜底 / 异常恢复路径里静默调用。**

受约束的 API 类别：
- **Voice Clone**：MiniMax voice cloning / 其他厂商的声音克隆（每次调用都有显著费用，且会占用账户额度）
- **TTS 合成**：MiniMax TTS / VolcEngine TTS / CosyVoice TTS（大量调用时费用可观）
- **LLM 付费推理**：Gemini / DeepSeek / 其他付费模型（批量调用费用会快速累积）
- **ASR 转录**：AssemblyAI / 其他付费转录
- **任何按量计费的第三方 API**

禁止的模式：
- ❌ 在 `except Exception:` 分支自动 fallback 到付费 API
- ❌ 在 "找不到数据时自动 X" 的兜底逻辑里调用付费 API
- ❌ 在 "用户没选择时默认帮他做 X" 的便利逻辑里调用付费 API
- ❌ 在 batch / loop / retry 里无上限调用付费 API
- ❌ 支付渠道之间自动 fallback（如 Paddle/Alipay 失败时自动改走 wechatpay 重新下单）——各 provider 独立，渠道由用户在前端显式选择（plan 2026-05-22 §8.1）

允许的模式：
- ✅ 用户在前端显式点击按钮触发（例如 "克隆音色" 按钮）
- ✅ 用户在 API payload 里显式传入 `action: clone` 等指令
- ✅ 运行时路径中已经存在的、用户知情的付费调用（例如 TTS 合成本来就是 pipeline 的必经步骤）
- ✅ 管理员在 admin 后台明确配置后的自动化流程

修复付费 API 相关 bug 时的准则：
- 不要用 "自动调另一个付费 API 来绕过失败" 作为修复方案
- 优先方案：让失败显式暴露给用户，由用户决定下一步
- 次选方案：提供免费 / 本地的 fallback（例如预设音色而非克隆音色）

**曾发生的教训：** 2026-04-05 曾因在 S2 说话人审核的 fallback 路径加了自动克隆逻辑，导致 MiniMax 账户余额被两次 clone 调用耗尽。用户多次强调 "克隆应该用户主动决定" 之后才发现问题。此类失误不得重复。

## Project Overview

多用户视频翻译/配音 SaaS 工作台。React (Next.js) 前端 + Python 后端，通过 FastAPI Gateway 连接。

## Project Graphs

- New sessions should read `docs/graphs/GITNEXUS_PROJECT_GRAPH.md` first, then enter the relevant subgraph by task.
- Graph index: `docs/graphs/README.md`
- Workflow core: `docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md`
- CosyVoice / Mainland Worker: `docs/graphs/GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md`
- Express CosyVoice Auto-Clone: `docs/graphs/GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md`
- Smart Auto Review: `docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`
- Anonymous Preview / Chunked Upload: `docs/graphs/GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md`
- Jianying draft delivery: `docs/graphs/GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md`
- Review flow: `docs/graphs/GITNEXUS_REVIEW_GRAPH.md`
- Editing / Post-Edit / Regeneration: `docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md`
- Storage / Delivery / R2: `docs/graphs/GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md`
- Commercialization: `docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md`
- Free Tier / MiMo VoiceClone: `docs/graphs/GITNEXUS_FREE_TIER_GRAPH.md`
- Support / Notifications / Announcements: `docs/graphs/GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md`
- Admin / Ops / Calibration: `docs/graphs/GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md`
- Benchmark / Quality / Cost: `docs/graphs/GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md`
- Pan Backup / Archive / Restore: `docs/graphs/GITNEXUS_PAN_BACKUP_GRAPH.md`
- Use these graph docs as the fast orientation layer before deeper code reads when the task is architecture-sensitive or the codebase is unfamiliar.

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
| Gateway | all routes | 8880 | Auth, job ownership, proxy, native upload, voice clone, user voices |

Gateway 原生端点（不经过 Job API 代理）：
- `POST /job-api/jobs/{job_id}/voice-clone` — 音色克隆（含 shadow credits）
- `GET/POST/DELETE /gateway/user-voices` — 个人音色库 CRUD
- `POST /internal/user-voices/expire` — 内部：标记音色过期

> **Note:** Web UI API (port 8876) 已在 Phase 4 下线。`src/services/web_ui/server.py` 和 `handler.py` 在 2026-04-17 legacy cleanup 中彻底删除（`services.web_ui` 包只剩 `project_resolver` / `voice_library` / `translation_review` / `snapshot` / `job_managers` / `config_helpers` 等 library 模块，被 Job API 继续引用）。所有 HTTP endpoint 功能已迁移到 Job API 和 Gateway。

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

### S2 转录审校（三轮拆分，2026-04-09）

`review_transcript()` 内部拆为三轮，对外接口和 `ReviewResult` 不变：
- **Pass 1**（speaker）：音频+文本，只允许 `correct_speaker`，contract 过滤越界
- **Pass 2**（text）：纯文本，只允许 `fix_text` / `split` + glossary，contract 过滤
- **Pass 3**（voice profile）：per-speaker 音频片段 → 音色画像，在翻译审核后、音色选择前调用
- **Fallback**：Pass 1/2 任一失败 → `legacy_review_transcript_single_pass()`；Pass 3 失败 → 不回滚
- **MiMo Omni**：直接走 legacy 单次路径

关键文件：
- `src/services/transcript_reviewer.py` — `_review_pass1_speakers()` / `_review_pass2_text()` / `review_pass3_voice_profiles()`
- `src/pipeline/process.py` — 编排入口 + Pass 3 调用点

产物（每个任务 `transcript/` 下）：
- `s2_pass1_result.json` / `s2_pass2_result.json` / `s2_pass3_result.json` — 各轮原始结果
- `s2_review_result.json` — 聚合结果（排障首选）
- `s2_review_raw_response.json` — Pass 1/2 原始模型输出
- `s2_review_speaker_diff.json` — 各阶段 snapshot 对比

### 快捷版（Express）音色策略

非交互模式（`wait_for_review=False`）默认仍走预设音色匹配；只有 Express CosyVoice auto-clone canary 同时满足 Gateway availability、server-confirmed `express_consent`、admin 主开关/allowlist/cap、atomic reservation 与 worker runtime gate 时，才允许在 pipeline 内自动克隆。

- gate 不满足、样本不足、reservation denied、worker clone 失败时，Express 必须回预设音色 fallback，不得把失败伪装成克隆成功。
- 自动克隆成功后写入 `user_voices.is_temporary=true` 与 `temporary_expires_at`，并通过 worker routing 进入 CosyVoice mainland worker TTS。
- 预约与清理由 `express_clone_reservations`、reservation sweeper、temporary voice cleanup sweeper/CLI 负责；不要新增绕过 reservation 的付费 clone 路径。
- 用户显式传入 `voice_a` / `voice_b` 仍正常传递。

### 免费档（Free Tier）音色与交付策略

`service_mode="free"` 是独立模式，不是 Express alias。后端必须由 `AVT_ENABLE_FREE_TIER=true` 显式打开，前端入口必须由对应 Next flag 展示；创建任务还必须带 server-validated `free_consent.voice_rights_confirmed=true`。

- 免费档允许的自动 voiceclone 仅限 `voice_strategy=free_voiceclone` 下的 MiMo voiceclone 窄路径：pipeline stamp `voiceclone_reference_path`，`TTSGenerator` 调 `mimo_tts_provider.synthesize_voiceclone`。
- admin `free_tier_voiceclone_enabled=false` 时，free tier 继续运行但降级到 preset mapping，不应失败或绕到其他 clone provider。
- free voiceclone fallback 必须强制回 MiMo preset；不得 fallback 到 MiniMax、CosyVoice 或其他付费 voice clone provider。
- free job 有 10 分钟时长 fail-closed gate、免费水印和下载范围限制；不要让 clean audio、materials pack 或 editor draft 暴露给免费档。

### TTS & Voice Matching

三引擎统一音色匹配架构（2026-04-08）：
- **统一 Reranker**: `voice_reranker.py` — provider-agnostic 9 维评分（age/persona/pitch/maturity/energy/delivery/childlike/texture）
- **三 Provider**: MiniMax (604 音色, 41 语言) / CosyVoice (~60 中文) / VolcEngine 豆包 (1.0 ~300 / 2.0 ~30)
- **Studio 三引擎选择**: `voice_selection_review` 阶段，每说话人可独立选择不同引擎音色，前端三 Tab 切换
- **统一入口**: `voice_match_resolver.py` → dispatch 到各 provider selector → `combined_rerank()`
- **DB 数据源**: Gateway `voice_catalog` + `voice_labels` 表，`/api/internal/voice-catalog` 端点

关键文件：
- `src/services/tts/voice_reranker.py` — 共享评分模块
- `src/services/tts/minimax_voice_selector.py` — MiniMax selector（语言预过滤）
- `src/services/tts/cosyvoice_voice_selector.py` — CosyVoice selector（endpoint 过滤 + legacy fallback）
- `src/services/tts/volcengine_voice_selector.py` — VolcEngine selector
- `src/services/tts/voice_match_resolver.py` — 统一 dispatch
- `docs/plans/2026-04-08-three-engine-voice-selection-plan.md` — 详细方案文档

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

## 2026-04-17 Legacy Migration Cleanup 遗产

见 `docs/plans/2026-04-17-legacy-migration-cleanup.md`（方案）+ `tests/test_legacy_cleanup_guards.py`（契约级守卫）。4 Phase 12 commits 一次性收尾单机→Web 迁移，新增的运行时模块和约定：

**新模块 / helper：**
- `gateway/internal_auth.py` — 唯一 `internal_headers()` helper，gateway → Job API 内部调用统一用它注入 `X-Internal-Key`。admin 路由、voice-catalog / labeling 路径、CosyVoice verify 全走这个。**不要** 在各 gateway 文件里再写本地 `_internal_headers()` 副本。
- `src/services/_file_lock.py` — 跨平台 reentrant file lock (threading.RLock + fcntl/msvcrt)。用于保护 JSON registry 的 load→modify→save 序列（`VoiceRegistry` 已经用了；未来其他 JSON state file 也应复用）。

**env var 语义分工**（docker-compose.yml 已设；代码都按 `os.environ.get(NAME, "<prod default>")` 读）：
- `AIVIDEOTRANS_CONFIG_DIR` → `/opt/aivideotrans/config`（admin_settings.json / pricing_runtime.json / .env 等都在这里）
- `AIVIDEOTRANS_JOBS_DIR` → `/opt/aivideotrans/app/jobs`（Job API 的 JSON store）
- `AIVIDEOTRANS_PROJECTS_DIR` → `/opt/aivideotrans/app/projects`
- `AIVIDEOTRANS_RUNTIME_LOGS_DIR` → `/opt/aivideotrans/data/runtime_logs`
- Windows 本地开发可在 `.env` 里覆盖指向 `D:/...` 下的目录，见 `.env.example`。

**配置约定变更：**
- Gateway 业务模块**不得**硬编码 `http://localhost:8877` 或 `http://127.0.0.1:8877`，一律用 `from config import settings` + `settings.job_api_upstream`。`config.py:12` 的 default 是唯一合法落点。回归守卫：`tests/test_legacy_cleanup_guards.py::test_gateway_business_modules_no_hardcoded_job_api_url`（AST-level 扫字符串字面量）。
- `AVT_INTERNAL_API_KEY` 是必需 env，gateway 启动时 `startup_checks.validate_internal_api_key` 校验，最少 16 字符。生产部署前先 `secrets.token_urlsafe(32)` 生成。

**已删除（不要再创建）：**
- `frontend/`（旧 Vite）— `frontend-next/` 是唯一前端
- `src/services/web_ui/server.py` + `handler.py` — 仅保留 library 模块（`project_resolver`、`voice_library`、`translation_review`、`snapshot`、`job_managers`、`config_helpers`、`speaker_review`）
- `main.py` 的 `web-ui` 子命令 — 只剩 `control-panel` / `job-api` / `process` 等
- 根 `projects/` 空目录、`build/`、`tmp_local_video_repro/`

**永久回归守卫：** `tests/test_legacy_cleanup_guards.py` 10 个契约级测试（file existence + CLI 行为 + AST import graph + AST 字面量 + Caddyfile 结构）。任何回退会在 CI 立刻红。

## Phase 2 下载后端（R2 可切换，2026-04-23 落地）

对 `publish.dubbed_video` artifact 的下载路径做"服务端可切换目标"切面。方案详情见
[`docs/plans/2026-04-23-phase2-r2-download-minimal.md`](docs/plans/2026-04-23-phase2-r2-download-minimal.md)。

**硬约束（不要破坏）**：

- **前端零感知 R2**：下载 URL 永远是 `/job-api/jobs/{id}/download/publish.dubbed_video`，前端代码里**不得**出现 `r2.cloudflarestorage` / `avt-artifacts` / `X-Amz-*` / `AWS4-HMAC-SHA256` / `presigned` 字样。Phase 3+ 做上传也必须守住这条。回归守卫：`tests/test_phase2_download_backend.py::test_frontend_has_no_r2_leakage`（递归 AST 扫 `frontend-next/src/**/*.{ts,tsx,js,jsx,mjs}`）。
- **Gateway 是下载决策的唯一真源**：`gateway/storage/backend_router.py::resolve_download_target` 是唯一决定"这次下载是否走 R2"的地方。Job API 不知道 R2 存在。不要把 R2 判断散到 `job_intercept.py` 各处。
- **R2 任何异常必须自动回落 local**：HEAD / upload / presign 任一抛异常 → `resolve_download_target` 返 `None` → gateway 回 Job API 直通字节流。**用户永远不看 R2 故障**。日志用 WARNING（不是 ERROR），因为 user-visible path 仍正常；CRITICAL 只保留给启动期配置缺失。
- **默认 `AVT_DOWNLOAD_REDIRECT_BACKEND=local`**：docker-compose.yml 默认值 + `gateway/config.py` Settings 字段默认值双保险。生产只有明确 `.env` 开 `r2` + 配齐 `R2_ENDPOINT/ACCESS_KEY_ID/SECRET_ACCESS_KEY` 才切换；任一缺失 `gateway/startup_checks.py::validate_r2_backend` 启动时 CRITICAL 并自动降级回 local（不崩容器）。

**付费 API 约束覆盖情况**：R2 存储**按量计费**（PUT / GET / 出站流量）但**不属于本项目 "付费外部 API" 硬约束范围**——R2 费用是基础设施成本，不像 MiniMax clone 那样每次调用都直接扣用户可见账户额度。lazy upload 在 HEAD 404 时自动触发，符合 "有界 fallback" 而非 "费用失控 fallback" 的语义（per-key 文件锁 + idempotent HEAD 保护重复上传）。

**Event 打点**（routing-decision 语义，非 download-succeeded 语义）：

三种事件类型定义在 `src/services/jobs/events.py` SUPPORTED_EVENT_TYPES 集合：

- `download.redirect.r2` — 路由决定走 R2，返 302 presigned URL
- `download.fallback.local` — R2 路径出异常，路由切回 local 字节流
- `download.local.direct` — `backend=local` 默认分支，路由直接透传 Job API

**⚠️ 这三个事件是路由决策时打点，不是下载成功的证据**。写入时机在 `RedirectResponse` / `proxy_request` **之前**。浏览器是否真的跟了 302、local stream 是否真的流到最后，gateway 都不知道。Rollout 仪表盘 **不得** 把 `download.redirect.r2` 计数当 "R2 下载成功数"，也 **不得** 把 `download.fallback.local` 当 "R2 故障 + local 成功"——后者只是 "路由决策切到 local"，local 可能紧接着 4xx/5xx。真正的下载成功率要从 upstream access log / HTTP status 单独算。详见 [plan §11.7](docs/plans/2026-04-23-phase2-r2-download-minimal.md)。

**事件写入路径**：Gateway 侧 **不 import `services.jobs.events`**——`services.jobs.__init__.py` 会传染拉入 pydub，而 gateway 容器不装 pydub（见 `display_name_orchestrator.py:30-35`）。写入逻辑抽在独立模块 `gateway/storage/event_log.py::emit_download_event`（纯 stdlib，无 fastapi / pydub 依赖），直接手写 JSONL append 到 `{jobs_dir}/{job_id}.events.jsonl`，schema 与 `JobEvent.to_dict()` 严格对齐。`gateway/job_intercept.py._emit_download_event` 是一层薄 delegator。**未来新增 download-related event type 必须同步改**：
1. `src/services/jobs/events.py` 的 `SUPPORTED_EVENT_TYPES`
2. `gateway/storage/event_log.py` 的 `_DOWNLOAD_EVENT_TYPES`
3. 回归守卫 `test_emit_download_event_supported_types_in_sync_with_jobs_events` 会在任一侧漏改时 red。

**R2 client 重试策略**：`gateway/storage/r2_client.py` 的共享 boto3 client 配 `retries={"max_attempts": 1}` — **不重试**（`max_attempts=1` 是总尝试次数=1）。HEAD / PUT / presign 任一失败 → `backend_router.resolve_download_target` 返 `None` → 回落 local。理由：fallback 本身是安全网，重试 R2 只会延长用户感知等待——local 字节流立刻可用，UX 更好。

**Gateway 需要 jobs/ bind mount**：docker-compose.yml `gateway` service 加了与 `app` 相同的 `${AIVIDEOTRANS_ROOT}/data/jobs:/opt/aivideotrans/app/jobs` (rw) mount，确保 event JSONL 落到宿主机、与 Job API 共用一份 store。**删了这个 mount 会让所有 download event 静默丢失**。

**R2 key 形状**：`jobs/{job_id}/publish.dubbed_video{suffix}`，`suffix` 从本地文件的实际扩展名取（`.mp4` / `.mov` 等）。Dashboard 可视性 + 未来多容器格式前瞻。不要改成无后缀。

**R2 upload lock 路径**：`{jobs_dir}/_r2_upload_locks/{sha256(key)}`——**不在** artifact 目录下。理由：避免被未来 Studio `editing/commit` 的 `overwrite` / `copy_as_new` 文件搬运误扫进来。回归守卫：`tests/test_phase2_download_backend.py::test_lock_path_not_in_artifact_dir`。

**Presigned URL TTL = 120s**（不是 3600s）。窗口短，泄漏也无意义。`ResponseContentDisposition` header 同时注入 RFC 6266 `filename="..."` + RFC 5987 `filename*=UTF-8''...`，非 ASCII 字符走 `_ascii_fallback_filename` 降级，保证各浏览器下载文件名稳定可读。

## Studio 视频修改工作流（Phase 1 落地，2026-04-19）

对**已完成的 Studio 任务**（`status == succeeded`），用户可以进入修改流程对
译文 / 音色 / 单段 TTS 做增量修改，最终覆盖原任务或保存为副本。方案详情见
[`docs/plans/2026-04-18-studio-post-edit-plan.md`](docs/plans/2026-04-18-studio-post-edit-plan.md)。

**Feature flag 双端 gate（D29）**：

- 后端：`AVT_ENABLE_POST_EDIT=true` 才打开 editing 端点（enter-edit /
  editing/cancel / editing/commit）及相关 segments / voice-map mutation。
  默认 False → Gateway 返回 404。
- 前端：`NEXT_PUBLIC_ENABLE_POST_EDIT=1` 才渲染"修改"入口 + 视频修改页
  （`/workspace/{id}/edit`）。

**状态机（D21）**：

```
succeeded ──[enter-edit]──→ editing
editing   ──[mutation / touch]──→ editing（editing_touched_at 刷新）
editing   ──[editing/cancel]──→ succeeded（draft 丢弃）
editing   ──[editing/commit]──→ running (alignment → publish) ──→ succeeded
```

- `editing ∈ ACTIVE_JOB_STATUSES`（列表页轮询 / cleanup 保护）
- `editing ∉ WORKER_ACTIVE_STATUSES`（reap-stale 不误杀）
- `editor/editing/` 子目录：所有可变文件（`segments.json` / `voice_map.json` /
  `tts_segments_draft/*.wav` / `segment_status.json`）；baseline `editor/...`
  在 editing 期间**绝对不动**。
- 闲置 24h（`editing_touched_at < now - 24h`）由 `editing_idle_scanner` 自动 cancel。

**commit 两种策略**：

- `overwrite`：editing/ 覆盖 baseline；`edit_generation += 1`；跑 alignment → publish。
- `copy_as_new`：两阶段提交（D34）。Phase A 准备新目录（hardlink baseline
  + apply draft + 新 JobRecord + runner accept），失败整体回滚源 editing/
  不变。Phase B 源 status=succeeded + rm source editing/。

**付费 API 硬约束（D26）**：

commit 管线（alignment / publish 阶段代码）**永不**调用 `tts_generator.*`。
守卫测试 `tests/test_phase1_guards.py` AST 扫保护。re-TTS 只在 user-initiated
端点触发，默认 caller `_not_wired_tts_caller` 抛 NotImplementedError → 501；
真实 TTS provider wiring 待专项任务。

**关键端点清单**（见 Gateway `_is_post_edit_mutation_subpath` 白名单）：

| HTTP | 路径 | Task | 说明 |
|------|------|------|------|
| POST | `/job-api/jobs/{id}/enter-edit` | T1-1 | succeeded → editing，建 editor/editing/ |
| POST | `/job-api/jobs/{id}/editing/cancel` | T1-1 | 丢 editing/ 回 succeeded |
| POST | `/job-api/jobs/{id}/editing/commit` | T1-9 | overwrite / copy_as_new |
| GET  | `/job-api/jobs/{id}/editing/segments` | T1-2 | 读编辑态段落 |
| POST | `/job-api/jobs/{id}/segments/{sid}/update` | T1-2 | patch cn_text etc. |
| POST | `/job-api/jobs/{id}/segments/{sid}/status` | T1-2 | 状态变更 |
| POST | `/job-api/jobs/{id}/segments/{sid}/regenerate-tts` | T1-5 | 单段 re-TTS 写 draft |
| POST | `/job-api/jobs/{id}/segments/{sid}/accept-draft` | T1-5 | 接受 draft |
| POST | `/job-api/jobs/{id}/segments/{sid}/discard-draft` | T1-5 | 丢弃 draft |
| POST | `/job-api/jobs/{id}/segments/{sid}/split` | 2026-04-21 | 单切点拆分（→ 2 段），保留供旧前端调用 |
| POST | `/job-api/jobs/{id}/segments/{sid}/split-many` | **Phase 2a** | 原子多切点拆分（write-ahead journal 三态 A/B/C 恢复；plan 2026-05-17 §5.6） |
| GET  | `/job-api/jobs/{id}/suggest-split-quota` | **Phase 2b v2** | 读拆分识别配额（每任务 cap=MAX(MIN(0.2×N, anomaly), 5)；plan 2026-05-17 §5.4 v2） |
| POST | `/job-api/jobs/{id}/segments/{sid}/suggest-split` | **Phase 2b v2** | 多模态 LLM 识别说话人切点（用户显式点按钮触发；S2 Pass 1 模型复用） |
| POST | `/job-api/jobs/{id}/regenerate-all-tts` | T1-6 | 批量 re-TTS |
| GET  | `/job-api/jobs/{id}/editing/voice-map` | T1-6 | 读音色覆盖 |
| POST | `/job-api/jobs/{id}/editing/voice-map` | T1-6 | set / clear 音色 |

所有 segment 端点入参都走 `validate_segment_id`（D36 regex `^[a-z0-9_]{1,64}$`）深度防御。

## Key Conventions

- 所有 UI 文本和沟通用中文
- Next.js 16 + React 19 + TypeScript strict + Tailwind v4 + shadcn/ui
- API client is a thin `fetch` wrapper — no axios, no react-query
- 响应式设计：桌面 + 手机 web 通用
