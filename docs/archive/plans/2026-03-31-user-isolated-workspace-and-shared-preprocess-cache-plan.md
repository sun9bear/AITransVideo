# 用户隔离工作区与共享预处理缓存实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先完成 `user_id + job_id` 隔离工作区、并发打通、本地上传链路打通与旧路径兼容，再为后续共享预处理缓存预留清晰扩展点。
**Architecture:** 一期只做用户隔离 workspace、用户隔离 uploads staging、source-aware runner / CLI / pipeline 与旧目录兼容；二期再补全只读 preprocess cache。
**Tech Stack:** Python 3.11, FastAPI, Next.js 16, 本地文件系统路径工具, pytest。
**Spec:** `docs/specs/2026-03-31-user-isolated-workspace-and-shared-preprocess-cache-design.md`

---

## 当前状态（2026-03-31）

**一期（Task 1-7）：✅ 已完成。** 经 13 轮迭代开发与审核，所有一期任务已落地并通过验收矩阵。

已交付能力：
- `user_id + job_id` 隔离工作区（`projects/<user_id>/<job_id>/`）
- 上传目录按用户隔离（`uploads/<user_id>/<upload_id>_<safe_name>`）
- gateway → Web UI 可信 `X-User-Id` 头注入（`/api/*` 与 `/web-ui-api/*` 双路径）
- Job API 全局单活跃闸门移除，并发控制完全由 gateway 套餐规则管理
- CLI 与 `ProcessConfig` 显式 `--source-type/--source-ref` 支持
- `process_runner._build_command()` 显式 source 参数构造 + `workspace_dir` 接入
- `ProcessPipeline` ingest 按 `source_type` 分流（YouTube / local_video / local_audio）
- 旧 URL 目录复用逻辑移除，新任务不再共享旧 workspace
- `_finalize_process` 优先 `project_dir/workspace_dir`，legacy source_ref 搜索仅作兜底
- 前端 `source_type` 对齐到 `local_video`，并发 guard 改用 entitlements
- 死代码清理（`_find_existing_project_by_url`、`getCurrentJob`、stale `local_audio` 测试）
- `transcription_method` 持久化 round-trip 补齐

**二期（Task 8-9）：⏸️ Deferred。** 等有真实多用户重复来源压力后再补充共享只读预处理缓存。

---

## 交付策略

### 一期（✅ 已完成）

- Task 1-7：工作区隔离、并发策略修正、`source_type` 端到端打通、旧复用逻辑清理、前端对齐与回归。

### 二期（⏸️ 延后）

- Task 8-9：共享预处理缓存、指纹与锁、cache hydrate / publish。

这样拆分的原因是：一期已经能解决”用户数据串线”和”上传任务跑不通”这两个最高优先级问题，而共享 cache 的复杂度高于当前收益。

---

## Phase 1: 任务模型与路径规则 ✅

### Task 1: 为 job snapshot 增加 `user_id` 与 `workspace_dir`

**Files:**
- Update: `gateway/job_intercept.py`
- Update: `src/services/jobs/models.py`
- Update: `src/services/jobs/api.py`
- Update: `src/services/jobs/store.py`
- Update: `tests/test_gateway_create_job.py`
- Update: `tests/test_job_model_snapshot.py`
- Update: `tests/test_job_api.py`

- [x] **Step 1: 设计新增字段**
明确并补齐 `user_id`、`workspace_dir`、`source_content_hash` 在 gateway → Job API → store 的传递链路；保留现有 `project_dir` 作为兼容字段。

- [x] **Step 2: 写失败测试**
为 gateway 创建任务、Job API 持久化、JobRecord round-trip 增加新字段断言。

- [x] **Step 3: 运行失败测试**
运行 `python -m pytest tests/test_gateway_create_job.py tests/test_job_model_snapshot.py tests/test_job_api.py -q`  
预期：新增字段断言失败或字段缺失。

- [x] **Step 4: 实现最小改动**
在 job snapshot / JobRecord / store 中补齐 `user_id` 与 `workspace_dir`，确保旧 payload 缺字段时仍能读取。

