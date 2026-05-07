# 全项目代码审计报告

**审计日期：** 2026-05-07
**审计范围：** AIVideoTrans Codex Web MVP（多用户视频翻译/配音 SaaS）
**代码规模：**
- Python 后端 `src/`：218 个 .py 文件
- Gateway `gateway/`：80 个 .py 文件
- 前端 `frontend-next/src/`：165 个 .ts/.tsx 文件
- 测试 `tests/`：242 个 .py 文件
- Alembic 迁移：016 个版本

**审计方法：** 6 个并行专项审计 Agent，覆盖安全、后端逻辑与付费 API 约束、前端质量、数据库与数据完整性、性能、架构遗留与守卫有效性。所有发现均带 `file:line` 引用与可执行的修复建议。本报告对 6 份子报告做交叉验证与系统性归并。

---

## 0. 执行摘要

### 整体结论

项目是一个迁移频繁、迭代活跃的中型 SaaS。架构主线清晰（Gateway 8880 / Job API 8877 / Pipeline / Next.js），关键安全契约（R2 前端零感知、付费 API 用户显式触发、`AVT_INTERNAL_API_KEY` 启动校验、Alipay 验签、Bcrypt + HttpOnly cookie）**主体合规**。但本次审计在每一个维度都发现了至少一条 CRITICAL 级问题，多条与"近期新增功能未补全守卫 / 历史迁移未彻底清理 / 多线程并发未处理"有关。

### Top 风险（按严重度 × 影响）

| # | 风险 | 类别 | 直接影响 |
| --- | --- | --- | --- |
| 1 | `/job-api/jobs/{id}/source-metadata` 与 `/metering` **完全无认证** | 安全 | 任意 loopback 调用方可改 metering，**伪造账单与成本读侧** |
| 2 | Idle editing scanner 因 `from src.services.web_ui import …` 错误 import 在生产**从未执行** | 后端逻辑 | `editor/editing/` 永不被 24h 自动回收 + copy_as_new Phase B 失败时手动恢复路径失效 |
| 3 | Job-store / state_manager / editing 状态全部**无 `file_lock`** | 数据完整性 | `ThreadingHTTPServer` 多线程下 load→modify→save 静默丢失更新 |
| 4 | `quota.reserve_quota` 缺 `SELECT FOR UPDATE` | 数据完整性 | 免费用户并发提交可超额消耗免费额度 |
| 5 | `_find_text_edits_without_tts` 跳过 split 段 | 后端逻辑 | 用户切分 + 改文本 + 不重生成 TTS，commit 越过 audio-sync gate，alignment 失败留下"missing wavs" |
| 6 | Alembic `env.py` 缺 4 个表的 model import（voice_catalog / voice_labels / background_tasks / label_tasks） | 数据完整性 | 任何 `alembic revision --autogenerate` 会生成 **drop 4 张生产表**的迁移 |
| 7 | `payment_webhook_events.provider_event_id` 全局 unique 而非 `(provider, event_id)` 复合 | 数据完整性 | 跨 provider 事件 ID 同名碰撞会丢支付事件 |
| 8 | Pipeline 完全串行 + `read_bytes()` 全内存读视频 + 每行 stdout 双 fsync | 性能 | 30 min 视频 +6–30s 纯 IO 等待 / 1 GB 视频下载 RSS 翻倍 / OOM 风险 |
| 9 | 根 `projects/` 空目录回归 + `tests/test_legacy_cleanup_guards.py::test_no_root_projects_dir` 已**失效** | 守卫漂移 | CI 红 + 单机 Web UI 时代遗留的伪目录混淆数据路径 |
| 10 | gateway 业务模块违反 importlib 绕过约定（`job_intercept.py:2584`） | 架构 | 任何往 `editor_package_writer` 加回 pydub 依赖会瞬间击垮 `/api/jobs/{id}/rename` |

### 必须立即修复（P0，本周内）— 已经过 Codex 二次复核

1. **`/job-api/jobs/{id}/source-metadata` 与 `/metering` 加 `Depends(_require_internal_access)`**（[gateway/main.py:307-308](gateway/main.py:307)）— 账单/额度/任务元数据可被伪造的最高风险
2. **内部端点暴露三件套**：(a) `internal_expire_voice` 加 internal access 校验（[gateway/user_voice_api.py:495](gateway/user_voice_api.py:495)）；(b) router prefix `/internal` → `/api/internal` 让 Caddy block 生效（[Caddyfile:92](Caddyfile:92)）；(c) Job API 启动校验 `AVT_INTERNAL_API_KEY` 必填，移除空 key 时 fail-open 分支（[src/services/jobs/api.py:1033-1039](src/services/jobs/api.py:1033)）
3. **`_verify_job_ownership` 改 fail-closed**（[gateway/job_intercept.py:2496](gateway/job_intercept.py:2496)）— 潜在 IDOR
4. **`quota.reserve_quota` / `release_quota` / `ensure_admin_credits_bucket` 全部加 `with_for_update()`**（[gateway/quota.py:55-76](gateway/quota.py:55)、[gateway/credits_service.py:855-892](gateway/credits_service.py:855)）— 免费额度可并发超用
5. **JSON / file state 层全局加 `file_lock`**：JobStore、StateManager、editing 三件套（segment / status / voice_map）、`admin_settings`。`ThreadingHTTPServer` 下 load-modify-save race 已确认
6. **`cleanup.py:193` 修 import 路径**（`from src.services.web_ui` → `from services.web_ui`）+ 加 startup smoke test — idle editing scanner daemon thread 在生产**从未真正运行**
7. **Alembic `env.py` 补 4 张表 model import**（voice_catalog / voice_labels / background_tasks / label_tasks）+ `idx_jobs_editing_touched_at` 同步进 `Job.__table_args__`
8. **修 `_find_text_edits_without_tts` 对 split 段 baseline 兜底**（[src/services/jobs/editing_commit.py:466-508](src/services/jobs/editing_commit.py:466)）；同时在 `split_editing_segment` 拒绝 zero-duration 半段、迁移 `voice_map` override
9. **删除根 `projects/` 空目录**（实测 `pytest -q tests/test_legacy_cleanup_guards.py::test_no_root_projects_dir` 当前红）

> 详细 P1 / P2 / P3 任务清单见 §9。

---

### 复核记录（2026-05-07，Codex 二轮独立复核）

初版报告由 6 个并行 Agent 产出后，由 Codex 做了独立复核与代码核对。复核确认绝大多数 finding 真实存在，但有 3 处需要修正：

| 编号 | 初版表述 | 复核结论 | 报告处理 |
| --- | --- | --- | --- |
| F-HIGH-3 | `admin/jobs` 的 cancel/delete + `TranslationForm` upload 三处缺 `credentials: 'include'` | **admin/jobs 实际已有**（见 page.tsx:259, 277），仅 upload-video 一处不一致 | 已撤回 admin/jobs 两条，保留 upload-video 一条；F-HIGH-3 块底部加"复核更正"标记 |
| F-HIGH-5 | `useBackgroundTask` 缺 abort 会触发"setState on unmounted warning" | `cancelled` guard 已防 setState，warning 不触发；但**资源不取消**（fetch 继续 + JSON 反序列化）是真问题 | 已修正影响描述，问题保留 |
| P-CRITICAL-4 | `/api/admin/jobs` 自身在 async route 做大量同步文件 IO | `/api/admin/jobs` 只做 outerjoin + memory merge；真正的同步 IO 重头是它**调用的 upstream Job listing**（已被 P-CRITICAL-1 涵盖）+ `/api/admin/s2-stats` | 已重述位置与重 IO 来源 |

其余 30+ 条 HIGH 与全部 CRITICAL 均经 Codex 核对真实存在并维持当前严重度。Codex 还实跑了 `pytest -q tests/test_legacy_cleanup_guards.py::test_no_root_projects_dir`，确认当前红。

#### 第二轮 Codex 复核（同日，针对修订版的二次抽样）

| 编号 | 初版表述 | 复核结论 | 报告处理 |
| --- | --- | --- | --- |
| §2.4 标签级别 | 前端两条标为 `F-CRITICAL` | 真实性成立，但 admin 列表 key + forbidden state 都是 UX/一致性问题，**严重度不应与安全/计费/数据丢失同级**。§9 已归 P2 | 标签从 `F-CRITICAL-1/2` 降为 `F-HIGH-7/8`，并在影响字段补足量级说明 |
| §6 守卫矩阵 | `test_no_root_projects_dir` 标"❌ 已失效" | 措辞不准——**测试本身有效，是被它守卫的不变量被破坏**（守卫成功 catch 到回归）。"已失效" 容易让人误以为测试本身失能 | 改为"✅ 有效，但当前红" |

二次复核同时抽样确认了以下新增项的真实性，全部成立：
- `gateway/upload.py:107-108` 同步 `shutil.copyfileobj` ✅
- `gateway/database.py:36` `pool_size=5, max_overflow=10` ✅
- `gateway/user_voice_api.py:153-208` `probe_user_voice` 无 rate limit ✅
- `.env.example` 缺多项 R2 / post-edit / Whisper / `AVT_GATEWAY_URL` ✅
- `data/minimax_seed*.sql` 死种子文件 ✅
- `AdminAuditLog` 仅覆盖 entitlement / payment upgrade，不覆盖其他 admin 写操作 ✅

§9 优先级清单方向（P0 9 项 → P1 auth/支付/IO 性能 → P2 UX/吞吐/架构 → P3 清理）经第二轮 Codex 复核**认可**，无需调整。

---

## 1. 审计范围与方法

6 个 Agent 并行扫描，每个 Agent 专注一个领域，互不重叠工作：

| Agent | 范围 | 主要文件数 |
| --- | --- | --- |
| Gateway 安全 | `gateway/` 80 文件，重点 auth/payment/R2/internal | ~25 |
| Backend 逻辑 + 付费 API | `src/pipeline/`、`src/services/jobs/`、`src/services/transcript_reviewer.py`、`src/services/tts/`、`src/services/voice/` | ~200 |
| 前端 | `frontend-next/src/` 全量 | 165 |
| 数据库 | `gateway/models.py`、`gateway/alembic/versions/001..016`、`src/services/state_manager.py`、`src/services/_file_lock.py`、`gateway/credits_service.py`、`gateway/billing.py` | ~30 |
| 性能 | 全栈热路径（pipeline、Job API、Gateway、前端 polling） | ~60 |
| 架构与守卫 | `tests/test_legacy_cleanup_guards.py`、`test_phase1_guards.py`、`test_phase2_download_backend.py`、CLAUDE.md、env / docker-compose 一致性 | ~70 |

