# 快捷版 / 工作台版输出内容分层方案

> **Status:** ✅ shipped — Tasks 1-4 后端落地于 `ea53533`；Tasks 5-8 前端落地于 `189e649`；CodeX P2 补丁 `e97e6c5`（ResultMediaCard 独立渲染）。回归覆盖 `tests/test_job_api_express_filter.py`。
> **Date:** 2026-04-18
> **Shipped:** 2026-04-18 → 2026-04-19（逐次 commit 到 US 生产）
> **Goal:** 让 Express（快捷版）任务只对用户暴露"配音视频"，Studio（工作台版）保留全量产物（视频 + 配音音频 + 素材包）。
> **Architecture:** pipeline 继续产全部产物（技术上 `publish.dubbed_video` 依赖 `editor.dubbed_audio_complete`，不能裁剪产物侧）；过滤只在**用户暴露层**做——Job API 的 `/artifacts` / download whitelist / stream endpoint / `tts-segments-zip` 按 `job.service_mode` 决定返回什么 keys + 拒绝什么下载；前端 `ResultMediaCard` 和 `ResultDownloadList` 按 `serviceMode` 隐藏素材包相关 UI。**关键设计选择**：`materials-availability` 返回值保持真实（不做 false-mask），避免老 Express job 的 "生成视频" fallback 被误屏蔽。
> **Tech Stack:** Python stdlib `http.server`（Job API）+ Next.js / React 前端 + SQLAlchemy（只读 `job.service_mode` 字段）+ FFmpeg（不改）

## 修订记录

### v3（2026-04-18，整合 CodeX 二审意见）

- **P1 修复**：把 `ResultDownloadList` 过滤**下沉到组件本身**（新增 `serviceMode?` prop + 组件内部过滤），两处 callsite 都传 prop。原 v2 只在 `projects/[jobId]/page.tsx` 调用点过滤，漏了 `workspace/[jobId]/page.tsx` 也渲染 `ResultDownloadList` 的场景。
- **P2 修复**：**从 Express 白名单移除 `source.original_video`**。原因：
  - 前端 `DOWNLOADABLE_ARTIFACT_KEYS` 白名单当前**不含** `source.original_video`，`buildResultDownloadUrl` / `toResultDownloadItems` 也不支持它——要让 Express UI 能展示"原始视频下载"需要扩展 TS 类型系统 + 后端 download path 兼容
  - Express 用户心智是"要配音视频直接发布"——原始视频不是核心产出
  - 去掉它更简洁、更对齐定位
  - Express 可见产物收敛为 `{publish.dubbed_video}` + `{publish.dubbed_video_poster}`（poster 用于 LazyVideoPlayer 预览，不进 ResultDownloadList）

### v2（2026-04-18，整合 CodeX 一审意见）

- **P1 修复①**：补 `/jobs/{id}/artifacts` 暴露面过滤（原方案漏）+ 前端 `ResultDownloadList` 按 `serviceMode` 兜底过滤
- **P1 修复②**：放弃 "materials-availability false-mask" 方案，改为**保持真实值**，仅在 UI 和下载层做权限过滤——避免老 Express job `hasAudio=false` 被误导致 "生成视频" fallback 按钮消失
- **P2 修复③**：Task 4 明确补 `frontend-next/src/types/api.ts` 的 `ApiJobRecord.service_mode` 字段（TS 类型完整性）
- **P2 修复④**：Phase 0（12 个 video-output WIP 的 baseline commit）从本方案 Task 剥离，改为独立的**前置条件**；本方案假设 baseline 已提交
- **统一**：测试文件从 `tests/test_web_ui.py` 迁到**新建的** `tests/test_job_api_express_filter.py`
- **更正**：前端 `ResultMediaCard` 调用点从 3 处改为实际的 2 处（workspace 页不渲染 ResultMediaCard）

### v1（2026-04-18 初稿）

初稿，见 `docs/plans/2026-04-18-express-studio-output-filter-plan.md` 历史版本。

---

## Context

当前生产（HEAD `3cf4a38` + 美国主机）：无论 Express 还是 Studio，**都走同一套 UI**——显示素材包按钮、配音音频下载、视频播放器。但两种模式的用户心智天然不同：

- **Express（快捷版）**：全自动流程，用户要的是"拿到配音视频直接发布"；素材包对他是噪音
- **Studio（工作台版）**：可以审核、克隆音色、per-speaker 选引擎；素材包是核心价值

### 技术约束

`src/modules/output/output_dispatcher.py` 的 PUBLISH 分支实际上**必须**先产 editor package（因为 `VideoRenderer` 需要 `dubbed_audio_path` 作为 FFmpeg `-i` 输入）。所以 Express 用户的"只要视频"不是削减产物，而是**削减用户可见的下载入口**——磁盘上文件全在，只是 API 不暴露、前端不显示、下载端点拒绝访问。

### 前置条件：video-output baseline commit

Worktree 上当前有 12 个未提交的 WIP 文件（2026-04-18 另一轮会话做的视频输出改造：poster / LazyVideoPlayer / pipeline PUBLISH 硬编码 / 三轨 ambient audio mix）。**本方案的所有改动建立在那批 WIP 已 commit 并成为 HEAD 的基础上**，具体文件：

- `src/pipeline/process.py`（EDITOR→PUBLISH 硬编码）
- `src/modules/output/output_dispatcher.py`
- `src/modules/output/publish/publish_models.py`
- `src/modules/output/publish/video_renderer.py`（ambient mix + poster）
- `src/services/jobs/api.py`（`stream/poster` 端点）
- `src/services/jobs/video_render_async.py`（manifest 注册 poster_path）
- `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx`
- `frontend-next/src/components/workspace/ResultMediaCard.tsx`（LazyVideoPlayer）
- `frontend-next/src/lib/api/downloads.ts`（`buildStreamUrl` 增加 `poster`）
- `docs/plans/2026-04-16-background-task-system-plan.md`（文档更新）
- `docs/plans/2026-04-16-ui-navigation-redesign-plan.md`（文档更新）
- `docs/plans/2026-04-16-video-output-subtitles-player-plan.md`（文档更新）