- [x] **Step 5: 再跑测试**
运行 `python -m pytest tests/test_gateway_create_job.py tests/test_job_model_snapshot.py tests/test_job_api.py -q`  
预期：通过。

### Task 2: 引入统一路径助手并固化目录规范

**Files:**
- Create: `src/services/job_paths.py`
- Update: `src/services/web_ui/handler.py`
- Update: `src/services/jobs/process_runner.py`
- Update: `src/services/web_ui/project_resolver.py`
- Create: `tests/test_job_paths.py`
- Update: `tests/test_web_ui.py`
- Update: `tests/test_process_runner.py`

- [x] **Step 1: 设计路径助手接口**
新增 `build_workspace_dir(user_id, job_id)`、`build_upload_path(user_id, upload_id, filename)`、`is_legacy_project_dir(...)` 等纯函数。

- [x] **Step 2: 写失败测试**
覆盖 `projects/<user_id>/<job_id>/` 与 `uploads/<user_id>/<upload_id>_<safe_name>` 两类路径规则。

- [x] **Step 3: 运行失败测试**
运行 `python -m pytest tests/test_job_paths.py tests/test_web_ui.py tests/test_process_runner.py -q`  
预期：新路径助手尚不存在或旧逻辑断言失败。

- [x] **Step 4: 实现最小改动**
将新任务工作区统一改为 `projects/<user_id>/<job_id>/`。  
将上传目录统一改为 `uploads/<user_id>/<upload_id>_<safe_name>`。  
如果当前上传接口拿不到认证态 `user_id`，先补一个可信用户上下文注入点，再启用该目录规则。

- [x] **Step 5: 再跑测试**
运行 `python -m pytest tests/test_job_paths.py tests/test_web_ui.py tests/test_process_runner.py -q`  
预期：通过。

---

## Phase 2: 并发控制与 source-aware 执行链路 ✅

### Task 3: 移除 Job API 的全局单活跃任务闸门

**Files:**
- Update: `src/services/jobs/service.py`
- Update: `tests/test_job_service.py`
- Update: `tests/test_gateway_job_policy.py`

- [x] **Step 1: 写失败测试**
增加“不同用户 / 同用户在套餐上限内可并发创建任务”的测试，确保冲突控制只发生在 gateway 套餐规则层。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_job_service.py tests/test_gateway_job_policy.py -q`  
预期：仍被 `_find_active_job()` 全局阻断。

- [x] **Step 3: 实现最小改动**
删除或收窄 `src/services/jobs/service.py` 中 `_find_active_job()` 的全局拒绝逻辑，不再让上游 Job API 否决 gateway 已经放行的并发任务。

- [x] **Step 4: 再跑测试**
运行 `python -m pytest tests/test_job_service.py tests/test_gateway_job_policy.py -q`  
预期：通过。

### Task 4a: 给 CLI 与 `ProcessConfig` 增加显式来源参数

**Files:**
- Update: `main.py`
- Update: `src/pipeline/process.py`
- Update: `tests/test_process_pipeline.py`

- [x] **Step 1: 写失败测试**
为 `main.py process` 增加 `--source-type`、`--source-ref` 解析测试，并保留旧 `youtube_url` 兼容入口。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_process_pipeline.py -q`  
预期：`ProcessConfig` 仍只有 `youtube_url`。

- [x] **Step 3: 实现最小改动**
让 CLI 接受显式 `source_type/source_ref`，并让 `ProcessConfig` 同时能兼容旧字段与新字段。

- [x] **Step 4: 再跑测试**
运行 `python -m pytest tests/test_process_pipeline.py -q`  
预期：通过。

### Task 4b: 让 runner 按 `source_type` 构造命令

**Files:**
- Update: `src/services/jobs/process_runner.py`
- Update: `tests/test_process_runner.py`