报告中的所有 `file:line` 引用均为实测得出。每条 finding 互相独立可裁剪，便于按优先级派单。

---

## 2. CRITICAL 问题汇总（按领域）

### 2.1 安全（Gateway）

#### S-CRITICAL-1：`/source-metadata` 与 `/metering` 端点完全无认证

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/main.py:307](gateway/main.py:307)、[gateway/main.py:308](gateway/main.py:308) |
| 实现 | [gateway/job_intercept.py:2734-2906](gateway/job_intercept.py:2734)（`update_source_metadata`）、[gateway/job_intercept.py:2909-3061](gateway/job_intercept.py:2909)（`update_job_metering`） |
| 问题 | 注册时无 `Depends(require_auth)`、无 `X-Internal-Key` 校验、无 ownership。任何能到 8880 的调用方都可改 `Job.actual_minutes`、`tts_billed_chars`、`final_cn_chars`、`voice_clone_billable_count`、`title`、`display_name`。`actual_minutes` 还会触发 `reserve_credits_or_raise` 内联扣费 |
| 攻击面 | Loopback 现状下需先穿透 Caddy，但 Caddyfile 一行变更就会公网化；admin 路径若 leak 同样到达 |
| 修复 | 用 `voice_catalog_api.py:91` 的 `_require_internal_access` 同款依赖：`app.post(..., dependencies=[Depends(_require_internal_access)])(update_source_metadata)` |

#### S-CRITICAL-2：`internal_expire_voice` 跳过 internal access 检查

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/user_voice_api.py:495-522](gateway/user_voice_api.py:495) |
| 问题 | 同 router 下其他端点（`/by-voice-ids:309`、`/speed-profiles:374`）都先调 `_internal_access_error(request)`，本端点漏掉。又加上 router 用的是 `prefix="/internal"`（不是 `/api/internal`），`Caddyfile:92-97` 的 `@internal_block` 只挡 `/api/internal/*` |
| 攻击面 | 如果 Caddyfile 有任何错配把 `/internal/*` 路由到 gateway，未授权调用方可任意标记别人的音色 expired |
| 修复 | 在函数顶端加 `internal_error = _internal_access_error(request); if internal_error: raise internal_error`；同时把 `prefix="/internal"` 改成 `prefix="/api/internal"` 让 Caddy block 生效 |

### 2.2 后端逻辑 / 付费 API 约束

#### B-CRITICAL-1：Idle editing scanner 在生产环境**从未执行**

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/web_ui/cleanup.py:193](src/services/web_ui/cleanup.py:193) |
| 问题 | `from src.services.web_ui import editing_idle_scanner` — `Dockerfile:15` 设的是 `PYTHONPATH=/opt/aivideotrans/app/src`，容器内 `src` 不是顶层包，只有 `services.*` 可以 import。该 import 在 daemon 进入 `_cleanup_loop()` 第一次执行时立即 raise，**daemon 线程静默死掉** |
| 影响 | 1) `editing` 状态 + `editing_touched_at` 超 24h 永远不被自动 cancel，`editor/editing/` 累积 2) `copy_as_new` Phase B 失败的"由 idle scanner force-cancel 兜底"路径**完全不存在** |
| 修复 | `from services.web_ui import editing_idle_scanner`；加一条 startup smoke test 强制走一遍 `_cleanup_loop()` 单次循环 |
| 测试缺口 | Tests 用 `tests/conftest.py` 把 `src/` 加到 sys.path 让 `src.services.*` 也 importable，掩盖了生产容器的差异 |

#### B-CRITICAL-2：`_find_text_edits_without_tts` 跳过 split 段，audio-sync gate 漏判

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/editing_commit.py:466-508](src/services/jobs/editing_commit.py:466)（核心 487-488 行） |
| 问题 | 用户 `split_editing_segment` 之后，新 ID `seg_005_a` / `seg_005_b` 在 `editing_segments.py:838-839` 被标 `text_dirty`。但 commit 时的扫描逻辑：`baseline_segment = baseline_by_id.get(sid); if not baseline_segment: continue` —— baseline 仍然只 keys 旧的 `seg_005`，新 ID 直接 skip，`unsynced` 返回空集合 |
| 影响 | 用户 split 段 + 修改两半文字 + 不重生成 TTS → commit 通过 → `_apply_editing_to_baseline` 把新 ID 写进 baseline → alignment 阶段 [process.py:3156-3164](src/pipeline/process.py:3156) 报 `missing wavs in editor/tts_segments/: [seg_005_a, seg_005_b]`，job 落 failed，用户无明确恢复路径 |
| 修复 | baseline 缺失 + segment_id 命中 split-suffix 模式（`*_a`/`*_b`/`*_split_*`）时强制视为 unsynced（要求 draft wav 存在）。回归测试：split + 改文 + commit-without-regen → 期望 `EditingAudioSyncRequiredError` |

#### B-CRITICAL-3：Editing 状态在 `ThreadingHTTPServer` 下并发不安全

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/api.py:122-127](src/services/jobs/api.py:122)（ThreadingHTTPServer），[src/services/jobs/editing_segments.py:284-402](src/services/jobs/editing_segments.py:284)（`patch_editing_segment`），[src/services/jobs/editing_voice_map.py:118-145](src/services/jobs/editing_voice_map.py:118)（`set_voice_override`），[src/services/jobs/editing_segments.py:476-500](src/services/jobs/editing_segments.py:476)（`mark_segment_status`） |
| 问题 | `services/_file_lock.py` 已存在并被 `voice_registry.py` / `jianying_draft_runner.py` 正确使用；但 editing 路径全部裸 `load → modify → atomic_write_json`，没拿锁。`ThreadingHTTPServer` 每个请求一个 OS 线程，并发 patch / voice-map / status 必然 race |
| 影响 | 用户在两个浏览器窗口同时编辑 / 快速点击 → 后写覆盖前写，前一次编辑**静默丢失**；这是用户感知极差但极难复现的 bug |
| 修复 | 在 service 层包 `from services._file_lock import file_lock; with file_lock(project_dir / "editor" / "editing" / ".editing_lock"): ...`。锁是 reentrant，嵌套调用安全 |

#### B-CRITICAL-4：`StateManager.set_stage` 同样无 file_lock

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/state_manager.py:66-100](src/services/state_manager.py:66) |
| 问题 | `state = self.load(); …; self.save(state)`。`save()` 自身用 atomic rename，但 load → save 之间无锁。Pipeline runner 线程 + Job API HTTP 线程同时 set_stage 会丢更新 |
| 修复 | 用 `file_lock(self.state_path)` 包裹 set_stage / set_project |

### 2.3 数据库 / 数据完整性

#### D-CRITICAL-1：`quota.reserve_quota` 缺 `SELECT FOR UPDATE`

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/quota.py:55-76](gateway/quota.py:55) |
| 问题 | `select(User).where(User.id == user_id)` → `user.free_jobs_quota_used += 1`，无行锁。两个并发 free-job 创建都读 `used=4`、都写 `5`，应该 `6` 实际是 `5` |
| 影响 | 免费用户可超额消耗免费额度（具体超出量取决于并发度）。`release_quota` 同病 |
| 修复 | `select(...).with_for_update()`，与 `credits_service.reserve_credits_or_raise` 已有的模式对齐 |

#### D-CRITICAL-2：JSON job-store / state_manager / admin_settings 缺锁缺原子写

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/store.py:16-19](src/services/jobs/store.py:16)、[src/services/state_manager.py:66-100](src/services/state_manager.py:66)、[gateway/admin_settings.py:782-785](gateway/admin_settings.py:782)、[gateway/admin_settings.py:831-834](gateway/admin_settings.py:831) |
| 问题 | `JobStore` `_write_json_atomic` 只保证最终 write 原子，但所有 caller 的 `record = store.load_job(); next = replace(...); store.save_job(next)` 无锁。同一 `{job_id}.json` 同时被三个写者操作：1）Job API 请求 handler 2）`ProcessJobRunner._record_line` 后台线程 3）`src/pipeline/process.py:1308` 的 pipeline 子进程。`admin_settings.py` 直接 `read_text` + `write_text` 无 atomic、无锁 |
| 影响 | 编辑/状态/`editing_touched_at` 静默丢更新；admin 配置 crash 时损坏；并发 admin 操作互相覆盖 |
| 修复 | 全部包 `services._file_lock.file_lock(...)`；admin_settings 配 `utils.atomic_io.atomic_write_json` |

#### D-CRITICAL-3：Alembic `env.py` 漏 import 4 个生产表的 model

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/alembic/env.py:15](gateway/alembic/env.py:15) |
| 问题 | 只有 `from models import Base`。`voice_catalog`、`voice_labels`、`label_tasks`、`background_tasks` 四张表通过 005 / 006 / 014 迁移上线，但 `VoiceCatalog` / `VoiceLabel` / `BackgroundTask` / `LabelTask` 类没被 import 到 `Base.metadata` |
| 影响 | 任何人执行 `alembic revision --autogenerate -m "..."` 会生成**drop 这 4 张表**的迁移。`015` 的 `idx_jobs_editing_touched_at` partial index 同样在 model 里缺失 |
| 修复 | env.py 加：<br>`import voice_catalog_models  # noqa: F401`<br>`import background_task_models  # noqa: F401`<br>`import label_task_models  # noqa: F401`<br>同时在 `Job.__table_args__` 加 `Index("idx_jobs_editing_touched_at", "editing_touched_at", postgresql_where=text("editing_touched_at IS NOT NULL"))` |

#### D-CRITICAL-4：`payment_webhook_events.provider_event_id` 是全局 unique，不是 (provider, event_id)

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/models.py:300-303](gateway/models.py:300)，[gateway/alembic/versions/004_add_payment_tables.py:45](gateway/alembic/versions/004_add_payment_tables.py:45) |
| 问题 | 当下只有 Alipay 在用（`2026...` 数字 ID）+ fake provider。一旦上 Stripe / WeChat Pay，跨 provider 同名 ID 碰撞会被当成"重复事件"丢掉 |
| 修复 | 新迁移：drop 旧 unique → `op.create_unique_constraint("uq_payment_webhook_provider_event", "payment_webhook_events", ["provider", "provider_event_id"])` |

### 2.4 前端（说明：以下两条原标 F-CRITICAL，经 Codex 复核降为 F-HIGH——真实性成立但严重度不应与安全/计费/数据丢失同级，§9 也已归入 P2）

#### F-HIGH-7：admin 列表页用 `key={i}` 数组下标做 React key

