# 异步导出任务 v1：素材打包 + 视频生成

> **Status:** active (v1 已通过 commit `27c2b06` 落地；后续扩展项待验收后决定)  
> **Last updated:** 2026-04-17  
> **Revision:** 2026-04-17 第二稿，整合三方评审（Claude Code / CodeX / Trae）意见，收窄 scope 到 materials_pack + generate_video 两个具体场景，不再作为"通用后台任务框架"规划。

## Context

当前两条用户触发的重活是**同步阻塞**的：
- `POST /api/jobs/{id}/materials-pack`（[gateway/materials_api.py](../../gateway/materials_api.py)）：实时打包 zip 并流式返回。本次改造的直接触发点是用户反馈"点了素材包下载没反应"——iframe 下载被浏览器安全策略拦截，临时用 `window.open` 新标签页绕过，但根治需要让打包状态服务端持久化。
- `POST /jobs/{id}/generate-video`（[src/services/jobs/api.py:480](../../src/services/jobs/api.py)）：调用 `VideoRenderer().render()` 做 FFmpeg mux。短视频 3-5 秒，但**本项目支持最多 3 小时视频**，叠加 ambient audio 混合时耗时会到分钟级，必然超 HTTP 同步请求超时。

目标：两个端点都变成"立即返回 task_id → 后台执行 → 前端轮询 → 产物持久化"模式，支持**刷新/关闭浏览器不丢状态**。

## 非目标（硬性边界）

- **不**收编 `gateway/label_task_queue.py`。label_tasks 是 admin 音色标注专用设施，字段语义和生命周期都不同，本轮允许仓库并存两套 DB queue。未来若要统一，另起方案。
- **不**把付费 API 纳入这套队列。具体指 `voice_clone`（MiniMax 克隆）、`tts_preview`（MiniMax / VolcEngine TTS），这两类每次调用都扣费。通用框架带来的"自动重试 / stale recovery / 去重"机制对付费 API 会变成**静默扣费的新路径**——参考 CLAUDE.md 和 2026-04-05 的 MiniMax 余额事故。
- **不**保证 Gateway 重启后任务继续。重启即标 `failed`，由用户决定重试。stale recovery 只负责把孤儿任务从 `running/pending` 清到 `failed`，不恢复执行。
- **不**做 Job API 的 FastAPI 化重构。`generate_video` 侧的改造用 stdlib `threading` 即可，保持现有 `http.server` 架构。

## 参考模式（不复用实现）

`gateway/label_task_queue.py` 的 **模式** 值得参考：
- PostgreSQL 持久化状态（pending → running → completed/failed）
- `asyncio.create_task()` 单进程后台执行
- 启动时 stale recovery 把 `running OR pending` 标 `failed`
- 分块进度上报

本方案新建独立的 `background_tasks` 表和 `BackgroundTaskQueue` 模块，**不复用 label_tasks 的表和代码**。

---

## 设计

### 1. 数据模型

**Migration**: 新增 `background_tasks` 表

```sql
CREATE TABLE background_tasks (
    id VARCHAR(20) PRIMARY KEY,       -- 短 hex（和 label_tasks 一致）
    job_id VARCHAR NOT NULL,          -- 关联翻译任务
    user_id UUID NOT NULL,            -- 任务所有者（权限校验）
    task_type VARCHAR(32) NOT NULL,   -- 'materials_pack' | 'generate_video'
    params JSONB NOT NULL,            -- 任务参数（items 列表等）
    params_fingerprint VARCHAR(64) NOT NULL,  -- params 稳定序列化 + sha256
    status VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed
    progress JSONB,                   -- {"stage": "muxing", "percent": 70}
    result JSONB,                     -- 完成后的产物信息
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- latest 查询 + 并发去重用
CREATE INDEX idx_bg_tasks_active
  ON background_tasks (job_id, task_type, params_fingerprint, status)
  WHERE status IN ('pending', 'running');

-- 用户维度列表
CREATE INDEX idx_bg_tasks_user_updated
  ON background_tasks (user_id, updated_at DESC);
```

**`params_fingerprint` 是关键字段**：`materials_pack` 的 `items=[source_video, subtitles]` 和 `items=[dubbed_video]` 是两个语义不同的任务。按 type 单独查 latest 会把"上次的全量包"错误恢复为"这次的字幕包"。fingerprint 规则：`sha256(json.dumps(params, sort_keys=True, ensure_ascii=False))`。

### 2. BackgroundTaskQueue 模块

**文件**: 新建 `gateway/background_task_queue.py`