**⚠️ 开工前**：这 12 个 WIP 需要先作为一个独立 baseline commit 落地（最少拆两拨：runtime code 一个 commit，docs 更新一个 commit），**不是本方案的 Task**。本方案的 Task 1 开始时假设 HEAD 已经包含它们。

---

## Non-Goals（硬性边界）

- **不**改 pipeline 产物流程。pipeline 对两种 mode 都跑 `targets=[OutputTarget.PUBLISH]`（baseline 里已改），产 editor + publish。
- **不**让用户选择 `output_target`。用 `service_mode` 覆盖这个决策维度——用户选快捷版/工作台版时已经表态。
- **不**改 `src/services/jobs/service.py:96` 的 `!= 'editor'` strict check。前端仍然只发 `output_target: 'editor'`，该字段继续 legacy 化，不动它。
- **不**改 `materials-availability` 的返回 shape 或字段值。它继续反映"磁盘文件是否存在"的**事实查询**；权限过滤（谁能看、谁能下）挪到 UI 和 download 层做。避免 P1 #2 的老 Express job `hasAudio=false` 冲突。
- **不**改计费 / plan 限制。Express / Studio 的定价分层本来就有，不随本方案变化。
- **不**删除 Express job 的素材文件。磁盘上保留，只是不暴露；7 天过期随项目一起清理。
- **不**回填老 Express 任务的 `publish.dubbed_video`。HEAD EDITOR-only 时期的 Express job 没产视频，UI 仍通过"生成视频"按钮走 Export Tasks v1 的异步渲染（已部署）补救。
- **不**把 video-output baseline 的 12 个 WIP 作为本方案的 task（见前置条件章节）。

---

## Design

### 1. 四层过滤策略

```
┌──────────────────────────────────────────────────────────┐
│ Pipeline 侧（baseline 已定，本方案不改）                  │
│   两种 mode 都产 editor + publish.dubbed_video           │
│   所有产物写入 {project_dir}/                            │
└──────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ 暴露层过滤（Job API）——本方案后端核心                    │
│   materials-availability: 不动（保持真实值）             │
│   /artifacts:             Express 过滤返回 keys          │
│   /download/{key}:        Express 白名单限制 403         │
│   /stream/{kind}:         Express 禁 audio stream 403    │
│   /tts-segments-zip:      Express 禁 403                 │
└──────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ 前端 UI（本方案前端核心）                                 │
│   ResultMediaCard:   Express 隐藏 "配音音频" + "素材包" │
│   ResultDownloadList: Express 按 serviceMode 兜底过滤    │
│   视频播放器 + "生成视频" fallback + "配音视频"下载保留  │
└──────────────────────────────────────────────────────────┘
```

### 2. Express 白名单（v3：收敛，去掉 source.original_video）

```python
# 用于 /artifacts 过滤
EXPRESS_ALLOWED_ARTIFACT_KEYS = {
    "publish.dubbed_video",
    "publish.dubbed_video_poster",   # 支持 LazyVideoPlayer 预览
}

# 用于 /download/{key} 白名单（下载权限）
EXPRESS_ALLOWED_DOWNLOAD_KEYS = {
    "publish.dubbed_video",
}

# 用于 /stream/{kind} 白名单
EXPRESS_ALLOWED_STREAM_KINDS = {"video", "poster"}   # 禁 audio
```

**前端 `ResultDownloadList` 过滤的同义集（不含 poster，因为 poster 不走 download list）**：

```typescript
const EXPRESS_VISIBLE_DOWNLOAD_KEYS = new Set(['publish.dubbed_video'])
```

Studio 无白名单限制，沿用现有 `PUBLIC_RESULT_DOWNLOAD_KEYS` 全集。

**v3 取舍说明**：Express 不暴露 `source.original_video`——见"修订记录 v3"。

### 3. 老 Express job 的兼容（v2 修订核心）

**问题定位**（CodeX P1 #2）：
- 当前 `ResultMediaCard` 的 `VideoGenerationControl` fallback 分支条件是 `hasVideo === false && hasAudio === true`
- 初稿 Task 1 打算把 Express 下 `materials-availability.dubbed_audio` 固定 `false`
- 后果：老 Express job（只有 editor 产物、没有 publish.dubbed_video）会进入 `hasVideo=false && hasAudio=false`，**连 "生成视频" 按钮都看不到**

**v2 修正**：`materials-availability` 保持真实值，不做 false-mask。

- `hasVideo` 和 `hasAudio` 在 Express 下都反映**磁盘实际**
- "配音音频"下载按钮的显隐**解耦**到 `serviceMode` 条件（Express 隐藏）
- "生成视频"fallback 的显隐仍基于 `hasVideo=false && hasAudio=true`——因为 fallback 技术上需要 dubbed_audio 作为 FFmpeg 输入，这是事实条件，不是权限条件

**结果**：
- **老 Express job**（只 editor）：`hasVideo=false, hasAudio=true` → fallback 显示"生成视频"按钮，Express 用户能补救
- **新 Express job**（已跑 PUBLISH）：`hasVideo=true, hasAudio=true` → 显示 LazyVideoPlayer + "配音视频"下载；"配音音频"和"素材包"按钮被 serviceMode 隐藏
- **Studio job**：无论新老，完整 UI

### 4. Job API 行为对照表（v2 增补 `/artifacts`）

| Endpoint | Studio（现行不变） | Express（本方案新加） |
|---|---|---|
| `GET /jobs/{id}/materials-availability` | 返回 7 key 真实值 | **不变**（保持真实值；v2 修订） |
| `GET /jobs/{id}/artifacts` | 返回全集 | **按白名单过滤**（v2 新加 / v3 收敛：只返 `publish.dubbed_video` + `publish.dubbed_video_poster`） |
| `GET /jobs/{id}/download/{key}` | `PUBLIC_RESULT_DOWNLOAD_KEYS` 白名单 | 额外限制 `∈ EXPRESS_ALLOWED_DOWNLOAD_KEYS`（v3：只 `publish.dubbed_video`），否则 403 |
| `GET /jobs/{id}/stream/video` | 允许 | 允许 |
| `GET /jobs/{id}/stream/audio` | 允许 | **403** |
| `GET /jobs/{id}/stream/poster` | 允许 | 允许 |
| `GET /jobs/{id}/tts-segments-zip` | 允许 | **403** |