- [x] **Step 1: 写失败测试**
覆盖 `youtube_url`、`local_video`、`local_audio` 三类任务的 `_build_command()` 输出，确认本地路径不再落到 `youtube_url` 位置参数。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_process_runner.py -q`  
预期：本地来源仍被构造成 `main.py process <source_ref>`。

- [x] **Step 3: 实现最小改动**
让 `_build_command()` 显式传递 `--source-type`、`--source-ref`，继续保留 `--job-id`。

- [x] **Step 4: 再跑测试**
运行 `python -m pytest tests/test_process_runner.py -q`  
预期：通过。

### Task 4c: 让 pipeline 的 ingest 阶段按 `source_type` 分流

**Files:**
- Update: `src/pipeline/process.py`
- Update: `src/modules/media_understanding/providers.py`
- Update: `tests/test_process_pipeline.py`

- [x] **Step 1: 写失败测试**
覆盖 `youtube_url` 走下载、`local_video` 走本地媒体 ingest、`local_audio` 走本地音频 ingest 三类行为。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_process_pipeline.py -q`  
预期：pipeline 仍在 `run()` 开头强依赖 `config.youtube_url`。

- [x] **Step 3: 实现最小改动**
将 ingest 阶段拆成三条分支：  
`youtube_url` 保持下载流程；  
`local_video` 直接使用本地视频路径；  
`local_audio` 直接使用本地音频路径。

- [x] **Step 4: 验证 TTS 快照链路没有回退**
确认 `--job-id` 仍会驱动 pipeline 加载 job snapshot，并继续把 per-job `tts_provider` 传给 `TTSGenerator`；这里是回归验证，不是假定链路当前已断。

- [x] **Step 5: 再跑测试**
运行 `python -m pytest tests/test_process_pipeline.py tests/test_process_runner.py -q`  
预期：通过。

---

## Phase 3: 清理旧复用逻辑并明确迁移兼容 ✅

### Task 5: 删除按 URL 直接复用项目目录的逻辑

**Files:**
- Update: `src/pipeline/process.py`
- Update: `src/services/web_ui/project_resolver.py`
- Update: `tests/test_process_pipeline.py`
- Update: `tests/test_web_ui.py`