```python
async def create_task(db, job_id, user_id, task_type, params) -> str:
    """创建任务。若 (job_id, task_type, params_fingerprint) 已有 active 任务，返回已有 task_id。"""

async def get_task(db, task_id, user_id) -> dict | None:
    """查询任务（含权限校验）。"""

async def get_latest_active(db, job_id, user_id, task_type, params_fingerprint) -> dict | None:
    """查最近一个同 fingerprint 的 active 任务（pending/running）。completed/failed 不算 latest。"""

async def mark_running(db, task_id): ...
async def update_progress(db, task_id, progress: dict): ...
async def mark_completed(db, task_id, result: dict): ...
async def mark_failed(db, task_id, error: str): ...
async def recover_stale(db) -> int:
    """启动时：running OR pending → failed, error='Gateway 重启'"""
```

### 3. API 端点

**文件**: 新建 `gateway/background_task_api.py`

路由注册位置：`main.py` 中与 `materials_router` 同级，**必须在任何 Job API 代理 catch-all 之前**，否则会被误转发到 8877。

#### 3.1 创建任务
```
POST /api/jobs/{job_id}/tasks
Body: { "task_type": "materials_pack", "params": { "items": [...] } }
Response: { "task_id": "xxx", "status": "pending" }
```

行为：
1. 鉴权 + job 所有权校验
2. 计算 `params_fingerprint`
3. 查 active 任务：若存在则直接返回其 task_id（天然去重）
4. 插入新行（status=pending）
5. `asyncio.create_task()` 启动执行器
6. 立即返回

#### 3.2 查询任务
```
GET /api/jobs/{job_id}/tasks/{task_id}
Response: {
  "task_id": "xxx",
  "status": "running",
  "progress": { "stage": "muxing", "percent": 70 },
  "result": null,
  "error": null
}
```

#### 3.3 查询最新 active 任务（页面加载恢复状态用）
```
GET /api/jobs/{job_id}/tasks/latest?type=materials_pack&fingerprint={fp}
Response: { "task_id": "...", "status": "running", ... } 或 null
```

前端无法算 fingerprint 时（首次进入页面不知道用户要打什么包），可以省略 fingerprint 参数，返回该 type 下最近一条 active 任务。**但按钮点击时必须带 fingerprint，保证恢复的是同参数任务。**

#### 3.4 下载产物（仅 materials_pack）
```
GET /api/jobs/{job_id}/tasks/{task_id}/download
```
Streaming 返回 zip 文件。

**`generate_video` 不走这个路径**。视频完成后前端刷新 materials-availability，走既有的 `stream/video` + `download/publish.dubbed_video` 链路即可，不发明新 mp4 下载端点。

### 4. 执行器

#### 4.1 `materials_pack`（Gateway 原生执行）

Gateway 容器内用 stdlib `zipfile` 即可，不需要 FFmpeg。

流程：
1. 从 manifest 解析文件路径（复用 `materials_api.py` 的 `_ITEM_TO_ARTIFACT_KEYS` / `_resolve_artifact_path` 逻辑，抽成共享模块）
2. 校验总大小 ≤ 500MB（沿用现有 `_MAX_ZIP_SIZE_BYTES`，超限 task 标 failed，error 写明）
3. **打包前清理该 job 下旧 zip**：`rm {project_dir}/exports/materials_*.zip`
4. 打包到 `{project_dir}/exports/materials_{task_id}.zip`
5. `result = {"zip_path": "exports/materials_xxx.zip", "size_bytes": ..., "filename": "materials_{job_id[:12]}.zip"}`

产物路径安全性：沿用 `materials_api.py` 现有的 `p.relative_to(project_dir)` 校验，防 path traversal。

#### 4.2 `generate_video`（Gateway 协调 + Job API 异步执行）

**架构决策**：`VideoRenderer` 依赖 FFmpeg 和整个 `src/` 包，不能在 Gateway 容器跑（Gateway 没 FFmpeg，也不应跨层 import）。采用 **A2 协议**：Gateway executor 调 Job API，Job API 侧也做异步化。

**Job API 侧改造**（[src/services/jobs/api.py](../../src/services/jobs/api.py)）：

现有 `POST /jobs/{id}/generate-video` 端点改造为：
```
POST /jobs/{id}/generate-video
  → 生成 render_task_id (短 hex)
  → threading.Thread 后台跑 VideoRenderer，线程内定期写 render_status.json
  → 立即返回 { "render_task_id": "xxx" }

GET /jobs/{id}/generate-video/{render_task_id}
  → 读 {project_dir}/publish/render_status.json 返回
  → { "stage": "muxing" | "mixing_audio" | "encoding" | "done",
      "percent": 0-100,
      "result": { "dubbed_video_path": "..." } | null,
      "error": null | "..." }
```

状态文件 `{project_dir}/publish/render_status.json` 由渲染线程写入，end-of-render 时更新 manifest 的 `artifact_index["publish.dubbed_video"]`（现有逻辑保留）。