### 5. 前端 UI 对照表

| 元素 | Studio | Express |
|---|---|---|
| 视频播放器 / LazyVideoPlayer | ✓ | ✓ |
| "生成视频" fallback 按钮（`hasVideo=false && hasAudio=true` 时） | ✓ | ✓（老 job 用） |
| "配音视频"下载按钮 | ✓ | ✓ |
| "配音音频"下载按钮 | ✓ | ✗ 按 `serviceMode` 隐藏 |
| "素材包"按钮 + dialog | ✓ | ✗ 按 `serviceMode` 隐藏 |
| `ResultDownloadList`（项目详情页 + workspace 页） | 全集 | **组件内部按 `serviceMode` 过滤**，Express 下只剩"配音视频"一项（v3：filter 下沉到组件；v2 原本只在 projects 详情页 callsite 过滤，漏了 workspace 页） |

---

## File Structure

### 后端（4 处改动，+ 1 个新测试文件）

| 文件 | 类型 | 作用 |
|---|---|---|
| `src/services/jobs/api.py` | 改 | `/artifacts` 按 mode 过滤、`/download/{key}` 加 Express 白名单、`/stream/{kind}` 禁 Express audio、`/tts-segments-zip` 禁 Express |
| `tests/test_job_api_express_filter.py` | **新增** | 覆盖 `/artifacts` 过滤、`/download` 403、`/stream/audio` 403、`/tts-segments-zip` 403、`/materials-availability` 保持真实值 |

### 前端（7 处改动）

| 文件 | 类型 | 作用 |
|---|---|---|
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | 改 | 新增 `serviceMode` prop；Express 下隐藏 "配音音频" 按钮 + "素材包" 按钮/dialog；**保留** "生成视频" fallback |
| `frontend-next/src/components/result-download-list.tsx` | 改（**v3 新增**） | 新增 `serviceMode?` prop；组件内部按 `EXPRESS_VISIBLE_DOWNLOAD_KEYS` 过滤 items |
| `frontend-next/src/app/(app)/projects/page.tsx` | 改 | 传 `serviceMode={job.serviceMode}` 给 `<ResultMediaCard>` |
| `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` | 改 | 同上 + `<ResultDownloadList serviceMode={...}>` |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 改（**v3 新增**） | `<ResultDownloadList serviceMode={jobDetail?.serviceMode}>` |
| `frontend-next/src/lib/api/mappers.ts` | 改 | 在 `toJobSummary` 里补 `serviceMode: payload.service_mode` 映射（如缺） |
| `frontend-next/src/types/api.ts` | **改**（v2 新增） | `ApiJobRecord` 补 `service_mode?: 'express' \| 'studio'` 字段 |
| `frontend-next/src/types/jobs.ts` | 查看 | 确认 `JobSummary.serviceMode` 字段已存在（v1 查过：是） |

---

## Tasks

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

> **前置**：开工前请确认 HEAD 已经包含 video-output baseline commit（见 Context 章节）。如果 `git status` 还有那 12 个 WIP 未提交，先完成基线 commit 再回来。

---

### Task 1: 后端 `/artifacts` endpoint 按 service_mode 过滤

**Files:**
- Modify: `src/services/jobs/api.py`（`/artifacts` handler，约 68-75 行）
- Test: `tests/test_job_api_express_filter.py`（新建）

- [ ] **Step 1: 新建测试文件骨架 + 第一个失败测试**

```python
# tests/test_job_api_express_filter.py
"""Express 模式下 Job API 对外暴露层的过滤行为。

覆盖 /artifacts、/download/{key}、/stream/{kind}、/tts-segments-zip。
也覆盖 /materials-availability 的"保持真实值"契约（见 Task 4）——
该端点在 Express 下不过滤，见方案 Design 章节第 3 节。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ... fixture 导入沿用 test_job_api_phase1.py 的模式

def _build_express_job_fixture(tmp_path: Path, ...):
    """构造一个 service_mode=express 且磁盘有全量 editor + publish 产物的 job fixture."""
    ...

def test_artifacts_express_returns_only_publish_keys(...):
    """Express job 的 /artifacts 只返回 publish.dubbed_video + publish.dubbed_video_poster
    （v3 收敛：不含 source.original_video，因为前端 DOWNLOADABLE_ARTIFACT_KEYS 不支持）."""
    job_id, handler = _build_express_job_fixture(tmp_path, has_video=True, has_poster=True)
    resp = call_artifacts(handler, job_id)
    returned_keys = {item["key"] for item in resp["artifacts"]}
    assert returned_keys == {"publish.dubbed_video", "publish.dubbed_video_poster"}
    assert "source.original_video" not in returned_keys   # v3
    assert "editor.dubbed_audio_complete" not in returned_keys
    assert "editor.subtitles" not in returned_keys
```

- [ ] **Step 2: 跑测试确认失败**
  ```bash
  .venv/Scripts/python.exe -m pytest tests/test_job_api_express_filter.py::test_artifacts_express_returns_only_video_keys -v
  ```
  Expected: FAIL（当前 handler 不看 service_mode）

- [ ] **Step 3: 改 `src/services/jobs/api.py` `/artifacts` handler**

在 68-75 行附近 `get_artifacts(path_parts[1])` 调用前后加过滤：

```python
_EXPRESS_ALLOWED_ARTIFACT_KEYS = {
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
}

# existing branch:
# if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "artifacts":
#     artifacts_payload = service.get_artifacts(path_parts[1])

# ADD after obtaining payload:
record = service.require_job(path_parts[1])
if getattr(record, "service_mode", None) == "express":
    items = artifacts_payload.get("artifacts") or []
    filtered = [it for it in items if it.get("key") in _EXPRESS_ALLOWED_ARTIFACT_KEYS]
    artifacts_payload = {**artifacts_payload, "artifacts": filtered}

self._write_json(HTTPStatus.OK, artifacts_payload)
```