| 字段 | 内容 |
| --- | --- |
| 位置 | [admin/jobs/page.tsx:540, 565, 596](frontend-next/src/app/(app)/admin/jobs/page.tsx:540)、[admin/credits-monitor/page.tsx:607, 710, 828](frontend-next/src/app/(app)/admin/credits-monitor/page.tsx:607)、[admin/s2-monitor/page.tsx:271](frontend-next/src/app/(app)/admin/s2-monitor/page.tsx:271)、[admin/prompts/page.tsx:437](frontend-next/src/app/(app)/admin/prompts/page.tsx:437) |
| 问题 | admin 页面 10s 轮询 + 列表顺序变化时 React 复用错 DOM。展开 metering 详情 / AbortController 错误对应等微观 bug |
| 影响 | admin 在轮询期间可能看到错误的展开 row；analyzeLogs 缓存命中错误 jobId。仅影响 admin 体验，无安全/数据问题 |
| 修复 | 全部改业务 ID（`job.job_id` / `attempt.attempt_id` / `version.version_id`） |

#### F-HIGH-8：admin/users + voices + prompts 缺 forbidden state

| 字段 | 内容 |
| --- | --- |
| 位置 | [admin/users/page.tsx:64-68](frontend-next/src/app/(app)/admin/users/page.tsx:64)、[admin/voices/page.tsx:578](frontend-next/src/app/(app)/admin/voices/page.tsx:578)、[admin/prompts/page.tsx:130-134](frontend-next/src/app/(app)/admin/prompts/page.tsx:130) |
| 问题 | 其他 admin 页面（jobs / credits-monitor / s2-monitor / traffic / costs）都有统一的 `setForbidden(true)` + "仅管理员可访问" 渲染分支；这三个页面是 `catch { toast.error("加载失败") }`，无法区分网络故障与权限拒绝 |
| 影响 | 仅 UX 一致性问题——后端确实在 enforce role check（前端 admin/jobs 的 403 处理倒推），用户最终拿不到数据。不是真正的安全边界缺失 |
| 修复 | 三个页面加 `if (resp.status === 403) { setForbidden(true); return }`，与 `admin/jobs/page.tsx:289-294` 对齐 |

### 2.5 性能（详见 §5）

8 个 CRITICAL：fsync per stdout line / Job listing 全文件 glob / `read_bytes()` 全内存 / pipeline 完全串行 / alignment 串行 / background tasks 无并发上限 / 工作台轮询拉全量 logs / admin 端点阻塞 event loop。

### 2.6 架构 / 守卫

#### A-CRITICAL-1：根 `projects/` 空目录回归