为什么不用 DB：Job API 是 stdlib `http.server`（同步），接 async SQLAlchemy 成本高；用文件状态最轻，且天然随 project_dir 生命周期清理。

**进度粒度**：至少三阶段上报，让长视频不至于纯转圈：
- `starting`（FFmpeg 启动、参数校验）
- `muxing`（主渲染阶段，可用 FFmpeg `-progress` 管道解析百分比）
- `finalizing`（写 manifest、清理临时文件）

**Gateway executor**：
```python
async def execute_generate_video(db, task_id, job_id, params):
    # 1. POST Job API 启动渲染
    async with httpx.AsyncClient(timeout=30.0) as client:  # short timeout
        r = await client.post(f"{JOB_API}/jobs/{job_id}/generate-video")
        render_task_id = r.json()["render_task_id"]

    # 2. 轮询 Job API（3-5s 间隔），把进度同步到 background_tasks.progress
    while True:
        await asyncio.sleep(4)
        r = await client.get(f"{JOB_API}/jobs/{job_id}/generate-video/{render_task_id}")
        status = r.json()
        await update_progress(db, task_id, status)
        if status["stage"] == "done":
            await mark_completed(db, task_id, status["result"])
            return
        if status.get("error"):
            await mark_failed(db, task_id, status["error"])
            return
```

httpx 用 short timeout（30s），每次请求都是 short-lived，任何中间网络抖动都不会导致"幽灵任务"——Gateway 重启后 task 标 failed，但 Job API 侧的渲染线程仍会独立跑完，产物落盘，用户重试会命中"already_exists"快速返回。

### 5. 前端

#### 5.1 `useBackgroundTask` hook

**文件**: 新建 `frontend-next/src/lib/react/useBackgroundTask.ts`

```typescript
interface Options {
  jobId: string
  taskType: "materials_pack" | "generate_video"
  paramsFingerprint?: string   // 按钮未点击前可为空
  pollIntervalMs?: number       // 默认 3000，video 可设 4000
}

function useBackgroundTask(opts: Options) {
  // mount 时查 latest active（如有 fingerprint），恢复状态
  // pending/running → 启动轮询
  // 轮询错误退避：502/503/network 失败，退避重试 3 次（1s → 2s → 4s），仍失败才判 failed
  // running 超过 30min：UI 提示"任务可能卡死"但不自动标 failed
  // startTask(params) → POST /api/jobs/{id}/tasks，拿 task_id 开始轮询
  return { status, progress, result, error, startTask, isPolling }
}
```

#### 5.2 `ResultMediaCard` 改造

**素材包按钮**（fingerprint = hash 选中的 items）：
- 无 active 任务 → "素材包"
- pending/running → "素材打包中 X%"（有 progress 时显示，否则 Loader2 动画）
- completed → "素材包可下载"（高亮绿色）→ 点击跳 `/api/jobs/{id}/tasks/{task_id}/download`
- failed → "打包失败 · 重试"

**视频生成按钮**（fingerprint 固定，每个 job 唯一）：
- 无 video 且无 active 任务 → 占位框 + "生成视频"
- pending/running → 分阶段进度条："正在混合音轨 · 70%" 等
- completed → 刷新 `fetchMaterialsAvailability`，占位框替换为 `<video>` 播放器
- failed → "生成失败 · 重试"

#### 5.3 关键行为

| 场景 | 行为 |
|------|------|
| 刷新页面 | `useBackgroundTask` mount 时查 latest active，自动恢复轮询或显示已完成状态 |
| 关闭浏览器 | 服务端执行不受影响，重开后同上 |
| 按不同 items 再点素材包 | fingerprint 变化 → 独立任务，不会被旧任务"恢复"误覆盖 |
| Gateway 重启 | 任务标 failed，用户点重试。`generate_video` 的 Job API 线程仍跑完，重试时命中 `already_exists` 秒回 |
| 同一按钮双击 | 服务端 fingerprint 去重，返回已有 task_id |

---

## 产物生命周期

| 产物 | 路径 | 清理时机 |
|------|------|---------|
| 素材包 zip | `{project_dir}/exports/materials_{task_id}.zip` | 同 job 新任务创建时删旧；项目过期（7 天保留策略）时随目录删除 |
| 渲染状态 json | `{project_dir}/publish/render_status.json` | 随项目生命周期；下次渲染直接覆盖 |
| 渲染产物 mp4 | `{project_dir}/publish/dubbed_video.mp4`（既有） | 不变，随项目生命周期 |
| `background_tasks` 表行 | DB | 保留 30 天的定时清理（可放到后续 migration） |

---

## 已知限制