**设计点**：过滤在 payload 组装后做，保持原 `service.get_artifacts()` 行为不变（磁盘/manifest 层面真实），权限过滤只在暴露层。

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 追加 "Studio 不受影响" + "老 Express job (无 publish.dubbed_video)" 两条测试**

```python
def test_artifacts_studio_returns_all_keys(...):
    """Studio job 的 /artifacts 不过滤，返回全集."""
    ...

def test_artifacts_express_old_job_without_video_returns_empty_publish_set(...):
    """老 Express job 没 publish.dubbed_video，/artifacts 既不返 publish.*（不存在）
    也不返 editor.*（被过滤）。返回空 artifacts 列表符合预期——UI 的
    VideoGenerationControl fallback 不依赖 /artifacts，而是靠
    /materials-availability 的 hasAudio=true 触发。"""
    job_id, handler = _build_express_job_fixture(tmp_path, has_video=False, has_poster=False)
    resp = call_artifacts(handler, job_id)
    returned_keys = {item["key"] for item in resp["artifacts"]}
    assert "editor.dubbed_audio_complete" not in returned_keys
    assert "editor.subtitles" not in returned_keys
    assert "source.original_video" not in returned_keys
    # 大概率为空集（只有磁盘有 publish.* 才返）
    assert all(k in {"publish.dubbed_video", "publish.dubbed_video_poster"} for k in returned_keys)
```

- [ ] **Step 6: 跑全部测试确认通过**
  ```bash
  .venv/Scripts/python.exe -m pytest tests/test_job_api_express_filter.py -v
  ```

- [ ] **Step 7: Commit**
  ```bash
  git add src/services/jobs/api.py tests/test_job_api_express_filter.py
  git commit -m "feat(jobs-api): /artifacts 按 service_mode 过滤

  Express 模式只暴露 publish.dubbed_video + publish.dubbed_video_poster；
  editor.* 和 source.* 产物一律不返回。Studio 保持全集。
  /materials-availability 在 Express 下仍返回真实值（见 plan 第 3 节
  对 CodeX P1 #2 的解释）。source.original_video 从 Express 白名单
  移除见 plan v3 修订。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 2: 后端 `/download/{key}` 按 Express 白名单拒绝

**Files:**
- Modify: `src/services/jobs/api.py`（key-based download handler，约 84-105 行）
- Test: `tests/test_job_api_express_filter.py`

- [ ] **Step 1: 写失败测试**

```python
def test_download_rejects_editor_artifact_for_express_mode(...):
    """Express job 下 /download/editor.dubbed_audio_complete 返回 403."""
    job_id, handler = _build_express_job_fixture(tmp_path, has_video=True, has_audio=True)
    status, body = call_download(handler, job_id, "editor.dubbed_audio_complete")
    assert status == 403
    assert "Express" in body.get("error", "") or "不可下载" in body.get("error", "")

def test_download_rejects_source_video_for_express_mode(...):
    """Express job 下 /download/source.original_video 也 403（v3 收敛）."""
    status, _ = call_download(handler, job_id, "source.original_video")
    assert status == 403

def test_download_allows_video_for_express_mode(...):
    status, _ = call_download(handler, job_id, "publish.dubbed_video")
    assert status == 200

def test_download_allows_all_for_studio_mode(...):
    for key in ("publish.dubbed_video", "source.original_video",
                "editor.dubbed_audio_complete", "editor.subtitles"):
        status, _ = call_download(studio_handler, studio_job_id, key)
        assert status == 200, f"key={key} should pass for studio"
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 key-based download handler**

在 85-105 行附近，`record = service.require_job(job_id)` 之后加：

```python
_EXPRESS_ALLOWED_DOWNLOAD_KEYS = {
    "publish.dubbed_video",
}

# After: record = service.require_job(job_id)
if getattr(record, "service_mode", None) == "express" \
        and download_key not in _EXPRESS_ALLOWED_DOWNLOAD_KEYS:
    self._write_json(
        HTTPStatus.FORBIDDEN,
        {"error": f"该产物对 Express 任务不可下载: {download_key}"},
    )
    return
```

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: Commit**
  ```bash
  git add src/services/jobs/api.py tests/test_job_api_express_filter.py
  git commit -m "feat(jobs-api): Express 任务 /download 白名单

  Express 模式只允许下载 publish.dubbed_video；editor.* 和
  source.original_video 都 403。Studio 行为不变。
  (v3 收敛：原方案允许 source.original_video，但前端
  DOWNLOADABLE_ARTIFACT_KEYS 不支持它，且 Express 心智是
  '要配音视频直接发布'，不需要原始视频——见 plan v3 修订。)

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 3: 后端 `/stream/{kind}` 和 `/tts-segments-zip` 拒绝 Express

**Files:**
- Modify: `src/services/jobs/api.py`（stream handler 约 135-165 行、tts-segments-zip 约 107-130 行）
- Test: `tests/test_job_api_express_filter.py`

- [ ] **Step 1: 写失败测试**

```python
def test_stream_audio_rejects_express_mode(...):
    status, _ = call_stream(handler, job_id, "audio")
    assert status == 403

def test_stream_video_allows_express_mode(...):
    status, _ = call_stream(handler, job_id, "video")
    assert status == 200

def test_stream_poster_allows_express_mode(...):
    status, _ = call_stream(handler, job_id, "poster")
    assert status in (200, 404)  # 404 if old job without poster

def test_tts_segments_zip_rejects_express_mode(...):
    status, _ = call_tts_segments_zip(handler, job_id)
    assert status == 403
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改 `/stream/{kind}` handler**

```python
_EXPRESS_ALLOWED_STREAM_KINDS = {"video", "poster"}

# In stream/{kind} branch, right after `record = service.require_job(job_id)`:
if getattr(record, "service_mode", None) == "express" \
        and kind not in _EXPRESS_ALLOWED_STREAM_KINDS:
    self._write_json(
        HTTPStatus.FORBIDDEN,
        {"error": f"该媒体流对 Express 任务不可访问: {kind}"},
    )
    return
```