| 字段 | 内容 |
| --- | --- |
| 位置 | `D:\Claude\AIVideoTrans_Codex_web_mvp\projects\`（空目录） |
| 问题 | `tests/test_legacy_cleanup_guards.py:99-106::test_no_root_projects_dir` 断言其不存在，CI 现在跑会**立刻失败**。CLAUDE.md 明确指出真实数据在 `data/projects/` |
| 修复 | `rmdir`；`.gitignore:18` 行 `projects/` 缺前导斜杠的脆弱性同步处理 |

---

## 3. HIGH 问题详表（按领域）

每条 HIGH 给出"问题 / 影响 / 修复"完整字段，与 CRITICAL 同等详尽，方便直接派单。

---

### 3.1 Gateway 安全

#### S-HIGH-1：`_verify_job_ownership` 对 DB 缺失 job 是 fail-open

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/job_intercept.py:2496-2512](gateway/job_intercept.py:2496) |
| 问题 | 第一次 `select(Job).where(job_id == ?, user_id == ?)` 为空时，再做一次 `select(Job).where(job_id == ?)`；如果第二次也空，**只 logger.warning 就 return**，调用方继续往 upstream 代理。注释写 "legacy job?" |
| 影响 | `intercept_create_job` 对 Gateway DB 写入失败（瞬态）或行后被清理，job 在 Job API 存在但 Gateway DB 没记录。任何登录用户暴力枚举/猜测 job_id 即可读其他用户的 artifact，配合 `_maybe_r2_redirect` 还能直接拿到 R2 presigned URL |
| 修复 | 改 fail-closed：`raise HTTPException(status_code=404, detail="任务不存在")`。真正的 legacy 数据应通过 backfill 脚本补 DB 行而不是默认放行 |

#### S-HIGH-2：Captcha pre-verify 死代码，`consume_captcha_pass` 从未被调用

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/auth_phone.py:78-83](gateway/auth_phone.py:78)（定义+签发）；[gateway/auth_phone.py:195-242](gateway/auth_phone.py:195)（`send_code_endpoint` 应消费但实际重新跑 `risk_control.verify_captcha`） |
| 问题 | `pre-verify` 端点签发的 `pass_token` 从未被消费；`send-code` 直接对原 captcha_token 二次校验。Aliyun / Turnstile 等 provider 不允许 token 复用，所以**生产环境的整条 captcha 链可能都跑不通**（除非用 `AVT_CAPTCHA_PROVIDER=fake`）。同时 `_captcha_passes` dict 仅在签发时机会清理，长寿命进程无界增长 |
| 影响 | 1) Captcha 实际不生效或频繁误拒 2) 内存缓慢泄漏 |
| 修复 | 二选一：a) 把 `consume_captcha_pass(body.captcha_token)` 接到 `send_code_endpoint`，删 `verify_captcha` 二次校验；b) 删除整套 pass-token 流程。同时加定时清理 `_captcha_passes` 过期项 |

#### S-HIGH-3：`/auth/login` 无 rate limit

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/auth.py:182-235](gateway/auth.py:182) + [gateway/main.py:240](gateway/main.py:240) |
| 问题 | 密码登录路径完全无限速。Bcrypt 单次校验慢，但分布式凭据填充攻击仍可大规模并行。手机 OTP 路径有 `risk_control` 限速，密码路径漏 |
| 影响 | 邮箱/密码组合可被持续 probe；遇泄漏密码库时直接被打穿 |
| 修复 | 复用 `risk_control._RateLimiterState`，加 per-IP（5/min）+ per-account（5/min）双限。失败 5 次后给账号上 30 分钟冷却 |

#### S-HIGH-4：`verify_code_endpoint` 比较前先消费 challenge → DoS 受害者

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/auth_phone.py:281-286](gateway/auth_phone.py:281)、[gateway/auth_phone.py:477-481](gateway/auth_phone.py:477) |
| 问题 | 顺序是 `challenge.consumed_at = now; await db.commit()` **然后**才比较 OTP code。攻击者随便 spam `/verify-code` 用错码即可把受害者刚收到的有效 challenge 标记 consumed，受害者再输正确码就被拒 |
| 影响 | 受害者无法登录，且被 OTP 速率限制锁住（手机号 1/min、5/hour）。攻击成本 0，受害者恢复成本高 |
| 修复 | 先比较 code，仅在 a) code 正确，或 b) attempts ≥ 3 次错码 时才 mark consumed。`PhoneVerificationChallenge` 表加 `attempts` 列 |

#### S-HIGH-5：`X-Forwarded-For` 信任不分代理边界

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/auth_phone.py:146-155](gateway/auth_phone.py:146)（`_client_ip`）→ 流入 `risk_control.check_send_code_allowed`、`check_ip_trial_eligible_db`、`record_ip_trial_grant_db` |
| 问题 | 直接取 `X-Forwarded-For[0]` 不验来源是不是可信代理。Caddy 默认 `header_up` 是追加而非替换，所以攻击者上行带 X-Forwarded-For 也能绕过 |
| 影响 | 1) 绕过手机号发码 IP 限流（每 IP 每小时 20 次）2) 试用账号 IP 反作弊失效——攻击者可用伪造 IP 给 N 个手机号刷试用，每次 300 credits 直接变现 |
| 修复 | a) Caddy 配 `header_up X-Forwarded-For {http.request.remote.host}` 强制覆盖，或 b) Gateway 仅在 `request.client.host == "127.0.0.1"` 时信任 X-Forwarded-For，否则用 socket peer。两种方案都行，挑一种就行 |

---

### 3.2 后端逻辑

#### B-HIGH-1：`audio_utils.measure_duration_ms` ffprobe 无 timeout

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/utils/audio_utils.py:18-43](src/utils/audio_utils.py:18) |
| 问题 | `subprocess.run(['ffprobe', ...], capture_output=True, text=True, check=True)` 无 `timeout=` 参数。其他 ffmpeg/ffprobe site（separator.py:135、process.py:7343、editing_segments.py:909-936）都加了 timeout，唯独这处漏 |
| 影响 | 调用方多达 5+ 处（包括 `_populate_publish_resume_audio_paths`）。恶意/网络挂的音频文件可让 worker 线程永久阻塞，Job API ThreadingHTTPServer 一线程一请求 → 慢慢吃光 worker pool |
| 修复 | `subprocess.run(..., timeout=30)`，捕 `subprocess.TimeoutExpired` 抛 `AudioProbeError` |

#### B-HIGH-2：Job API 内部端点 `_internal_key` 空时全部放行 → 付费 Gemini 调用可被滥用

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/api.py:1033-1039](src/services/jobs/api.py:1033)（以及 538-545、1227-1235） |
| 问题 | `if _internal_key:` 才校验。env 缺失或部署遗漏时 `_internal_key = ""`，**所有 internal 端点全开**。其中 `/internal/voice-label/text` 与 `/internal/voice-label/audio/{round}` 通过 subprocess 调 `scripts/volcengine_batch_label.py`，后者读 `GEMINI_API_KEY`（付费 LLM） |
| 影响 | 部署疏忽 → 付费 Gemini 调用被任意内网调用方触发，无审计、无速率控制。Gateway 已有 `validate_internal_api_key` startup 校验，Job API 没有对称校验 |
| 修复 | Job API 启动校验 `AVT_INTERNAL_API_KEY` 必填且 ≥16 字符，模仿 `gateway/startup_checks.py::validate_internal_api_key`。把判断改 fail-closed：`if not _internal_key or req_key != _internal_key: 403` |

#### B-HIGH-3：S2 Pass 1/2/3 fallback 链无 max-spend cap

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/transcript_reviewer.py:1362-1375](src/services/transcript_reviewer.py:1362)、[transcript_reviewer.py:1655-1668](src/services/transcript_reviewer.py:1655)、[transcript_reviewer.py:2071-2083](src/services/transcript_reviewer.py:2071) |
| 问题 | 主模型失败 → retry 失败 → 遍历整个 `_fb_chain`（4-6 个候选）每一个都打一次付费 LLM。坏输入（malformed audio）会让所有模型都 reject，但循环不停，每轮都计费 |
| 影响 | 单条任务理论最大消费 = `len(_fb_chain) * 单次成本`。若未来 `llm_registry` 加候选，悄悄退化无人察觉。Pass 3 还涉及音频上传，单次成本更高 |
| 修复 | 加 `max_fallback_attempts = 2`；遇到非 transient JSON shape error 立即 break（错的是输入不是模型，再换模型也无意义） |

#### B-HIGH-4：`split_editing_segment` 允许 zero-duration 半段

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/editing_segments.py:776-779](src/services/jobs/editing_segments.py:776) |
| 问题 | `mid_ms = start_ms + int(round((end_ms - start_ms) * ratio))`。若段太短或 `ratio` 取整为 0，`mid_ms == start_ms`，A 半段 0 时长 |
| 影响 | commit 后 alignment 数学崩，用户看到诡异错误，无明显恢复路径 |
| 修复 | 计算后加 `if mid_ms <= start_ms or mid_ms >= end_ms: raise ValueError("split would produce zero-duration half")` |

#### B-HIGH-5：split 不迁移 voice_map override

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/jobs/editing_segments.py:707-849](src/services/jobs/editing_segments.py:707)（split 流程）+ [src/services/jobs/editing_voice_map.py](src/services/jobs/editing_voice_map.py)（load_voice_map） |
| 问题 | 用户为 `seg_005` 显式选了音色 X，split 后产生 `seg_005_a/_b`。voice_map 还以 `seg_005` 为 key，没人接，commit 时 `_apply_voice_map` 找不到对应段就丢 key，新两半静默退回 speaker default |
| 影响 | 用户的音色选择被静默丢弃，难复现的 UX bug |
| 修复 | split 完成后检查 `voice_map[old_id]`，propagate 到两个新 ID（或至少 A 半段），然后 clear 旧 key。加回归测试 |

---

### 3.3 前端

#### F-HIGH-1：`WorkspacePage useEffect` deps + `loadJob` 非 useCallback → stale closure 隐患

| 字段 | 内容 |
| --- | --- |
| 位置 | [frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:135-150](frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:135) |
| 问题 | auto-approve `translation_config_review` 的 useEffect deps 是 `[job]`，内部却调用普通函数 `loadJob(true)`。`loadJob` 每次 render 重新创建，但通过 `autoApprovedRef.current[job.id]` ref 防重。eslint 不能分析这个隐式依赖 |
| 影响 | 当前能跑，但任何后续修 `loadJob` 实现的人会陷入 stale closure 而无 lint 提示 |
| 修复 | `loadJob` 改 `useCallback(async (silent = false) => { ... }, [jobId])`，deps 写 `[job, loadJob]` |

#### F-HIGH-2：`usePollingTask` 不感知 visibility

| 字段 | 内容 |
| --- | --- |
| 位置 | [frontend-next/src/lib/react/usePollingTask.ts](frontend-next/src/lib/react/usePollingTask.ts)（hook 主体） |
| 问题 | 用户切到后台 tab 后仍每 4s 拉 3 个 endpoint。多个工作台 tab 同开，服务器侧 N 倍负载。`useBackgroundTask` 也无 abort signal |
| 影响 | QPS 雪崩，浪费用户带宽 + 服务器 CPU/IO |
| 修复 | hook 内部加 `document.addEventListener('visibilitychange', …)`；`document.hidden` 时清 interval、recover 时恢复。同时给 `useBackgroundTask` 接 AbortController（caller `admin/jobs/page.tsx:179` 已有正确范式可复用） |

#### F-HIGH-3：`TranslationForm.upload-video` 缺 `credentials: 'include'`

| 字段 | 内容 |
| --- | --- |
| 位置 | [frontend-next/src/components/workspace/TranslationForm.tsx:280](frontend-next/src/components/workspace/TranslationForm.tsx:280) |
| 问题 | 同源 fetch 浏览器默认 `same-origin` 仍带 cookie，但项目其他 fetch 全显式 `credentials: 'include'`。upload-video 这一处不一致。Caddy 反代 / Service Worker 介入时可能 cookie 丢失导致 401 |
| 影响 | 边缘场景上传失败，用户感知"网络故障" |
| 修复 | 加 `credentials: 'include'` |
| 复核更正 | 初版报告同时列了 `admin/jobs/page.tsx:257`（cancel）和 `:275`（delete）为同病。Codex 二次复核：这两处实际**已有** `credentials: "include"`（见文件 `:259` 与 `:277`）。本审计撤回该两条，仅 upload-video 一处成立 |

#### F-HIGH-4：`edit/page.tsx` 1907 行单文件巨型组件

| 字段 | 内容 |
| --- | --- |
| 位置 | [frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx](frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx)（整文件） |
| 问题 | 单 client component 包含 `VideoEditPage`(950)+`SegmentCard`(530)+`AudioSyncConflictModal`(80)+`CommitModal`(90)+辅助。Bundle 不能 code-split，任何 SegmentCard 重渲拖累 page-level state。`VoiceModifyTab.tsx` 1044 行同病 |
| 影响 | 维护性差 + bundle 体积 + re-render 性能 |
| 修复 | 拆 `SegmentCard.tsx` / `CommitModal.tsx` / `AudioSyncConflictModal.tsx` / `EditingHooks.ts`（13+ useCallback 抽到 hooks 文件） |

#### F-HIGH-5：`useBackgroundTask` fetch 无 AbortController（资源不取消）

| 字段 | 内容 |
| --- | --- |
| 位置 | [frontend-next/src/lib/react/useBackgroundTask.ts:222-244](frontend-next/src/lib/react/useBackgroundTask.ts:222) |
| 问题 | mount effect 用 `cancelled` 标志位防 setState（保证状态层正确性），但 fetch 本身没接 AbortController。组件 unmount 后**HTTP 请求继续完成 + JSON 反序列化照走**，仅是结果不再 setState |
| 影响 | 用户快速切换 ResultMediaCard 卡片时累积浪费的并发请求 + 内存 + 带宽。`admin/jobs/page.tsx:179` 已经有 AbortController 的正确范式可对照 |
| 修复 | `fetchLatest` / `fetchTask` / `createTask` 加 `signal?: AbortSignal` 参数，mount effect 创建 controller 并 cleanup 时 abort |
| 复核更正 | 初版报告写"setState on unmounted component warning"。Codex 复核：因 `cancelled` guard 已防 setState，该 warning 实际不触发；但**资源不取消**这一真问题保留 |

#### F-HIGH-6：多页面同时 polling 无共享 hook → fetch 雪崩

| 字段 | 内容 |
| --- | --- |
| 位置 | `WorkspacePage`(4s) + `TranslationForm`(5s listJobs) + `ProjectsContent`(4s listJobs) + `admin/jobs`(10s) + `admin/voices` modal polling |
| 问题 | 用户在 `/workspace/{id}` 页同时打开 NewTranslationDialog 时，两个 listJobs polling 并发（4s + 5s）。切到 `/projects` 又切回，`prevJobIdsRef` 状态不一致 |
| 影响 | 浪费后端配额，前端重复渲染 |
| 修复 | 抽 `useJobsList()` 共享 hook，stale-while-revalidate 语义。或上 SWR（违反"无 react-query"约定但量级到了） |

---

### 3.4 数据库

#### D-HIGH-1：015 partial index `idx_jobs_editing_touched_at` 在 model 缺失

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/alembic/versions/015_add_post_edit_fields.py:111-116](gateway/alembic/versions/015_add_post_edit_fields.py:111) vs [gateway/models.py:115-124](gateway/models.py:115) |
| 问题 | 迁移建了 `WHERE editing_touched_at IS NOT NULL` partial index；`Job.__table_args__` 列了 5 个 index 但漏了这个 |
| 影响 | autogenerate 会建议 drop。索引被 drop 后 `editing_idle_scanner` 查询退化为全表扫 |
| 修复 | model 加 `Index("idx_jobs_editing_touched_at", "editing_touched_at", postgresql_where=text("editing_touched_at IS NOT NULL"))` |

#### D-HIGH-2：`Session.expires_at` 无索引

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/models.py:628-643](gateway/models.py:628) + [gateway/auth.py:65-69](gateway/auth.py:65) |
| 问题 | `auth.create_session` 每次都跑 `DELETE FROM sessions WHERE expires_at <= NOW()` 做 opportunistic purge，但 expires_at 没索引 |
| 影响 | 累积到 10k+ session 后，每次登录的 cleanup DELETE 触发全表扫；同一事务里还要插新 session，行锁竞争加剧 |
| 修复 | `Session.__table_args__ = (Index("idx_sessions_expires_at", "expires_at"),)` + 迁移 `op.create_index(..., postgresql_concurrently=True)` |

#### D-HIGH-3：`pricing_config_versions.version` 无 unique

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/models.py:595-625](gateway/models.py:595) + [gateway/pricing_admin.py:125-142](gateway/pricing_admin.py:125) |
| 问题 | 计算下一版本号是 `select(func.max(version)) + 1` 后 INSERT，无锁。两个管理员同时 Save Draft 都读到 max=7，都插入 version=8 |
| 影响 | 重复版本号；admin UI 显示两个 v8；`desc(created_at)` 还能挑出"active"那条但语义混乱 |
| 修复 | a) `UniqueConstraint("version")` + 迁移 b) 用 advisory lock 或 `with_for_update` 串行 read-max + insert |

#### D-HIGH-4：Alembic 007 downgrade 在有 phone-only 用户时**会失败**

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/alembic/versions/007_add_phone_and_trial_fields.py:95-117](gateway/alembic/versions/007_add_phone_and_trial_fields.py:95) |
| 问题 | downgrade 把 `users.email` / `users.password_hash` 改回 `nullable=False`，但 phone 注册的用户 email 与 password_hash 都 NULL，alter 立即报"column contains null values" |
| 影响 | DR 回滚时必炸；目前没有 phone-only 用户的 DB 才能 rollback |
| 修复 | 二选一：a) downgrade 不重新加 NOT NULL（接受 schema 不对称）b) 先 `op.execute("DELETE FROM users WHERE email IS NULL OR password_hash IS NULL")` 再 alter，并在 docstring 明确数据丢失 |

#### D-HIGH-5：`ensure_admin_credits_bucket` top-up 无锁

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/credits_service.py:855-892](gateway/credits_service.py:855) |
| 问题 | top-up 分支 `existing.granted = ... + delta; existing.remaining = ... + delta` 没 `with_for_update()`。两个并发 admin probe 都读 `granted=900_000`，都加 100_000 写回 1_000_000，丢一次 top-up |
| 影响 | admin probe 不频繁，量级低，但与 `credits_service` 其他位置一致性破坏，是隐性 footgun |
| 修复 | `select(CreditsBucket).where(...).with_for_update()` |

#### D-HIGH-6：`pricing_runtime` 缓存无跨进程失效

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/pricing_runtime.py:18-61](gateway/pricing_runtime.py:18) |
| 问题 | `_cache` 是模块级。`invalidate_runtime_pricing_cache()` 只清当前 process。若 uvicorn 多 worker，A 进程发布新价 → B/C 进程读旧价直到重启 |
| 影响 | 多 worker 部署下 stale 价格 |
| 修复 | a) 单 worker 部署写文档，b) 每次 read 用 `os.path.getmtime` 校验文件 mtime 决定是否重 load，c) Redis pub/sub 广播 invalidate |

#### D-HIGH-7：`_process_payment_event` 多 commit 切割单事务

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/billing.py:706, 728, 737, 823](gateway/billing.py:706) |
| 问题 | 同函数里 4 处 `await db.commit()`。早期 return 已 commit webhook 事件行但还没改 order/user，后续再 mutate session 进入新事务。当下勉强工作，但任何在 SELECT 与 early-commit 之间加新 mutation 都会泄漏 |
| 影响 | 改动易 leak；重复 webhook 并发时无清晰 dedup branch，仅靠 unique constraint catch，日志噪音 |
| 修复 | 重构为单一 commit + 显式 short-circuit 标志位；或用 `INSERT ... ON CONFLICT DO NOTHING RETURNING id` 一把搞定，无 row 即 dedup |

---

### 3.5 性能 — 全 8 条均为 CRITICAL（不再分 HIGH）

详细量级与修复见性能 Agent 输出，下面是按 ROI 排序的速查表（完整 Top 10 见 §5）：

| ID | 标题 | 位置 | 量级估计 |
| --- | --- | --- | --- |
| P-CRITICAL-1 | Job listing 全文件 glob + JSON parse | [src/services/jobs/store.py:63-80](src/services/jobs/store.py:63)、被 [api.py:134](src/services/jobs/api.py:134) 调用 | 1000 jobs → 200-800 ms / 调用；前端 4s × 5 用户并发可打死 Job API |
| P-CRITICAL-2 | 每条 pipeline stdout 行触发全 JSON rewrite + 2 次 fsync | [src/services/jobs/process_runner.py:375-473](src/services/jobs/process_runner.py:375)、[store.py:103-119](src/services/jobs/store.py:103)、[store.py:37-45](src/services/jobs/store.py:37) | 3000 行 × 2 fsync = 6-30s 纯 IO（SSD）/ 3-5 min（HDD）；写盘 60-180 MB |
| P-CRITICAL-3 | `download_path.read_bytes()` 全内存读视频 | [src/services/jobs/api.py:308](src/services/jobs/api.py:308)、`review_actions.py:212/309/859/893` | 1 GB 视频 → Python RSS +1 GB；2 并发即可 OOM |
| P-CRITICAL-4 | `/api/admin/s2-stats` 在 async route 内同步读 4 个 JSON / job；`/api/admin/jobs` 通过调用 upstream Job listing 间接放大 P-CRITICAL-1 | [gateway/s2_monitor_api.py:329-486](gateway/s2_monitor_api.py:329)（重 IO 真源）；[gateway/admin_settings.py:893-941](gateway/admin_settings.py:893)（仅 outerjoin + memory merge，重在 upstream） | s2-stats：200 jobs × 4 文件 = 200-1000 ms 卡 event loop |
| P-CRITICAL-5 | Pipeline 完全串行（audio_sep / 转录 / S2 1/2 互不依赖） | [src/pipeline/process.py](src/pipeline/process.py) `run` 8000+ 行 | 30 min 视频可省 30-40% 总耗时 |
| P-CRITICAL-6 | Alignment 200 段 ffmpeg 串行 | [src/services/alignment/aligner.py:168-212](src/services/alignment/aligner.py:168) | 200 × 2s = 400s → 4 worker → 110s |
| P-CRITICAL-7 | `generate_video` / `materials_pack` 无并发上限 | [gateway/background_task_api.py:83](gateway/background_task_api.py:83)、[src/services/jobs/video_render_async.py:90-108](src/services/jobs/video_render_async.py:90) | 4 用户并发 → CPU 400% + 容易 OOM |
| P-CRITICAL-8 | 工作台 4s 轮询同时拉 3 endpoints + 全量 events | [workspace/[jobId]/page.tsx:121](frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:121)、[lib/api/jobs.ts:134-137](frontend-next/src/lib/api/jobs.ts:134) | 单 active job 110 KB/s 无效带宽，redactor 每 4s 50-200 ms 同步 CPU |

修复方向参考 §4 系统性问题 #5 与 §9 P1 / P2 清单。

---

### 3.6 架构 / 守卫

#### A-HIGH-1：`tmp_source_video.mkv` + `demo_output/` 杂物

| 字段 | 内容 |
| --- | --- |
| 位置 | `D:\Claude\AIVideoTrans_Codex_web_mvp\tmp_source_video.mkv`（10 字节、2026-03-31）；`D:\Claude\AIVideoTrans_Codex_web_mvp\demo_output\sprint_4b_demo\`（2026-04-24） |
| 问题 | tmp 是 10 字节占位文件早于 .gitignore 规则；demo_output 已 ignore 但目录本身仍在 |
| 影响 | 工作树污染；新会话可能误以为这些是有效产物 |
| 修复 | `rm tmp_source_video.mkv && rm -rf demo_output/` |

#### A-HIGH-2：`gateway/job_intercept.py:2584` 直接 import `services.jobs.display_name`，违反 importlib 绕过约定

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/job_intercept.py:2584](gateway/job_intercept.py:2584) `from services.jobs.display_name import resolve_collision` |
| 问题 | CLAUDE.md 与 `gateway/log_redactor_loader.py:8-35` + `gateway/display_name_orchestrator.py:37-43` 三处明文规定 gateway **不能** 直接 import `services.jobs.*`（pydub 链传染风险）。本文件已经为 `build_default_redactor` 用了 `importlib.spec_from_file_location` 绕过，但同一文件第 2584 行又直接 import |
| 影响 | 当前 `editor_package_writer` 不再 import pydub 所以暂时不炸——但任何后续给 `editor_package_writer` / `output_dispatcher` / `process_runner` 加回 pydub/audio 依赖的改动会让 `/api/jobs/{id}/rename` 在生产瞬间崩 |
| 修复 | a) 用 `display_name_orchestrator.py` 同款 importlib 模式加载 `resolve_collision`；b) 把 `resolve_collision` 抽到 `src/services/jobs/display_name` 同级 `_pure` 子模块让 gateway 安全直 import |

#### A-HIGH-3：CLAUDE.md 行号引用 `display_name_orchestrator.py:30-35` 已漂移

| 字段 | 内容 |
| --- | --- |
| 位置 | CLAUDE.md "事件写入路径" 段落 + [gateway/storage/event_log.py:15](gateway/storage/event_log.py:15) |
| 问题 | CLAUDE.md 多处引用 `display_name_orchestrator.py:30-35` 解释 pydub 警告，但实际那几行是空白行 + `__all__` 声明 + 模块变量。pydub 警告真正在 38-43 行（`_load_display_name_module` docstring） |
| 影响 | 新会话按文档跳行号找不到所声称的内容；其他类似引用可能也漂移 |
| 修复 | 把所有行号引用改成函数名引用："见 `gateway/display_name_orchestrator.py::_load_display_name_module`"。CLAUDE.md 内永不写绝对行号 |

#### A-HIGH-4：`_verify_job_ownership` 在 gateway 有两份不同实现

| 字段 | 内容 |
| --- | --- |
| 位置 | [gateway/job_intercept.py:2496-2513](gateway/job_intercept.py:2496) vs [gateway/voice_selection_api.py:121-138](gateway/voice_selection_api.py:121) |
| 问题 | 两个实现签名不同（一个返 None，一个返 `Job | None`），行为也不同：前者 fail-open（"legacy job?" 兜底），后者 fail-closed |
| 影响 | 同一 job_id 在两个端点产生不同 ownership 决策。修一边漏另一边 → IDOR 面扩大 |
| 修复 | 抽 `gateway/_job_ownership.py` 共享 helper（参考 `gateway/internal_auth.py` 抽 `internal_headers` 的模式），两端共用，统一"legacy job 找不到"策略 |

#### A-HIGH-5：`_internal_headers()` 在 4 个 worker 文件复制

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/tts/voice_reranker.py:357-365](src/services/tts/voice_reranker.py:357)、[src/services/tts/volcengine_voice_catalog.py:467-474](src/services/tts/volcengine_voice_catalog.py:467)、[src/services/tts/voice_speed_catalog.py:33-43](src/services/tts/voice_speed_catalog.py:33)、[src/services/tts/cosyvoice_voice_catalog.py:188-197](src/services/tts/cosyvoice_voice_catalog.py:188) |
| 问题 | 4 份独立实现的 `_internal_headers()` worker → gateway 用。CLAUDE.md "唯一 internal_headers helper" 只覆盖 gateway → Job API 方向，没覆盖 worker → gateway 方向 |
| 影响 | 4 份 header 协议变更要改 4 处；新增 trace-id header 易遗漏 |
| 修复 | 抽 `src/services/_internal_headers.py`（与 `_file_lock.py` 同级）；加 AST 守卫扫 `def _internal_headers` 定义点 ≤ 1 |

#### A-HIGH-6：worker → gateway URL 5+ 处硬编码 `127.0.0.1:8880`

| 字段 | 内容 |
| --- | --- |
| 位置 | `src/services/tts/voice_reranker.py:354`、`voice_speed_catalog.py:25,29`、`volcengine_voice_catalog.py:460`、`minimax_voice_selector.py:71`、`cosyvoice_voice_catalog.py:181` + `src/pipeline/process.py:400, 471, 3706, 7673` |
| 问题 | gateway 端有契约级守卫禁硬编码 8877，但**没对称守卫**禁止 worker 硬编码 8880。`process.py` 部分用 `os.environ.get("AVT_GATEWAY_URL", ...)` 合规，但 `src/services/tts/` 大部分直接写 `_GATEWAY_URL = "http://127.0.0.1:8880/..."` |
| 影响 | gateway 换端口/挪主机要改 5+ 处；同 8877 教训 |
| 修复 | 抽 `src/services/_gateway_endpoint.py`，所有人统一从 `AVT_GATEWAY_URL` env 读；加守卫 `tests/test_internal_endpoint_url_centralized.py` AST 扫禁止字面量 |

#### A-HIGH-7：`src/services/web_ui/project_resolver.py` 中 3 个公开未使用函数

| 字段 | 内容 |
| --- | --- |
| 位置 | [project_resolver.py:352-386](src/services/web_ui/project_resolver.py:352) `_resolve_allowed_project_file_download_path`、[project_resolver.py:459-484](src/services/web_ui/project_resolver.py:459) `_resolve_project_dir_by_job_id`、[project_resolver.py:326-350](src/services/web_ui/project_resolver.py:326) `_build_current_project_audio_preview_paths`（仅被前者调用，传递性死） |
| 问题 | 全仓 grep 这 3 个函数没有任何 caller。Web UI 单机时代音频预览端点的支撑代码，端点删了函数没跟着删 |
| 影响 | 维护成本——每次 audit 都要重新确认未用 |
| 修复 | 直接删 3 个函数。git 历史会保留 |

#### A-HIGH-8：`src/services/gemini/translator.py:46-50` 4 个 DEFAULT_* deprecated 常量

| 字段 | 内容 |
| --- | --- |
| 位置 | [src/services/gemini/translator.py:46-50](src/services/gemini/translator.py:46)（`DEFAULT_DYNAMIC_DENSITY_MIN/MAX`、`DEFAULT_TRANSLATION_LENGTH_UNDERSHOOT/OVERSHOOT_FACTOR`） |
| 问题 | 注释明示 "deprecated — kept for reference"。grep 全仓未发现引用（除 archive/）。`docs/archive/plans/2026-04-12-probe-tts-calibration.md:190` 已规划"删除" |
| 影响 | 维护噪音 |
| 修复 | 删 4 行常量 |

---

## 4. 跨 Agent 交叉印证：系统性问题

把多个 Agent 的发现叠在一起看，可以归出 5 个**根因层面**的系统性问题。修一处不够，要一次治理。

### 系统性问题 #1：JSON / 文件状态层全局缺并发与原子性治理

**Agent 见证：**
- B-CRITICAL-3（editing 状态无锁）+ B-CRITICAL-4（state_manager 无锁）— Backend Agent
- D-CRITICAL-2（job-store / admin_settings 无锁、不原子）— DB Agent
- P-CRITICAL-2（每行 stdout 全 JSON rewrite + 2 fsync）— 性能 Agent

**根因：** `services/_file_lock.py` 已建好，但只在 `voice_registry.py` 与 `jianying_draft_runner.py` 用了。其他四个写热点（JobStore / StateManager / editing 三件套 / admin_settings）漏接。

**根因 #2：** Job API 仍是 `ThreadingHTTPServer` 同步 stdlib 实现（V1 单机迁移遗产），与"现代 SaaS 多线程下 file-based state"完全不匹配。

**统一治理方案：**
1. 短期：所有 load→modify→save 路径统一加 `with file_lock(...)` 包裹；admin_settings 改 `atomic_write_json`。所有 fsync 改成 group commit（每 N 行或 1s 一次）。
2. 中期：Job state 整体迁到 PostgreSQL（已有 Job 表结构作为 mirror），废 JSON file store。
3. 长期：Job API 也迁到 FastAPI/uvicorn（与 Gateway 共享技术栈），换 `asyncpg`。

### 系统性问题 #2：付费 API 防护层"知道点 + 不知道线"

**Agent 见证：**
- 付费 API 守卫（`tests/test_phase1_guards.py`）确实成功阻止了"alignment / publish 调 TTS"的回归 — Backend Agent
- 但 Pass 1/2/3 fallback 链无 max-spend cap、`probe_user_voice` 无 rate limit、Job API 内部端点缺 env 时 fail-open（→ Gemini 调用不受控）、`_resolve_or_auto_clone_voice` 死代码尚未删
- Gateway 有 `validate_internal_api_key` startup 校验，Job API 无对称校验 — Backend Agent

**根因：** 守卫只覆盖了 commit 边界这一条线（最严重的 2026-04-05 教训对应的修补），没覆盖所有付费调用的入口。

**统一治理方案：**
1. 给 Pass 1/2/3 加 `max_fallback_attempts=2`，遇到非 transient 错误立即停止级联
2. `probe_user_voice` 加 per-user rate limit（10/min, 100/day）+ 计入 credits
3. Job API 启动校验 `AVT_INTERNAL_API_KEY` 必填且 ≥16 字符，移除"empty key 时跳过"分支
4. 删 `src/pipeline/process.py:5005-5104` 的 `_resolve_or_auto_clone_voice` 孤儿函数
5. `tests/test_phase1_guards.py` 的 AST 扫描扩到 `src/pipeline/`，覆盖未来新孤儿

### 系统性问题 #3：迁移路径治理不彻底

**Agent 见证：**
- A-CRITICAL-1：根 `projects/` 回归 — 架构 Agent
- A-HIGH-1：`tmp_source_video.mkv` + `demo_output/` 杂物 — 架构 Agent
- A-HIGH-2：gateway 直接 import `services.jobs.display_name` 违反 importlib 绕过约定 — 架构 Agent
- A-HIGH-3：CLAUDE.md 行号引用漂移 — 架构 Agent
- DEPRECATED 字段（`review_model` / `translation_model` / `voice_clone_cost_credits`）保留过期 — 架构 Agent
- 死代码：`_resolve_or_auto_clone_voice`、`project_resolver` 3 函数、`gemini/translator.py` 4 常量、`WEB_UI_TITLE`、`WEB_UI_DEFAULT_HOST` — 架构 + Backend Agent
- `_normalize_optional_text` 在 18 个文件重复定义 — 架构 Agent

**根因：** 历次迁移（单机→Web、Phase 2 R2、post-edit Phase 1）每次都在文档加新约束，但没在代码里同步删旧路径。守卫测试也只断言"必有项 ⊆ 文件"，不断言"白名单完整匹配"，所以删除条目不会 red。

**统一治理方案：**
1. 一次性物理清理：
   - `rmdir projects/`
   - `rm tmp_source_video.mkv`
   - `rm -rf demo_output/`
   - `data/minimax_seed*.sql`、`data/traits_analysis.json` 移到 `docs/archive/data/`
   - 删 `_resolve_or_auto_clone_voice`、`project_resolver` 3 死函数、`gemini/translator.py` 4 死常量、`WEB_UI_TITLE` / `WEB_UI_DEFAULT_HOST`、`PROCESS_RUN_TIMEOUT_SECONDS` 重复
2. 把 `_normalize_optional_text` / `_ensure_dict` / `_internal_headers` 抽到 `src/utils/`，加 AST 守卫禁止再写副本
3. 把 `gateway/job_intercept.py:2584` 改成 importlib 绕过模式，与 `display_name_orchestrator.py` 一致；加守卫扫 `gateway/*.py` 不许 `from services.jobs.* import …`，allowlist 已经走 importlib 的几个例外
4. 把所有 phase guard 的"必有 ⊆ 内容"改成"集合完全相等"

### 系统性问题 #4：Gateway 端 admin / internal 路由的认证一致性

**Agent 见证：**
- S-CRITICAL-1：`/source-metadata` + `/metering` 完全无认证
- S-CRITICAL-2：`internal_expire_voice` 跳过 internal access
- S-HIGH-1：`_verify_job_ownership` 对 DB 缺失 job 是 fail-open
- S-HIGH-3：`/auth/login` 无 rate limit
- 多种 admin 操作无 audit log（`update_admin_settings` / `update_review_prompts` / `toggle_model` / `cancel_job` / `delete_job` / `publish_pricing`）
- Frontend Agent：admin/users + voices + prompts 缺 forbidden state

**根因：** Gateway 路由的认证模式三种并存（`Depends(require_auth)` for user-facing、`Depends(_require_internal_access)` for pipeline-callback、admin 路由的内联 `_require_admin`）。新增端点时没强约束哪种认证必选，容易漏。

**统一治理方案：**
1. 加一个回归守卫：AST 解析 `gateway/main.py`，每个 `app.post("/job-api/*")` / `app.post("/api/admin/*")` 必须挂至少一个已知 dependency
2. 给所有 admin 写操作加 `AdminAuditLog` 行
3. 给 `/auth/login` 加 per-IP / per-account rate limit（复用 `risk_control._RateLimiterState`）
4. 修 `_verify_job_ownership` fail-closed
5. 前端 admin/users + voices + prompts 加 forbidden state，与其他 admin 页面对齐

### 系统性问题 #5：性能层"全 sync IO 在 async 路径里"

**Agent 见证：** 性能 Agent 8 个 CRITICAL + Backend Agent 多条 HIGH（ffprobe 无 timeout、shutil.copyfileobj 同步等）

**根因：** Gateway 是 FastAPI async（uvicorn），但内部大量 `shutil.copyfileobj` / `read_bytes` / `read_text` / `zipfile.ZipFile` 都是同步 stdlib，散落在 admin endpoint / upload / materials_pack。Job API 整体 sync 也加剧问题。

**统一治理方案：**
1. 立即改：所有 hot-path 同步 IO 用 `await asyncio.to_thread(...)` 包裹（最低工作量）
2. 中期：系统性的 streaming：`download` 改流式、Job API `_write_binary` 替成 chunked iter、materials_pack 用 task-based 异步流程
3. 加 `asyncio.Semaphore(2)` 给 background task executor，杜绝 N 个并发 ffmpeg 拖死容器
4. 前端 polling 接 `since` 增量协议、加 `document.visibilityState` 暂停、抽公共 polling hook 杜绝雪崩

---

## 5. 性能详表（节选）

完整版见性能 Agent 输出，此处只列 Top 10 按 ROI 排序：

| Rank | Finding | 当前耗时 / 频率 | 优化后 | 工作量 |
| ---: | --- | --- | --- | --- |
| 1 | fsync per stdout line + 全 JSON rewrite | 6-30s/任务 + 60-180 MB 写盘 | <1s + ~5 MB | 中 |
| 2 | Job listing 全文件 glob + parse | 1000 jobs → 200-800 ms / list | 内存 index → 5 ms | 中 |
| 3 | Pipeline 完全串行 | 30 min 视频 ~15-25 min | 节省 30-40% | 大 |
| 4 | Alignment 串行 | 200 段 × 2s = 400s | 4 worker → 110s | 小 |
| 5 | 前端轮询全量 logs | 110 KB/s/用户 | 增量 → ~5 KB/s | 小 |
| 6 | `read_bytes()` 全内存读视频 | 1 GB 视频 → 1 GB RSS | 流式 → ~64 KB | 小 |
| 7 | background task 无并发上限 | 5 并发 → CPU 400% | Semaphore(2) | 小 |
| 8 | Admin endpoint 同步 IO 阻塞 loop | 200-1000 ms 卡 | DB-side cache | 中 |
| 9 | R2 lazy upload 阻塞下载 | 1 GB 首下 → 30-90s 等待 | 后台 upload + presign | 中 |
| 10 | upload `shutil.copyfileobj` 同步 | 2 GB 上传阻塞 30-60s | `to_thread` 包裹 | 极小 |

---

## 6. 守卫测试有效性矩阵

### `tests/test_legacy_cleanup_guards.py`

| 测试 | 状态 | 备注 |
| --- | --- | --- |
| `test_no_legacy_frontend_dir` | ✅ 有效 | `frontend/` 不存在 |
| `test_no_tmp_local_video_repro_dir` | ✅ 有效 | |
| **`test_no_root_projects_dir`** | ✅ 守卫**有效**，但**当前红** | `projects/` 空目录回归触发该断言 fail。守卫工作正常，是被守卫的不变量被破坏。Codex 已 `pytest -q` 实跑确认 |
| `test_no_build_dir` | ✅ 有效 | |
| `test_no_web_ui_server_file` | ✅ 有效 | |
| `test_no_web_ui_handler_file` | ✅ 有效 | |
| `test_main_help_does_not_advertise_web_ui_subcommand` | ✅ 有效 | |
| `test_no_imports_of_deleted_web_ui_modules` | ✅ 有效 | |
| `test_gateway_business_modules_no_hardcoded_job_api_url` | ⚠️ **不对称** | 只挡 8877，不挡 worker→gateway 的 8880 硬编码 5+ 处 |
| `test_caddyfile_has_internal_block_rule` | ✅ 有效 | |

### `tests/test_phase1_guards.py`

| 测试 | 状态 | 备注 |
| --- | --- | --- |
| `test_alignment_modules_do_not_call_tts_generator` | ✅ AST 扫描有效 | |
| `test_publish_modules_do_not_call_tts_generator` | ✅ AST 扫描有效 | |
| `test_editing_commit_pipeline_does_not_call_tts_generator` | ✅ 有效 | |
| `test_paid_api_surface_isolated_from_commit_alignment_publish` | ✅ AST 扫描有效 | **未覆盖 `src/pipeline/`，导致 `_resolve_or_auto_clone_voice` 孤儿未被发现** |
| **`test_gateway_knows_every_post_edit_endpoint`** | ⚠️ **可被绕过** | 不覆盖 `regenerate-all-tts/cancel` / `split` / `preview-source`；只是子集断言 |
| 其他（parity / 字符串扫描） | ✅ 有效 | |

### `tests/test_phase2_download_backend.py`

| 测试 | 状态 | 备注 |
| --- | --- | --- |
| 全部 | ✅ 有效 | R2 异常路径全覆盖、JSONL schema 漂移检测、前端 R2 字面量泄漏 0 匹配 |

---

## 7. env 与配置一致性

| Env Var | docker-compose.yml | .env.example | 代码 | 备注 |
| --- | --- | --- | --- | --- |
| `AVT_INTERNAL_API_KEY` | ✅ 必填 | ✅ | ✅ startup 校验 | OK |
| `AVT_ENABLE_POST_EDIT` | ✅ default false | ❌ **缺** | ✅ default False | 应加 |
| `NEXT_PUBLIC_ENABLE_POST_EDIT` | ✅ Dockerfile build-arg | ❌ **缺** | n/a | 应加 |
| `AVT_DOWNLOAD_REDIRECT_BACKEND` | ✅ default local | ❌ **缺** | ✅ default "local" | 应加 |
| `R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ARTIFACTS_BUCKET` | ✅ default 空 | ❌ **缺** | ✅ Field default="" | 应加（带注释说明 fallback 行为） |
| `INSTALL_WHISPER` | ✅ default 0 | ❌ **缺** | n/a (build-time) | 应加 |
| `HF_HOME` | ✅ 写死 | ❌ **缺** | n/a | 应加 |
| `AIVIDEOTRANS_*_DIR` | ✅ 生产值 | ✅ 注释 | ✅ env-driven | OK |
| `AVT_GATEWAY_URL`（worker→gateway） | ❌ 不显式 | ❌ 缺 | 部分代码读，多处硬编码 127.0.0.1:8880 | 应统一抽 helper |

**Dev-mode bind mount 警告**：`docker-compose.yml:42-55` 仍开启 `src/` / `main.py` / `scripts/` 的 bind mount，CLAUDE.md 已明文要求"项目接近完成必须切回镜像不可变模式"。当前 V3 已接近上线，应安排切回。

---

## 8. R2 前端零感知契约：合规

执行命令：

```
grep -rE "r2\.cloudflarestorage|avt-artifacts|X-Amz-|presigned|AWS4-HMAC|cloudflarestorage" frontend-next/src/
```

**结果：0 匹配**。前端代码层 100% 符合 CLAUDE.md "Phase 2 R2 下载切面前端零感知"硬约束，与 `tests/test_phase2_download_backend.py::test_frontend_has_no_r2_leakage` 守卫测试一致。

---

## 9. 修复优先级建议（经 Codex 复核重排）

P0/P1/P2/P3 划分按 **风险性质** 而非"几条 CRITICAL"：直接威胁账单/越权/数据丢失/CI 红的进 P0；账户安全与计费一致性 + 高 ROI 性能进 P1；UX、吞吐与架构进 P2；纯清理进 P3。**性能瓶颈中只有 IO 类（fsync、`read_bytes`、并发上限）属 P1；pipeline 串行属吞吐优化，归 P2**。

---

### P0 — 立即修（本周内）

直接影响账单、越权、数据丢失或回归守卫红的项目。

| # | 任务 | 关键文件 |
| ---: | --- | --- |
| 1 | `/job-api/jobs/{id}/source-metadata` 与 `/metering` 加 `Depends(_require_internal_access)` | [gateway/main.py:307-308](gateway/main.py:307) |
| 2 | 内部端点暴露三件套：(a) `internal_expire_voice` 加 internal access 校验，(b) router prefix 改为 `/api/internal` 让 Caddy block 生效，(c) Job API 启动校验 `AVT_INTERNAL_API_KEY` | [gateway/user_voice_api.py:495](gateway/user_voice_api.py:495)、[Caddyfile:92](Caddyfile:92)、[src/services/jobs/api.py:1033](src/services/jobs/api.py:1033) |
| 3 | `_verify_job_ownership` 改 fail-closed（DB 缺 row → 404，不是默认放行） | [gateway/job_intercept.py:2496](gateway/job_intercept.py:2496) |
| 4 | `quota.reserve_quota` / `release_quota` 加 `with_for_update()`；`ensure_admin_credits_bucket` 同补 | [gateway/quota.py:55-76](gateway/quota.py:55)、[gateway/credits_service.py:855-892](gateway/credits_service.py:855) |
| 5 | JSON / file state 层全局 `file_lock`：JobStore、StateManager、editing 三件套（segment/status/voice_map）、`admin_settings` | [src/services/jobs/store.py](src/services/jobs/store.py)、[src/services/state_manager.py](src/services/state_manager.py)、[src/services/jobs/editing_segments.py](src/services/jobs/editing_segments.py)、[src/services/jobs/editing_voice_map.py](src/services/jobs/editing_voice_map.py)、[gateway/admin_settings.py](gateway/admin_settings.py) |
| 6 | 修 `cleanup.py:193` import 路径 + 加 startup smoke test 强制走一次循环 | [src/services/web_ui/cleanup.py:193](src/services/web_ui/cleanup.py:193) |
| 7 | Alembic `env.py` import voice_catalog / label_task / background_task models；`Job.__table_args__` 补 `idx_jobs_editing_touched_at` | [gateway/alembic/env.py:15](gateway/alembic/env.py:15)、[gateway/models.py:115-124](gateway/models.py:115) |
| 8 | split editing segment 三件套：(a) `_find_text_edits_without_tts` 对 split 段加兜底，(b) 拒绝 zero-duration 半段，(c) 迁移 `voice_map` override | [src/services/jobs/editing_commit.py:466-508](src/services/jobs/editing_commit.py:466)、[src/services/jobs/editing_segments.py:707-849](src/services/jobs/editing_segments.py:707) |
| 9 | 删除根 `projects/` 空目录（让 `tests/test_legacy_cleanup_guards.py::test_no_root_projects_dir` 重新绿） | 仓库根 |

---

### P1 — 本周内（账户安全 + 计费一致性 + 高 ROI 性能）

| # | 任务 | 关键文件 |
| ---: | --- | --- |
| 10 | Auth abuse 组合：`/auth/login` 加 per-IP/per-account rate limit；修 captcha pre-verify 死代码（删或接通 `consume_captcha_pass`）；`verify_code_endpoint` 改"先比对再 mark consumed + attempts 列"；`X-Forwarded-For` 加可信代理边界 | [gateway/auth.py:182](gateway/auth.py:182)、[gateway/auth_phone.py:78,195,281,146](gateway/auth_phone.py:78) |
| 11 | 支付/定价一致性：`payment_webhook_events` 改复合 unique `(provider, provider_event_id)`；`_process_payment_event` 重构为 `INSERT ... ON CONFLICT DO NOTHING RETURNING` 单事务；`pricing_config_versions.version` 加 unique；`pricing_runtime` 跨进程失效（mtime 校验或重启即可） | [gateway/models.py:300-303](gateway/models.py:300)、[gateway/billing.py:706](gateway/billing.py:706)、[gateway/models.py:595-625](gateway/models.py:595)、[gateway/pricing_runtime.py:18-61](gateway/pricing_runtime.py:18) |
| 12 | 性能 IO 三大件：(a) Job listing 内存索引化；(b) pipeline stdout 路径去 fsync + group commit + JobRecord 内存缓存；(c) `download` / `upload` 全部改流式或 `asyncio.to_thread` 包裹 | [src/services/jobs/store.py:63-80](src/services/jobs/store.py:63)、[src/services/jobs/process_runner.py:375-473](src/services/jobs/process_runner.py:375)、[src/services/jobs/api.py:308](src/services/jobs/api.py:308)、[gateway/upload.py:107-108](gateway/upload.py:107) |
| 13 | `asyncio.Semaphore(2)` 限制 background task executor 并发（generate_video / materials_pack） | [gateway/background_task_api.py:83](gateway/background_task_api.py:83)、[src/services/jobs/video_render_async.py:90-108](src/services/jobs/video_render_async.py:90) |
| 14 | `audio_utils.measure_duration_ms` ffprobe 加 `timeout=30` | [src/utils/audio_utils.py:18-43](src/utils/audio_utils.py:18) |
| 15 | S2 Pass 1/2/3 fallback 链加 `max_fallback_attempts=2`，非 transient 错误立即停止级联 | [src/services/transcript_reviewer.py:1362,1655,2071](src/services/transcript_reviewer.py:1362) |

---

### P2 — 排进迭代（UX、吞吐、架构治理）

| # | 任务 | 关键文件 |
| ---: | --- | --- |
| 16 | 前端 polling 治理：`usePollingTask` 加 `document.visibilityState` 暂停；后端 `/jobs/{id}/logs` 加 `since` 增量协议；抽 `useJobsList` 共享 hook 消除多页面雪崩 | [usePollingTask.ts](frontend-next/src/lib/react/usePollingTask.ts)、[src/services/jobs/api.py:152-162](src/services/jobs/api.py:152) |
| 17 | Pipeline / alignment 串行 → 并行（吞吐优化，非 P1 安全）：alignment 加 ThreadPoolExecutor；Pipeline audio_separation + 转录并行；S2 Pass 1/2 并行；翻译 chunk 并行 | [src/services/alignment/aligner.py:168-212](src/services/alignment/aligner.py:168)、[src/pipeline/process.py](src/pipeline/process.py)、[src/services/transcript_reviewer.py](src/services/transcript_reviewer.py)、[src/modules/translation/translator.py:43-52](src/modules/translation/translator.py:43) |
| 18 | 前端：admin/users + voices + prompts 加 forbidden state；`WorkspacePage` `loadJob` 改 `useCallback` 修 stale closure 隐患；`edit/page.tsx` 1907 行拆分；`useBackgroundTask` 加 AbortController；`F-HIGH-3` 修 `TranslationForm` upload 的 credentials | 多文件 |
| 19 | 架构治理：worker→gateway URL 统一抽 `src/services/_gateway_endpoint.py` + 加守卫；4 处 `_internal_headers()` 抽 `src/services/_internal_headers.py`；`gateway/job_intercept.py:2584` 改 importlib 绕过模式 + 加守卫扫 gateway 不许 import `services.jobs.*` | 多文件 |
| 20 | 报告说明：F-HIGH-3 中 `admin/jobs` cancel/delete 的 credentials 项**已存在**，从待办列表中移除；只保留 upload-video 一处（已在 P2-#18 任务里） | — |
| 21 | 守卫升级：`tests/test_phase1_guards.py::test_paid_api_surface_isolated...` 扩到 `src/pipeline/` 覆盖孤儿；`test_gateway_knows_every_post_edit_endpoint` 改集合相等断言；`test_gateway_business_modules_no_hardcoded_job_api_url` 加对称 worker→gateway 守卫；所有 admin 写操作加 `AdminAuditLog` 行 | [tests/test_phase1_guards.py](tests/test_phase1_guards.py)、[tests/test_legacy_cleanup_guards.py](tests/test_legacy_cleanup_guards.py) |
| 22 | DB pool size 5+10 → 20+20；Alembic 007 downgrade 修 phone-only 数据兼容 | [gateway/database.py:36](gateway/database.py:36)、[gateway/alembic/versions/007_*.py](gateway/alembic/versions/007_add_phone_and_trial_fields.py:95) |
| 23 | `probe_user_voice` 加 per-user rate limit（10/min, 100/day）+ 计入 credits | [gateway/user_voice_api.py:153-208](gateway/user_voice_api.py:153) |
| 24 | `Session.expires_at` 加 index（`postgresql_concurrently=True`） | [gateway/models.py:628-643](gateway/models.py:628) |

---

### P3 — 清理类（不挤占 P0/P1 修复窗口）

| # | 任务 | 影响 |
| ---: | --- | --- |
| 25 | 删 `tmp_source_video.mkv`、`demo_output/`、`data/minimax_seed*.sql`（移到 `docs/archive/data/`） | 仓库整洁 |
| 26 | 删死代码：`_resolve_or_auto_clone_voice`（[process.py:5005-5104](src/pipeline/process.py:5005)）、`project_resolver` 3 个公开未使用函数、`gemini/translator.py:46-50` 4 个 deprecated 常量、`WEB_UI_TITLE` / `WEB_UI_DEFAULT_HOST` | 维护噪音 |
| 27 | 抽 `src/utils/normalize_optional_text.py`，删 18 处副本；`_ensure_dict` 3 处副本同理 | DRY |
| 28 | CLAUDE.md 行号引用全部改函数名引用；下线 `review_model` / `translation_model` / `voice_clone_cost_credits` deprecated 字段（2026-06 窗口） | 文档准确性 |
| 29 | 补 `.env.example` 缺失的 R2 / post-edit / Whisper / `AVT_GATEWAY_URL` 等 env | 部署文档 |
| 30 | dev-mode bind mount 切回镜像不可变模式（V3 接近上线） | 生产稳定性 |

---

### P4 — 长期演进

| # | 任务 |
| ---: | --- |
| 31 | Job state 整体迁到 PostgreSQL，废 JSON file store |
| 32 | Job API 由 stdlib `ThreadingHTTPServer` 迁到 FastAPI/uvicorn |
| 33 | 引入真正的 task queue（rq / arq）替代裸 `asyncio.create_task` |
| 34 | 多 worker 部署下 `pricing_runtime` 用 Redis pub/sub 而非 mtime 失效 |

---

## 10. 附录：admin 页面后端 gate 检查清单（前端 + 后端联合）

前端 admin 入口由 sidebar `app-shell.tsx:200-205` 的 `user?.role === 'admin'` 控制，**这只是 UX 隐藏菜单**。真正的安全边界必须由 Gateway 后端强制。本审计无法直接验证后端每条 `/api/admin/*` 是否真的挂了 `_require_admin`，需要 follow-up 确认。

| 前端页面 | 关键 fetch endpoint | 客户端 forbidden 处理 | 后端 gate（待人工核） |
| --- | --- | --- | --- |
| `admin/jobs` | `/api/admin/jobs/...` | ✅ | ⏳ |
| `admin/users` | `/api/admin/users/...` | ❌ **缺** | ⏳ |
| `admin/voices` | `/api/admin/voices/...` | ❌ **缺** | ⏳ |
| `admin/prompts` | `/api/admin/...prompts...` | ❌ **缺** | ⏳ |
| `admin/settings` | `/api/admin/settings` | ✅ | ⏳ |
| `admin/s2-monitor` | `/api/admin/s2-stats?...` | ✅ | ⏳ |
| `admin/traffic` | `/api/admin/traffic/summary?...` | ✅ | ⏳ |
| `admin/conversions` | 同上 | ✅ | ⏳ |
| `admin/discovery` | `/api/admin/traffic/discovery?...` | ✅ | ⏳ |
| `admin/security` | `/api/admin/traffic/security?...` | ✅ | ⏳ |
| `admin/costs` | `/api/admin/costs/jobs?...` | ✅ | ⏳ |
| `admin/credits-monitor` | `/api/admin/credits...` | ✅ | ⏳ |
| `admin/pricing` | `/api/admin/pricing*` | ✅ | ⏳ |

---

## 11. 附录：守卫升级提议（具体落地点）

**A) `tests/test_legacy_cleanup_guards.py`**

```python
# 加：worker → gateway 8880 不准硬编码
def test_worker_modules_no_hardcoded_gateway_url():
    """src/services/tts/ 与 src/pipeline/ 不许出现 127.0.0.1:8880 / localhost:8880 字面量。"""
    banned = ["127.0.0.1:8880", "localhost:8880"]
    allow = {Path("src/services/_gateway_endpoint.py")}  # 唯一 helper
    for p in glob.glob("src/**/*.py", recursive=True):
        path = Path(p)
        if path in allow:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{p} hardcodes gateway URL: {needle}"