- **Gateway 重启行为**：`running/pending` 统一标 `failed`。用户需要手动点重试。不做自动续跑——避免付费路径被误触发、避免状态机复杂化。
- **同进程 asyncio**：`asyncio.create_task` 只在创建任务的 Gateway 进程中执行。Gateway 多副本部署时，DB 一致，但任务执行绑定特定进程。本项目当前单副本，不构成问题。
- **Job API 线程池**：`threading.Thread` 不做全局并发限制。同一 Gateway 进程上限由 asyncio.create_task 数量软限。若未来出现资源打满，再加 `asyncio.Semaphore`。
- **进度百分比精度**：`muxing` 阶段通过 FFmpeg `-progress pipe:1` 解析，精度到秒级。若解析失败降级为"阶段名显示 + 不定进度动画"，不阻塞主流程。

---

## 未来演进（不在本轮）

- **收编 label_tasks**：本轮跑稳 6 周后，若 DB queue 模式证明合理，再把 label_tasks 迁到 `background_tasks` 统一。
- **付费 API 异步化**（voice_clone / tts_preview）：需独立设计，至少包含用户意图 token、禁用自动重试、idempotency key 扣费保护。不与本方案混做。
- **Job API FastAPI 化**：Job API 整体重构时，可把 `generate-video` 的 threading + 状态文件换成 async + DB，和 Gateway 共享 `background_tasks` 表。

---

## 改动文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `gateway/alembic/versions/014_add_background_tasks.py` | 新 | `background_tasks` 表 migration |
| `gateway/background_task_models.py` | 新 | SQLAlchemy 模型 |
| `gateway/background_task_queue.py` | 新 | Queue + 状态机 + stale recovery |
| `gateway/background_task_executors.py` | 新 | `execute_materials_pack` + `execute_generate_video` |
| `gateway/background_task_api.py` | 新 | 4 个 REST 端点 |
| `gateway/main.py` | 改 | 注册 router（与 materials_router 同级，在代理 catch-all 前）+ startup stale recovery |
| `gateway/materials_api.py` | 改 | 抽出 manifest 解析 + 路径安全校验为共享 helper，保留旧同步端点一段时间作为 fallback |
| `src/services/jobs/api.py` | 改 | `/jobs/{id}/generate-video` 改造为立即返回 + threading 后台执行；新增 status 查询端点 |
| `src/modules/output/publish/video_renderer.py` | 改（轻） | `render()` 可选接受 progress callback，用于写 render_status.json |
| `frontend-next/src/lib/react/useBackgroundTask.ts` | 新 | 通用异步任务 hook |
| `frontend-next/src/lib/api/downloads.ts` | 改 | 新增 task API URL builders + fingerprint helper |
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | 改 | 素材包按钮 + 视频生成按钮改用 hook |

---

## 验证

### 必跑回归

- `tests/test_gateway_route_coverage.py` — 新增的 4 个 task 路由必须进覆盖；验证路由顺序（不被代理吞掉）
- `tests/test_publish_backend.py` — `generate-video` 异步化改造后，原有渲染成功/失败用例仍然过
- 新增 `tests/test_background_task_queue.py`：
  - create → get → mark_* 状态机
  - fingerprint 去重：同参数 create 两次返回同 task_id
  - latest active 只返回 pending/running
  - stale recovery 把 running/pending 清到 failed
- 新增 `tests/test_materials_pack_executor.py`：
  - 500MB 限制 → 任务标 failed
  - path traversal attempt → artifact 解析拒绝
  - 新任务前清理旧 zip
- 新增 `tests/test_generate_video_executor.py`：
  - Gateway → Job API 协议正常路径
  - Job API 返回错误 → task 标 failed + error 透传
  - `already_exists` 快速返回路径

### 手工验收

1. **素材包常规**：点素材包 → 按钮变"打包中" → 几秒后"可下载"（高亮）→ 点击下载 zip
2. **素材包刷新恢复**：点击后立即刷新页面 → 按钮仍显示"打包中" → 完成后变"可下载"
3. **素材包参数切换**：选不同 items 再点 → 发起新任务，不会被旧任务状态污染
4. **视频生成短视频**：点生成 → 进度条走完 → 播放器出现
5. **视频生成长视频**（3 小时测试素材）：进度条分阶段显示 → 关闭浏览器 → 重开仍显示进度 → 最终完成
6. **Gateway 重启**：任务跑到一半重启 Gateway → 前端显示"失败 · 重试" → 点重试 → `generate_video` 命中 `already_exists` 秒回；`materials_pack` 重新打包
7. **并发双击**：快速点两下按钮 → 服务端只有一条任务
8. `npm run build` 通过
9. `alembic upgrade head` 成功