- [ ] **Step 4: 改 `/tts-segments-zip` handler**

```python
# In tts-segments-zip branch, right after `record = service.require_job(job_id)`:
if getattr(record, "service_mode", None) == "express":
    self._write_json(
        HTTPStatus.FORBIDDEN,
        {"error": "TTS 分段包对 Express 任务不可访问"},
    )
    return
```

- [ ] **Step 5: 跑测试确认通过**

- [ ] **Step 6: Commit**
  ```bash
  git add src/services/jobs/api.py tests/test_job_api_express_filter.py
  git commit -m "feat(jobs-api): Express 任务 /stream/audio 和 tts-segments-zip 返回 403

  Express 模式只允许 stream/video 和 stream/poster；audio stream 和
  tts segments zip 均 403。Studio 行为不变。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 4: 后端确认 `/materials-availability` 保持真实值（无代码改动，纯测试）

**Files:**
- Test: `tests/test_job_api_express_filter.py`

此 Task 存在是**为了钉住 v2 的关键设计决策**（CodeX P1 #2 修复）：避免未来有人以"Express 该隐藏 audio 字段"的直觉把 `materials-availability` 改成 false-mask，从而破坏老 Express job 的 "生成视频" fallback。

- [ ] **Step 1: 写钉死测试**

```python
def test_materials_availability_express_returns_real_audio_value_for_video_fallback(...):
    """v2 关键契约：Express job 的 dubbed_audio 保持真实值，避免
    前端 ResultMediaCard 的 'hasVideo=false && hasAudio=true' fallback
    被误屏蔽。回归参考：CodeX 评审 P1 #2。
    """
    # 老 Express job：磁盘只有 editor.dubbed_audio_complete，没有 publish.dubbed_video
    job_id, handler = _build_express_job_fixture(
        tmp_path, has_video=False, has_audio=True, has_poster=False,
    )
    availability = call_materials_availability(handler, job_id)
    assert availability["dubbed_audio"] is True, (
        "Express 下 dubbed_audio 必须保持真实值 (True if file exists), "
        "否则前端 VideoGenerationControl fallback 会因为 hasAudio=false 消失"
    )
    assert availability["dubbed_video"] is False  # 事实反映
    # subtitles/segments 也保持真实值，但 Express UI 不展示它们
```

- [ ] **Step 2: 跑测试确认通过**（因为目前 handler 本来就没按 mode 过滤）
  ```bash
  .venv/Scripts/python.exe -m pytest tests/test_job_api_express_filter.py::test_materials_availability_express_returns_real_audio_value_for_video_fallback -v
  ```
  Expected: PASS

- [ ] **Step 3: Commit**
  ```bash
  git add tests/test_job_api_express_filter.py
  git commit -m "test(jobs-api): 钉死 Express /materials-availability 保持真实值契约

  CodeX 评审 P1 #2：如果 /materials-availability 在 Express 下把
  dubbed_audio 强制 false，老 Express job 会因为 hasVideo=false &&
  hasAudio=false 走不到 VideoGenerationControl fallback 分支，用户
  失去'生成视频'补救入口。本测试钉死这个契约，防止未来回退。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 5: 前端 TS 类型链补全（`types/api.ts` + `mappers.ts`）

**Files:**
- Modify: `frontend-next/src/types/api.ts`
- Modify: `frontend-next/src/lib/api/mappers.ts`
- View: `frontend-next/src/types/jobs.ts`（确认 `JobSummary.serviceMode` 存在）

- [ ] **Step 1: 检查现状**
  ```bash
  grep -n "service_mode\|serviceMode" \
    frontend-next/src/types/api.ts \
    frontend-next/src/types/jobs.ts \
    frontend-next/src/lib/api/mappers.ts
  ```
  Expected:
  - `types/api.ts`：当前**没有** `service_mode` 字段（CodeX P2 #3 确认）
  - `types/jobs.ts`：应该有 `serviceMode?: 'express' | 'studio'`（v1 查过有）
  - `mappers.ts`：`toJobSummary` 是否有 `serviceMode: payload.service_mode` 这一行——**可能缺**

- [ ] **Step 2: 补 `types/api.ts`**

在 `ApiJobRecord` interface（约第 8 行附近）里加字段：

```typescript
export interface ApiJobRecord {
  // ... existing fields
  service_mode?: 'express' | 'studio'
}
```

- [ ] **Step 3: 补 `mappers.ts`（如缺）**

在 `toJobSummary` 函数返回对象里加：

```typescript
serviceMode: payload.service_mode,
```

（类型自动从 `types/api.ts` 推断为 `'express' | 'studio' | undefined`，对齐 `JobSummary.serviceMode`。）

- [ ] **Step 4: 确认 `types/jobs.ts` `JobSummary.serviceMode`**

若缺，补 `serviceMode?: 'express' | 'studio'`。

- [ ] **Step 5: TS 编译验证**
  ```bash
  cd frontend-next && node_modules/.bin/next build
  ```
  Expected: Exit 0，无新 TS error

- [ ] **Step 6: Commit**
  ```bash
  git add frontend-next/src/types/api.ts \
    frontend-next/src/types/jobs.ts \
    frontend-next/src/lib/api/mappers.ts
  git commit -m "types(frontend): ApiJobRecord + toJobSummary 补 service_mode 字段

  Express/Studio 分层 UI 依赖这个字段流转。CodeX 评审 P2 #3 指出原
  types/api.ts 缺 service_mode，本次补齐。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 6: 前端 `ResultMediaCard` 按 serviceMode 隐藏素材 UI

**Files:**
- Modify: `frontend-next/src/components/workspace/ResultMediaCard.tsx`

- [ ] **Step 1: 扩展 props + 新增 `isExpress` 派生**

```typescript
interface ResultMediaCardProps {
  jobId: string
  serviceMode?: 'express' | 'studio'   // 新增
}