```

**B) `tests/test_phase1_guards.py`**

```python
# 改：endpoint 白名单完整性匹配
EXPECTED_POST_EDIT_ENDPOINTS = frozenset({
    "enter-edit", "editing/cancel", "editing/commit",
    "regenerate-all-tts", "regenerate-all-tts/cancel",  # ← 新加
    "editing/voice-map", "editing/revert-unsynced-text",
    "segments/{sid}/update", "segments/{sid}/status",
    "segments/{sid}/regenerate-tts", "segments/{sid}/accept-draft",
    "segments/{sid}/discard-draft",
    "segments/{sid}/split", "segments/{sid}/preview-source",  # ← 新加
})

def test_gateway_knows_every_post_edit_endpoint():
    actual = _parse_whitelist_from_gateway()
    assert actual == EXPECTED_POST_EDIT_ENDPOINTS  # 集合相等
```

**C) 新加守卫**：禁止 `gateway/*.py` 直接 `from services.jobs.*` 

```python
# tests/test_gateway_import_isolation.py
ALLOWED_LOADERS = {  # 已经走 importlib.spec_from_file_location 的例外
    "gateway/log_redactor_loader.py",
    "gateway/display_name_orchestrator.py",
}

def test_gateway_does_not_import_services_jobs_packages():
    for p in glob.glob("gateway/*.py"):
        if p in ALLOWED_LOADERS:
            continue
        tree = ast.parse(Path(p).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("services.jobs."):
                    pytest.fail(f"{p} directly imports {node.module}")
```

---

## 12. 总结

本次审计在六个维度均发现 CRITICAL 问题，但项目主体架构与安全契约依然合规。最值得欣慰的是：

- R2 前端零感知契约 100% 合规（grep 0 匹配）
- Alipay 验签、Bcrypt、HttpOnly cookie、Caddy `/api/internal/*` block 等基础安全控制到位
- Phase 1 / Phase 2 守卫主体设计正确（虽有局部漏覆盖）
- 付费 API 的"用户显式触发"原则**主路径**遵守（commit / alignment / publish 都有 AST 守卫）

最值得警惕的是：

- **Idle editing scanner 在生产环境从未跑过**（B-CRITICAL-1）— 是这次审计中最隐秘也最严重的发现之一
- **JSON 文件状态层多线程不安全是普遍问题** — `services/_file_lock.py` 已建好却被三个核心写者全部漏接
- **Alembic env.py 漏 4 张表 model** — 任何 autogenerate 都会引发数据灾难
- **`/source-metadata` + `/metering` 完全无认证** — 直接威胁账单系统正确性
- **Migration 留尾未彻底清理** — 文档与代码漂移、死代码、违反 importlib 绕过约定散布

按 P0 立即修可在 1 周内消除最大风险面；P1 / P2 在 1 个月内可大幅提升性能与守卫覆盖度。

---

*审计员：6 个 Claude 并行 Agent + 主席 Agent 汇总*
*所有发现来自代码与配置文件实测，每条 finding 配 file:line。*