- [x] **Step 1: 写失败测试**
增加“不同用户相同 URL 任务会创建不同 workspace”的测试，并覆盖旧 `_find_existing_project_by_url()` 的误复用场景。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_process_pipeline.py tests/test_web_ui.py -q`  
预期：仍可能命中旧目录复用。

- [x] **Step 3: 实现最小改动**
移除 `_find_existing_project_by_url()` 作为新任务 workspace 选择依据。  
保留旧目录只读查找，仅用于历史任务展示与兼容读取。

- [x] **Step 4: 再跑测试**
运行 `python -m pytest tests/test_process_pipeline.py tests/test_web_ui.py -q`  
预期：通过。

### Task 6: 修复 `process_runner` 的项目目录回填与旧任务 fallback

**Files:**
- Update: `src/services/jobs/process_runner.py`
- Update: `tests/test_process_runner.py`

- [x] **Step 1: 写失败测试**
覆盖以下场景：  
新任务优先使用 `workspace_dir`；  
旧任务 `workspace_dir` 缺失时回退到 `project_dir`；  
`project_dir` 仍为空时再走旧目录解析；  
日志解析同时支持 Windows 与 POSIX 路径。

- [x] **Step 2: 运行失败测试**
运行 `python -m pytest tests/test_process_runner.py -q`  
预期：POSIX 路径与 `data/projects` 断言失败。

- [x] **Step 3: 实现最小改动**
收尾阶段优先使用 snapshot 中的 `workspace_dir` / `project_dir`。  
旧 job 仅在缺少这些字段时回退到原有 `projects/<slug>` / `data/projects/...` 查找。  
不做历史数据迁移，只做兼容读取。

- [x] **Step 4: 再跑测试**
运行 `python -m pytest tests/test_process_runner.py -q`  
预期：通过。

---

## Phase 4: 前端对齐与整体验收 ✅

### Task 7: 对齐 frontend upload / submit 流程并完成回归

**Files:**
- Update: `frontend-next/src/app/translations/new/page.tsx`
- Update: `frontend-next/src/lib/api/jobs.ts`
- Update: `tests/test_gateway_create_job.py`
- Update: `tests/test_web_ui.py`
- Update: `docs/specs/2026-03-31-user-isolated-workspace-and-shared-preprocess-cache-design.md`
- Update: `docs/plans/2026-03-31-user-isolated-workspace-and-shared-preprocess-cache-plan.md`

- [x] **Step 1: 对齐前端 payload**
提交任务时显式传递 `source_type`。  
本地上传场景改用服务端返回的稳定 `source_ref`，并为后续 `source_content_hash` 预留字段。

- [x] **Step 2: 清理单活跃任务时代的前端阻断**
不再以 `getCurrentJob()` 的单任务 guard 阻止新建；改为以 entitlements 与真实 active jobs 列表为准。

- [x] **Step 3: 写回归测试**
覆盖本地上传提交流程、并发场景下的前端行为、gateway payload 契约。

- [x] **Step 4: 运行回归测试**
运行：

```bash
python -m pytest tests/test_gateway_create_job.py tests/test_gateway_job_policy.py tests/test_job_api.py tests/test_job_model_snapshot.py tests/test_job_service.py tests/test_process_pipeline.py tests/test_process_runner.py tests/test_web_ui.py -q
```

预期：通过。

- [x] **Step 5: 运行 CLI smoke test**
运行：

```bash
python main.py --help
python main.py process --help
```

预期：命令可正常展示帮助，未因 source-type 重构失效。

---

## Phase 5: 二期延后任务 ⏸️

### Task 8: 新建只读共享 cache 基础模块

**Files:**
- Create: `src/services/shared_preprocess_cache.py`
- Create: `tests/test_shared_preprocess_cache.py`

- [ ] 定义 source cache 与 derived cache 的目录结构、键规则和指纹结构。
- [ ] 提供 `lookup`, `publish`, `hydrate`, `acquire_lock` 等轻量接口。
- [ ] 使用临时目录 + 原子 rename 发布缓存，避免读到半成品。
- [ ] 为并发写保护与命中/未命中场景补齐单元测试。
- [ ] 运行 `python -m pytest tests/test_shared_preprocess_cache.py -q`。

### Task 9: 在 pipeline 中接入 source cache 与 pre-review derived cache

**Files:**
- Update: `src/pipeline/process.py`
- Update: `src/modules/media_understanding/providers.py`
- Update: `tests/test_process_pipeline.py`

- [ ] `youtube_url` 任务先查 `cache/source/youtube/<source_key>`，本地上传任务先查 `cache/source/local/<sha256>`。
- [ ] 音频提取/分离结果写入 `cache/derived/audio/...`。
- [ ] 原始 transcript 与审核前结构化 transcript 写入 `cache/derived/transcript/...`。
- [ ] review 后阶段禁止回写共享 cache，改为只写当前 workspace。
- [ ] 增加 cache 命中可跳过重复预处理、review 后产物不复用的测试。
- [ ] 运行 `python -m pytest tests/test_process_pipeline.py tests/test_shared_preprocess_cache.py -q`。

---

## 执行顺序建议

1. 先做 Task 1-2，固定 `user_id`、`workspace_dir` 与路径规则。
2. 再做 Task 3，去掉阻断付费并发的上游闸门。
3. 按 4a → 4b → 4c 顺序打通 `source_type` 全链路。
4. 然后做 Task 5-6，清理旧 URL 复用并完成新旧目录兼容。
5. 最后做 Task 7，完成前端对齐与全量回归。
6. Task 8-9 延后到真实多用户重复来源压力出现后再做。

## 完成标准

一期完成应满足：

- 不同用户相同来源不会共享 workspace。
- 本地上传视频可以作为独立 source type 成功跑完整条任务链。
- Plus / Pro 套餐并发能力与 gateway 宣称一致。
- 新任务固定写入 `projects/<user_id>/<job_id>/`。
- 旧任务在不迁移数据的前提下仍能被读取。
- `tests/test_process_runner.py` 中 Linux 路径与 `data/projects` 相关断言全部通过。

二期完成后再新增：

- 审核前下载/分离/转录可复用。
- 审核后产物不会复用。