export function ResultMediaCard({ jobId, serviceMode }: ResultMediaCardProps) {
  const isExpress = serviceMode === 'express'
  // ... existing hooks / state
```

- [ ] **Step 2: Express 下隐藏"配音音频"按钮**

找到现有 `{hasAudio && audioDownloadUrl && <a href={audioDownloadUrl}>...`，包条件：

```tsx
{!isExpress && hasAudio && audioDownloadUrl && (
  <a href={audioDownloadUrl} download>
    <Button variant="outline" size="sm" className="gap-2">
      <Music className="h-4 w-4" />
      配音音频
      <Download className="h-3 w-3" />
    </Button>
  </a>
)}
```

- [ ] **Step 3: Express 下隐藏"素材包"按钮 + dialog**

找到现有 `<MaterialsPackButton ... />` 和 dialog 渲染块，各包条件：

```tsx
{!isExpress && (
  <MaterialsPackButton
    task={packTask}
    onOpenDialog={() => setShowPackDialog(true)}
    onDownload={handleDownloadPack}
  />
)}

{!isExpress && showPackDialog && (
  <div className="mt-4 rounded-lg border ...">
    {/* ... existing dialog JSX */}
  </div>
)}
```

- [ ] **Step 4: 保留 "生成视频" fallback**

**不要**改 `VideoGenerationControl` 的显示条件。它仍是 `hasVideo === false && hasAudio === true`。v2 设计刻意保持 availability 真实值让这个条件对 Express 老 job 成立。

- [ ] **Step 5: npm build + eslint 验证**
  ```bash
  cd frontend-next && node_modules/.bin/next build
  cd frontend-next && node node_modules/eslint/bin/eslint.js src/components/workspace/ResultMediaCard.tsx
  ```
  Expected: Exit 0

- [ ] **Step 6: Commit**
  ```bash
  git add frontend-next/src/components/workspace/ResultMediaCard.tsx
  git commit -m "feat(frontend): ResultMediaCard Express 模式隐藏素材 UI

  新增 serviceMode prop。Express 下隐藏'配音音频'和'素材包'按钮/dialog。
  视频播放器、生成视频 fallback、配音视频下载 对两种 mode 保留。
  v2 关键：保留 VideoGenerationControl 的 hasVideo=false && hasAudio=true
  触发条件（见 plan 第 3 节 CodeX P1 #2 修复）。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 7: 前端 `ResultDownloadList` 组件层按 serviceMode 过滤（v3 下沉）

**Files:**
- Modify: `frontend-next/src/components/result-download-list.tsx`

**背景**（CodeX 二审 P1）：项目详情页 + workspace 页**都**渲染 `ResultDownloadList`，数据来自 `getProjectArtifacts()` → `toResultDownloadItems()`。v2 打算在每个 page callsite 过滤，但 CodeX 指出 workspace 页被漏了。v3 改为**在组件内部过滤**，所有调用方只需传 `serviceMode` prop——新增页面也不会漏。

后端 Task 1 已经过滤 `/artifacts` 返回的 items，所以本 Task 的前端过滤是**兜底层**：即便后端改动被误 revert，前端仍能按 `serviceMode` 再过滤一次展示。

- [ ] **Step 1: 读 `ResultDownloadList` 当前实现**

```bash
cat frontend-next/src/components/result-download-list.tsx
```

确认 props 形状（当前只有 `items`）。

- [ ] **Step 2: 扩 props + 内部过滤**

```tsx
// frontend-next/src/components/result-download-list.tsx

const EXPRESS_VISIBLE_DOWNLOAD_KEYS = new Set<string>([
  'publish.dubbed_video',
])

type ResultDownloadListProps = {
  items: readonly ResultDownloadItem[]
  serviceMode?: 'express' | 'studio'   // v3 新增
}

export function ResultDownloadList({ items, serviceMode }: ResultDownloadListProps) {
  const visibleItems = serviceMode === 'express'
    ? items.filter((it) => EXPRESS_VISIBLE_DOWNLOAD_KEYS.has(it.key))
    : items
  // ... existing rendering using visibleItems
}
```

**设计点**：过滤在组件内做而不是在 mapper 里——保持 `toResultDownloadItems()` 的 pure mapper 职责（它不需要知道 serviceMode）；UI 层过滤天然隔离，新增 page callsite 只要记得传 prop 就安全。

- [ ] **Step 3: npm build 验证**
  ```bash
  cd frontend-next && node_modules/.bin/next build
  ```
  Expected: Exit 0（此 Task 独立可 build，调用点传 prop 在 Task 8 做）

- [ ] **Step 4: Commit**
  ```bash
  git add frontend-next/src/components/result-download-list.tsx
  git commit -m "feat(frontend): ResultDownloadList 组件内部按 serviceMode 过滤

  新增 serviceMode?: 'express' | 'studio' prop。Express 下只显示
  publish.dubbed_video 一项。后端 /artifacts Task 1 已做相同过滤，
  前端这层是兜底。

  v3: filter 下沉到组件而非 callsite，避免遗漏新增页面
  （CodeX 二审指出原 v2 漏了 workspace 页）。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 8: 调用点传 `serviceMode` prop（v3：ResultMediaCard 2 处 + ResultDownloadList 2 处）

**Files:**
- Modify: `frontend-next/src/app/(app)/projects/page.tsx` — 传 `serviceMode` 给 `<ResultMediaCard>`
- Modify: `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` — 传 `serviceMode` 给 `<ResultMediaCard>` **和** `<ResultDownloadList>`
- Modify: `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` — 传 `serviceMode` 给 `<ResultDownloadList>`（**v3 新增**）

**注**：
- v1 误列 workspace 渲染 ResultMediaCard——实际没有，workspace 只渲染 ResultDownloadList
- v2 只让 projects 详情页传 prop，漏了 workspace 页——CodeX 二审 P1
- v3：ResultMediaCard callsites = 2 处（projects list + detail），ResultDownloadList callsites = 2 处（projects detail + workspace），本 Task 覆盖总 3 个文件

- [ ] **Step 1: projects 列表页**（ResultMediaCard 唯一出现）
  ```tsx
  // 之前
  return <ResultMediaCard jobId={job.id} />
  // 之后
  return <ResultMediaCard jobId={job.id} serviceMode={job.serviceMode} />
  ```

- [ ] **Step 2: projects 详情页**（ResultMediaCard + ResultDownloadList 都在）
  ```tsx
  <ResultMediaCard jobId={jobId} serviceMode={job?.serviceMode} />
  // ...
  <ResultDownloadList items={downloads} serviceMode={job?.serviceMode} />
  ```

- [ ] **Step 3: workspace 页**（只有 ResultDownloadList）
  ```tsx
  <ResultDownloadList items={downloads} serviceMode={jobDetail?.serviceMode} />
  ```
  （确认变量名——可能叫 `job` 或 `jobDetail` 或 `summary`，按实际 binding 取 serviceMode。）

- [ ] **Step 4: npm build 验证**
  ```bash
  cd frontend-next && node_modules/.bin/next build
  ```
  Expected: Exit 0

- [ ] **Step 5: Commit**
  ```bash
  git add frontend-next/src/app/\(app\)/projects/page.tsx \
    frontend-next/src/app/\(app\)/projects/\[jobId\]/page.tsx \
    frontend-next/src/app/\(app\)/workspace/\[jobId\]/page.tsx
  git commit -m "feat(frontend): 3 页面 callsite 传 serviceMode prop

  projects 列表/详情/workspace 页从 job summary 读 serviceMode 传给
  ResultMediaCard 和 ResultDownloadList，驱动 Express/Studio UI 分支。
  v3 修正 CodeX 二审 P1：workspace 页渲染 ResultDownloadList 也需要
  传 serviceMode，原方案遗漏。

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
  ```

---

### Task 9: 本地全量回归 + 前端 build

- [ ] **Step 1: 后端专项 + 相邻测试**
  ```bash
  .venv/Scripts/python.exe -m pytest \
    tests/test_job_api_express_filter.py \
    tests/test_job_api.py \
    tests/test_job_api_phase1.py \
    tests/test_job_api_phase2.py \
    tests/test_gateway_route_coverage.py \
    tests/test_background_task_queue.py \
    tests/test_background_task_api.py \
    tests/test_publish_backend.py \
    -q --disable-warnings
  ```
  Expected: 全部 PASS（除 baseline 已知遗留的 `test_project_output` / `test_process_pipeline` 7 个失败）

- [ ] **Step 2: 前端 build + eslint**
  ```bash
  cd frontend-next && node_modules/.bin/next build
  cd frontend-next && node node_modules/eslint/bin/eslint.js \
    src/components/workspace/ResultMediaCard.tsx \
    src/app/\(app\)/projects/page.tsx \
    src/app/\(app\)/projects/\[jobId\]/page.tsx \
    src/lib/api/mappers.ts \
    src/types/api.ts \
    src/types/jobs.ts
  ```
  Expected: Exit 0，无新增 error

---

### Task 10: 部署到 US 主机

**Files:** 打 tar + Upload + rebuild next + restart app + alembic（无新 migration）

- [ ] **Step 1: 打 tar 包**
  ```bash
  TS=$(date +%Y%m%d-%H%M%S)
  TAR="/c/Users/Administrator/Desktop/avt-express-filter-$TS.tar.gz"
  tar czf "$TAR" \
    src/services/jobs/api.py \
    frontend-next/src/components/workspace/ResultMediaCard.tsx \
    frontend-next/src/components/result-download-list.tsx \
    frontend-next/src/app/\(app\)/projects/page.tsx \
    frontend-next/src/app/\(app\)/projects/\[jobId\]/page.tsx \
    frontend-next/src/app/\(app\)/workspace/\[jobId\]/page.tsx \
    frontend-next/src/lib/api/mappers.ts \
    frontend-next/src/types/api.ts \
    frontend-next/src/types/jobs.ts
  ls -la "$TAR"
  ```

  **注**：tests/ 不进生产包。

- [ ] **Step 2: 上传 + 解压**
  ```bash
  "D:/daili/scripts/Deploy-Via-154.cmd" us "<TAR>" "//tmp/avt-deploy.tar.gz" \
    "cd /opt/aivideotrans/app && tar xzf //tmp/avt-deploy.tar.gz && echo ok"
  ```

- [ ] **Step 3: restart app**（src/ bind mount，热更新）
  ```bash
  "D:/daili/scripts/SSH-US-Via-154.cmd" "docker restart aivideotrans-app"
  ```

- [ ] **Step 4: rebuild next + up -d**
  ```bash
  "D:/daili/scripts/SSH-US-Via-154.cmd" \
    "cd /opt/aivideotrans/app && docker compose --env-file /opt/aivideotrans/config/.env build next && docker compose --env-file /opt/aivideotrans/config/.env up -d next"
  ```

- [ ] **Step 5: 健康检查**
  ```bash
  "D:/daili/scripts/SSH-US-Via-154.cmd" \
    "docker ps --format 'table {{.Names}}\t{{.Status}}' && curl -sf http://localhost:8880/gateway/health"
  ```
  Expected: 5 容器 healthy + gateway ok

- [ ] **Step 6: 验证镜像里新代码**
  ```bash
  "D:/daili/scripts/SSH-US-Via-154.cmd" \
    "docker run --rm aivideotrans-next:latest sh -c 'grep -rohE \"serviceMode\" /app/.next 2>/dev/null | head -3'"
  ```
  Expected: 有匹配

---

### Task 11: 手工验收

- [ ] **Step 1: Studio job → 检查 UI 全量**
  - 登录前端，新建 Studio 任务等跑完
  - ResultMediaCard 显示：视频播放器、"配音视频"下载、"配音音频"下载、"素材包"按钮
  - 项目详情页 `ResultDownloadList` 显示全部下载项（`publish.dubbed_video` / `editor.dubbed_audio_complete` / `editor.subtitles*` / `editor.tts_segments_zip` / `manifest.file` 等）
  - **workspace 页** `ResultDownloadList` 同样显示全量

- [ ] **Step 2: Express job → 检查 UI 精简**
  - 新建 Express 任务等跑完
  - ResultMediaCard 显示：视频播放器 + "配音视频"下载
  - **不**显示"配音音频"按钮、**不**显示"素材包"按钮
  - 项目详情页 `ResultDownloadList` 只剩一项 "配音视频"（`publish.dubbed_video`）
  - **workspace 页** `ResultDownloadList` 同样只剩 "配音视频"一项（v3 新加验收点）

- [ ] **Step 3: 直连 URL 偷下载（安全验证）**
  - 用 Express job 的 ID 直接访问：
    - `GET /job-api/jobs/{id}/download/editor.dubbed_audio_complete` → **403**
    - `GET /job-api/jobs/{id}/download/source.original_video` → **403**（v3 新加）
    - `GET /job-api/jobs/{id}/download/publish.dubbed_video` → **200**
    - `GET /job-api/jobs/{id}/stream/audio` → **403**
    - `GET /job-api/jobs/{id}/stream/video` → **200**
    - `GET /job-api/jobs/{id}/stream/poster` → **200**（或 404 如 poster 不存在）
    - `GET /job-api/jobs/{id}/tts-segments-zip` → **403**
    - `GET /job-api/jobs/{id}/artifacts` → 只含 `publish.dubbed_video` + `publish.dubbed_video_poster`
    - `GET /job-api/jobs/{id}/materials-availability` → 真实值（`dubbed_audio` 可能为 true）

- [ ] **Step 4: 老 Express job 兼容性（CodeX P1 #2 关键验收）**
  - 找一个 HEAD EDITOR-only 时期的 Express job（只有 editor 产物，没 publish.dubbed_video）
  - 打开看 UI：`hasVideo=false && hasAudio=true` → 显示"占位框 + 生成视频"按钮
  - 点"生成视频" → 走 Export Tasks v1 异步渲染 → 完成后视频出现
  - **⚠️ 如果这一步看到"占位框但没按钮"或"什么都没有"，说明 Task 4 的 false-mask 回归契约被破坏，必须回溯检查 `/materials-availability` 返回 `dubbed_audio` 的值**

---

## 产物生命周期（不变）

Express 和 Studio 的 project_dir 磁盘结构完全一致，7 天过期策略不变。Express 的 editor 产物仍在磁盘上（只是 API 不暴露），随项目过期一起删。

---

## 已知限制

- **老 Express job 没视频**：HEAD EDITOR-only 时期的 Express job 需要手动点"生成视频"补救。Task 11 Step 4 专门验证这个兼容性。Task 4 的回归测试是其契约锁。
- **Express 下 `/materials-availability` 会"告诉"前端磁盘有 editor 产物**：这是 v2 设计的 trade-off——维持 fallback 需要 `hasAudio=true` 真实值；下载/展示权限在 UI + `/download` + `/stream` + `/artifacts` 四层拦截，视为可接受的信息披露。
- **Express 用户猜到 URL 尝试绕过**：Task 1/2/3 的 403 和 artifacts 过滤是这个场景的防护。Gateway 层把 `/job-api/jobs/{id}/{subpath}` 代理到 Job API，enforcement 放 Job API 是正确位置（CodeX 评审已确认无 bypass 路径）。
- **Studio 用户想退到 Express 视图**：不支持。有需求只能新建 Express 任务。

---

## 未来演进（不在本方案）

- **Plan 分层**：free 用户可能强制 Express，付费才能用 Studio。plan_catalog 层的事，不在本方案。
- **Express 视频质量档**：未来独立方案。
- **`output_target` 字段清理**：本方案让这个字段彻底 legacy，未来可以独立 cleanup（DB migration drop column + 代码清理）。
- **`/artifacts` 过滤下沉到 service 层**：目前 handler 层过滤 payload，未来若引入多个 consumer 可以下沉到 `JobService.get_artifacts(service_mode=...)`。

---

## 验证

### 必跑回归

- `tests/test_job_api_express_filter.py`（新增，本方案 Task 1/2/3/4 共 ~12 个用例）
- `tests/test_job_api.py` + `test_job_api_phase1.py` + `test_job_api_phase2.py` — 不动，确保既有 Job API 契约不回归
- `tests/test_gateway_route_coverage.py` — 不动
- `tests/test_publish_backend.py` — 不动
- `tests/test_background_task_queue.py` + `test_background_task_api.py` — 不动，Export Tasks v1 不受影响
- 前端 `npm run build` + `eslint`

### 手工验收（见 Task 11）

- Studio 完整 UI
- Express 精简 UI
- 直连 URL 拒绝（4 个端点 × 403）
- 老 Express job fallback（关键 corner case）

---

## 改动文件清单

| 文件 | 类型 | 规模 |
|---|---|---|
| `src/services/jobs/api.py` | 改 | ~50 行（4 个分支 + 3 个白名单常量） |
| `tests/test_job_api_express_filter.py` | **新增** | ~180 行（~13 个 Express filter + 回归契约测试） |
| `frontend-next/src/components/workspace/ResultMediaCard.tsx` | 改 | ~15 行（新 prop + 2 处条件渲染） |
| `frontend-next/src/components/result-download-list.tsx` | 改（**v3 新增**） | ~10 行（新 prop + 内部 filter） |
| `frontend-next/src/app/(app)/projects/page.tsx` | 改 | ~1 行 |
| `frontend-next/src/app/(app)/projects/[jobId]/page.tsx` | 改 | ~2 行（两处 prop 传递） |
| `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | 改（**v3 新增**） | ~1 行 |
| `frontend-next/src/lib/api/mappers.ts` | 改（如缺） | 0-3 行 |
| `frontend-next/src/types/api.ts` | 改（v2 新增） | 1-3 行 |
| `frontend-next/src/types/jobs.ts` | 查看（v1 已有） | 0 行 |
| `docs/plans/2026-04-18-express-studio-output-filter-plan.md` | 改 | 本文档 |

**后端**：~50 行改动 + ~180 行测试
**前端**：~30 行改动（跨 7 个文件）
**前提**：12 个 video-output baseline WIP 已作为独立 commit 落地
