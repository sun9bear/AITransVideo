# 磁盘止血:R2 主动推产物 + 接入现有 TTL 清理

**日期**:
- 2026-05-07 v1 草稿
- 2026-05-07 v2(吸收 CodeX 一审 + 实际代码调研后重写)
- 2026-05-07 v3(吸收 CodeX 二审 P1+P2 + PG 同步路径调研后重写)
- 2026-05-08 v4(吸收 CodeX 三审 P1+P2 + Gateway 包依赖隔离调研后重写)

**状态**:Stage A 编码完成(2026-05-08),待用户在远程主机部署 + 灰度。Stage B 待 Stage A 稳定 1-2 周后启动
**上游方案**:[2026-04-21-cloudflare-r2-deployment-plan.md](./2026-04-21-cloudflare-r2-deployment-plan.md)
**上游基线**:[2026-04-23-phase2-r2-download-minimal.md](./2026-04-23-phase2-r2-download-minimal.md)
**作者**:sun9bear(Claude Opus 4.7 协助起草与修订)

---

## v4 修订摘要(2026-05-08)

CodeX 三审在 v3 基础上指出 6 处仍未闭合的问题(4 P1 + 2 P2)。v4 全部吸收:

| # | v3 错误 / 缺漏 | 调研 / 修复 |
|---|---|---|
| **P1.1** | sweeper 反向同步 PG 时不调 quota settle 且字段不全 → reserved quota 永久卡住 + publisher 拿不到 project_dir | 调研 [`gateway/quota.py:131`](../../gateway/quota.py) `settle_job_quota` **已幂等**(`quota_state not in ("none","reserved")` 直接 return)。**修复**:抽 helper `_mirror_job_terminal_state(db, job, upstream_record)` 同步 status/project_dir/completed_at/current_stage 并幂等调用 settle_job_quota;`intercept_list_jobs` 与 sweeper 共用 |
| **P1.2** | Gateway 直接 `from services.jobs.store import JobStore` 触发 pydub 等 Job API 重依赖;且 `runtime_wiring.build_default_store` 是我编造的不存在 | CLAUDE.md 已警告(see [feedback_docker_logs_ephemeral 同段](../../CLAUDE.md))。**修复**:① 共享常量 / publisher / parity 改放 `src/services/r2_publisher_lib/`(平级 jobs 包,不进 services.jobs)以避免 `__init__.py` 污染;② Gateway 自写 [`gateway/storage/job_store_reader.py`](../../gateway/storage/job_store_reader.py),纯 stdlib glob 读 `jobs/*.json`,绕过 JobStore class |
| **P1.3** | edit_generation>0 时 registry miss 走 lazy 兜底会签到旧 g0 R2 对象 | **修复**:lazy 兜底**仅在 `job.edit_generation == 0`** 才允许;edit_generation>0 + registry miss → 直接返 None,Job API 走 byte-passthrough |
| **P1.4** | Stage B parity 守门只在 Gateway 侧;`src/services/web_ui/cleanup.py` 是同步代码,无 AsyncSession,无法调 async parity → Job API cleanup 仍能绕过守门 rmtree | **修复**:Stage B 启用 `AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=true` 时,Job API cleanup.py **完全停止 rmtree project_dir**,只翻 JSON status / 删 `jobs/<id>.json`(数据库行管理)。Gateway 是 project_dir 的**唯一删除入口**。原本"双 cleaner 同步行为"改为"单 cleaner 删盘 + 双 cleaner 同步状态" |
| **P2.1** | manifest 缺失时 `load_manifest_artifact_index` 返空 → 每个 key 记 `skipped_missing` → parity 视为 OK → 误删本地数据 | **修复**:publisher 入口先校验 manifest 存在 + index 非空,否则**整批 entries 状态记 `failed`**(error="manifest missing/empty");`skipped_missing` 仅在 manifest 有效但**单个 key** 在 index 中找不到 / 本地文件不存在时使用 |
| **P2.2** | jianying_draft_zip 完全排除会让 7d cleanup 后用户失去已生成的核心交付物(项目无法重建) | **修复**:已生成 jianying(`JobRecord.jianying_draft_zip_path` 不为空)**纳入** EAGER_PUSH;Gateway PG 没这个字段 → JSON store reader 读出来给 sweeper / publisher;sweeper 候选谓词扩展为 "registry NULL OR (jianying_path 不空但 registry 缺 jianying entry)";Stage B parity 同步检查 |

**总工时变化**:v3 ~5.5d → v4 **~6.5d**(+0.5d 抽 mirror helper + Gateway JSON reader + +0.3d jianying 条件 + +0.2d Job API cleanup 收口)

**两阶段拆分**:
- **Stage A(~5.0d)**:per-artifact registry + 主动推(从 JSON store 扫,不 import services.jobs)+ 共享 mirror helper + manifest 缺失硬失败 + edit_generation>0 lazy 禁用 + jianying 条件推送 + 下载链路改 registry。**不动 cleanup 行为**。
- **Stage B(~1.5d)**:Stage A 稳定 1-2 周后,Gateway 接 R2 parity 守门 + Job API cleanup 收口 rmtree 权限。

---

## 1. 背景

### 1.1 现状

- US 主机扩展卷 60% 使用率,`projects/` 单调增长。
- Phase 2 lazy upload 已落地,**仅** `publish.dubbed_video` 一个 key,**仅在用户首次下载时同步 PUT**。
- `gateway/project_cleanup.py` + `src/services/web_ui/cleanup.py` 双侧 7 天 TTL + 6 小时间隔已经在跑。
- **当前生产已在炸**(v2 调研):lazy upload 在用户 7 天后访问时,本地 manifest.json 已删 → manifest 解析失败 → fallback to local → 404。
- **PG.status 同步路径不健壮**(v3 发现):`intercept_list_jobs` 是 PG.status 唯一同步路径,只在用户访问列表时触发 → 任务完成无人访问 → 永远不同步。
- post-edit overwrite `edit_generation += 1` 但 r2_key 无版本号 → 老 R2 对象会被 HEAD 命中。
- **Gateway 容器禁止 import `services.jobs.*`**(v4 调研:CLAUDE.md 已警告 pydub 污染)。

### 1.2 目标

**Stage A 必达**:
1. 每个 succeeded 任务 EAGER_PUSH 集合 artifact 在 R2 上有完整副本,带 edit_generation 版本
2. Gateway 下载链路改读 PG `r2_artifacts` registry,不再依赖本地 manifest.json
3. Gateway 下载层守住 Express allowlist
4. post-edit overwrite 后下载到的是新产物
5. sweeper 不依赖 PG.status,直接从 JSON store 扫
6. Gateway 包依赖隔离:不 import `services.jobs.*`,不污染 pydub 链
7. manifest 缺失等异常 = 整批 failed,parity 拒绝放行,**不能误删用户数据**
8. 已生成的 jianying_draft_zip 也推 R2,7d cleanup 后仍可下载
9. quota settle 幂等,sweeper 反向同步不会卡 reserved
10. 全链路异常退回现有 lazy upload + Job API byte-passthrough

**Stage B 必达**:
11. Gateway parity 守门 + Job API cleanup 停止 rmtree → Gateway 是 project_dir 唯一删除入口
12. 上线后扩展卷使用率长期稳定下降

### 1.3 非目标

- ❌ R2 浏览器直传(Phase 3)
- ❌ public custom domain(v2 撤销)
- ❌ Worker HMAC(备胎专题)
- ❌ `editor.tts_segments_zip` / materials_pack zip 推 R2(动态生成)
- ❌ R2 lifecycle / 自动过期
- ❌ 修改 lazy upload 主路径(继续作为兜底,且 v4 限定 edit_generation==0 才走)
- ❌ 任何前端 diff
- ❌ 修改 cleanup TTL(7 天保持)

---

## 2. 架构目标态

### 2.1 包依赖隔离(v4 关键约束)

```
Gateway 容器                          App 容器
─────────────────────────              ──────────────────────────
gateway/                              src/services/jobs/
  ├─ storage/                          ├─ api.py
  │   ├─ r2_client.py                  ├─ store.py
  │   ├─ backend_router.py             ├─ models.py
  │   ├─ job_store_reader.py [v4 新]    └─ ... (含 pydub 依赖)
  │   └─ event_log.py
  ├─ r2_artifact_sweeper.py [v4 新]    src/services/r2_publisher_lib/
  └─ job_intercept.py                  ├─ __init__.py (空, 不 export jobs.*)
                                       ├─ downloadable_keys.py [v4 新]
import path:                           ├─ r2_publisher.py [v4 新]
  - 平级 services 包: ✓                └─ r2_parity.py [v4 新]
  - services.jobs.*  : ✗ 禁
                                       app 容器读路径:
                                       - 平级 services.r2_publisher_lib.* ✓
                                       - services.jobs.api 等内部继续用
```

`r2_publisher_lib/` 是新建子包,**与** `services.jobs/` **平级**。Gateway 通过现有 `sys.path.insert(0, src/)` 套路 import:
```python
# Gateway 侧
from r2_publisher_lib.downloadable_keys import EXPRESS_ALLOWED_DOWNLOAD_KEYS
from r2_publisher_lib.r2_publisher import publish_artifacts
```
不触发 `services.jobs.__init__.py`,不拉 pydub。

JobStore 不 import,Gateway 自写 `gateway/storage/job_store_reader.py` 直接读 JSON 文件。

### 2.2 sweeper 数据流(v4 改:JSON reader + jianying 条件)

```
                ┌──────────────────────────────────────┐
                │ JSON store (app 容器写, Gateway 读)   │
                │ jobs/<job_id>.json                    │
                │ status, completed_at, project_dir,    │
                │ current_stage, edit_generation,       │
                │ jianying_draft_zip_path  [v4 新读]    │
                └────────────┬─────────────────────────┘
                             │ 共享 bind mount
                             │ /opt/aivideotrans/app/jobs (Gateway RO 读够)
                             ▼
gateway/storage/job_store_reader.py  [v4 新]
    │  iter_succeeded_records() → 纯 stdlib, glob *.json
    │  返 JobJsonRecord(job_id, status, completed_at, project_dir,
    │                    current_stage, edit_generation, jianying_draft_zip_path)
    ▼
gateway/r2_artifact_sweeper.py
    │
    │  1. iter_succeeded_records() 过滤 grace 期外
    │  2. 限流: AVT_R2_SWEEPER_BACKFILL_RATE_PER_MIN
    │  3. 对每个候选:
    │     ├─ _mirror_job_terminal_state(db, job_record):
    │     │    - 共享 helper, 与 intercept_list_jobs 复用
    │     │    - 同步 status / project_dir / completed_at / current_stage
    │     │    - 幂等 settle_job_quota (gateway/quota.py:131 内置 quota_state 守门)
    │     ├─ 候选谓词:
    │     │    a. PG.r2_artifacts IS NULL                      → 推全集
    │     │    b. jianying_draft_zip_path 不空 AND
    │     │       registry 中无 editor.jianying_draft_zip      → 推差量(只 jianying)
    │     │    c. 其他                                         → 跳过
    │     └─ asyncio.create_task(_run_publish(job_id, push_subset))
    │
    ▼
_run_publish (in Gateway event loop)
    │  asyncio.to_thread(publish_artifacts, ...)
    │
    │  publish_artifacts:
    │    1. **manifest 完整性检查 (P2.1)**
    │       - manifest.json 不存在 → 整批 entries failed
    │       - artifact_index 为空 → 整批 entries failed
    │    2. 对 push_subset 中每个 key:
    │       a. resolve_manifest_artifact_path
    │       b. 本地缺 → state="skipped_missing"
    │       c. r2_key = jobs/{id}/g{N}/{key}{suffix}
    │       d. HEAD R2 → state="already_present"
    │       e. 否则 PUT → state="pushed"
    │       f. 异常 → state="failed"
    │
    ▼
PublishResult.entries 整批 UPDATE PG.r2_artifacts
  - 如果是 jianying 差量: 把 jianying entry append 到现有 array, 其他不动
  - 如果是全集: 整体替换
  - all_ok → r2_push_retry_after = NULL
  - 有 failed → r2_push_retry_after = now + 5min
```

### 2.3 下载链路(v4 改:edit_generation>0 不走 lazy)

```
浏览器 GET /job-api/jobs/{id}/download/{key}
    │
    ▼
Gateway _resolve_r2_redirect
    │
    ├─ 1. _verify_job_ownership                              [现有]
    ├─ 2. 读 PG Job: service_mode, edit_generation, r2_artifacts
    ├─ 3. 共享 allowlist (P2.1) → 不通过返 None
    ├─ 4. registry 路径:
    │     找 (artifact_key + edit_generation) 匹配 entry
    │     ├─ state ∈ {pushed, already_present} → presign + 302
    │     ├─ state == "skipped_missing"        → 返 None (Job API 404)
    │     ├─ state == "failed"                 → 走 lazy (条件)
    │     └─ 不命中                             → 走 lazy (条件)
    │
    ├─ 5. lazy 兜底 (v4 收紧, P1.3):
    │     - 仅 artifact_key == "publish.dubbed_video"
    │     - **AND** edit_generation == 0
    │       (g0 之外的 generation 不能签老 r2_key, 否则会拿到旧产物)
    │     满足 → 走老 lazy 路径
    │     不满足 → 返 None, Job API byte-passthrough
    │
    └─ 6. 任何步异常 → fallback to local
```

### 2.4 编辑后一致性(v3 设计保留)

```
overwrite commit:
  Gateway _apply_editing_commit_gateway_side (line 2298) [现有 hook]
    job.r2_artifacts = None
    job.r2_push_retry_after = None
    → sweeper 下一轮以 edit_generation=N+1 重推全集

copy_as_new:  新 job_id 自然 NULL → sweeper 自动处理

jianying 生成:
  app 容器 jianying_draft_runner 写 JSON store
    jianying_draft_zip_path = "/path/to/draft.zip"
    → Gateway sweeper 下一轮从 JSON 读到, 谓词匹配 → 差量推 jianying
```

---

## 3. 任务清单

### Stage A(~5.0d)

| ID | 文件 / 模块 | 改动类型 | 工时 |
|----|-------------|---------|------|
| A1 | `gateway/alembic/versions/0XX_add_r2_artifacts.py` | 新建 migration:`r2_artifacts JSONB NULL` + `r2_push_retry_after TIMESTAMPTZ NULL` + partial index | 0.2d |
| A2 | `gateway/models.py:Job` | 加上述两字段 | 0.1d |
| A3 | `src/services/r2_publisher_lib/__init__.py` **新建空 package** | 包依赖隔离边界 | 0.1d |
| A4 | `src/services/r2_publisher_lib/downloadable_keys.py` **新建** | 三套常量 + EAGER_PUSH + content_type 表 | 0.2d |
| A5 | `src/services/jobs/api.py` | 删本地常量,引用 A4 | 0.1d |
| A6 | `gateway/storage/r2_client.py` | `upload_artifact` / `generate_presigned_download_url` 接 content_type | 0.3d |
| A7 | `gateway/storage/backend_router.py` | `r2_key_for` 加 `edit_generation: int \| None = None` | 0.2d |
| A8 | `src/services/r2_publisher_lib/r2_publisher.py` **新建** | publisher 主逻辑;**P2.1 manifest 校验**;jianying 支持 | 0.7d |
| A9 | `gateway/storage/job_store_reader.py` **新建** | 纯 stdlib JSON store reader,读 status/project_dir/completed_at/current_stage/edit_generation/jianying_draft_zip_path | 0.4d |
| A10 | `gateway/r2_artifact_sweeper.py` **新建** | **300s 周期(用户 2026-05-08 定调)**+ 候选谓词(NULL OR jianying 差量)+ 限流 + 自调度 | 0.6d |
| A11 | `gateway/job_terminal_mirror.py` **新建** | 共享 mirror helper:同步全字段 + 幂等 settle_job_quota | 0.3d |
| A12 | `gateway/job_intercept.py:intercept_list_jobs` (line 595-650) | 现有 mirror 逻辑改为调 A11 helper(行为等价但抽出来) | 0.2d |
| A13 | `gateway/main.py` | startup hook 启 sweeper(双 feature flag) | 0.1d |
| A14 | `gateway/job_intercept.py:_resolve_r2_redirect` | 改读 PG `r2_artifacts`,扩 key 白名单,Express/Studio 双侧守门;**v4 lazy 收紧 edit_generation==0** | 0.7d |
| A15 | `gateway/job_intercept.py:_apply_editing_commit_gateway_side` (line 2298) | overwrite 分支清空 r2_artifacts + r2_push_retry_after | 0.1d |
| A16 | `gateway/storage/event_log.py` + `services/jobs/events.py` | 新事件 `download.redirect.r2_registry`;双侧 SUPPORTED_EVENT_TYPES 同步 | 0.2d |
| A17 | 测试 | 见 §6 测试矩阵 | 0.7d |
| A18 | 部署 + 灰度 + 回滚演练 | 双 feature flag 渐进开 | 0.3d |
| **Stage A 合计** | | | **~5.0d** |

### Stage B(~1.5d,Stage A 稳定 1-2 周后启动)

| ID | 文件 / 模块 | 改动类型 | 工时 |
|----|-------------|---------|------|
| B1 | `src/services/r2_publisher_lib/r2_parity.py` **新建** | `r2_parity_ok` 实现:每个 expected key 必须有 entry 且 state ≠ failed;jianying 条件检查 | 0.3d |
| B2 | `gateway/project_cleanup.py:cleanup_expired_projects` | rmtree 前调 parity;失败时不删盘 + 不翻 status | 0.2d |
| B3 | `src/services/web_ui/cleanup.py` | **P1.4 修复**:`AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=true` 时停 rmtree project_dir,只删 JSON 行 / 翻 status | 0.4d |
| B4 | 测试:`tests/test_cleanup_r2_parity.py` + `tests/test_jobapi_cleanup_delegate.py` | 守门各分支 + delegate 模式行为 | 0.3d |
| B5 | 部署 + dry_run 7 天 + 切真删除 | | 0.3d |
| **B6(2026-05-08 加)** | `gateway/main.py:_periodic_project_cleanup` + `src/services/web_ui/cleanup.py:_cleanup_loop` | cleanup 调度从 6h interval 改成"等到下一个北京时间 3 点(=UTC 19:00)"再跑;两侧同步 | 0.2d |
| **Stage B 合计** | | | **~1.7d** |

---

## 4. Stage A 关键设计

### 4.1 PG schema(A1+A2)

```python
r2_artifacts: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
r2_push_retry_after: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

`r2_artifacts` JSONB 数组,每 entry 形如:

```json
{
  "artifact_key": "editor.subtitles_en",
  "edit_generation": 0,
  "state": "skipped_missing",  // pushed | already_present | skipped_missing | failed
  "r2_key": "...",         // pushed/already_present 时填
  "filename": "...",       // 同上
  "content_type": "...",   // 同上
  "size": 524288000,       // 同上
  "source_mtime_ns": ...,  // 同上
  "error": "...",          // failed 时填
  "pushed_at": "2026-05-08T03:21:00Z"
}
```

partial index:

```python
op.create_index(
    "idx_jobs_r2_push_pending",
    "jobs",
    ["completed_at"],
    postgresql_where=sa.text("r2_artifacts IS NULL"),
)
```

### 4.2 共享常量(A3+A4)放在 r2_publisher_lib(P1.2)

新建 [`src/services/r2_publisher_lib/__init__.py`](../../src/services/r2_publisher_lib/__init__.py)(空):

```python
"""Shared library for R2 artifact publish + parity logic.

This package is intentionally **flat and dependency-light** so the Gateway
container (which does NOT install pydub / ffmpeg / Job-API-only packages)
can import from it without triggering services.jobs.__init__.py.

DO NOT import from services.jobs.* in this package. If you need JSON store
data, the caller passes it in.
"""
```

`src/services/r2_publisher_lib/downloadable_keys.py` — 内容同 v3 §4.2,只是路径变了。Job API 侧 [`src/services/jobs/api.py`](../../src/services/jobs/api.py) 改为:

```python
from r2_publisher_lib.downloadable_keys import (
    EXPRESS_ALLOWED_ARTIFACT_KEYS,
    EXPRESS_ALLOWED_DOWNLOAD_KEYS,
    EXPRESS_ALLOWED_STREAM_KINDS,
)
```

(不写 `services.r2_publisher_lib.*`,因为 src/ 已在 sys.path,导入风格与 `services.manifest_reader` 一致)

回归守卫(`tests/test_legacy_cleanup_guards.py` 加一条):AST 扫 `gateway/**/*.py`,任何 `from services.jobs` import 都 fail。

### 4.3 Gateway JSON store reader(A9,P1.2)

[`gateway/storage/job_store_reader.py`](../../gateway/storage/job_store_reader.py)(新建):

```python
"""Read JSON store records from Gateway side without importing
services.jobs.* (which pulls pydub / heavy Job API deps; see CLAUDE.md
'Gateway 业务模块不得 import services.jobs.events' 同精神).

Pure stdlib. Yields lightweight records with only the fields sweeper /
parity need. Schema mirrors src/services/jobs/models.JobRecord but is
intentionally divergent to keep Gateway-side reads independent of any
JobRecord shape changes.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_JOBS_DIR_ENV = "AIVIDEOTRANS_JOBS_DIR"
_DEFAULT_JOBS_DIR = "/opt/aivideotrans/app/jobs"


@dataclass(frozen=True)
class JobJsonRecord:
    job_id: str
    status: str
    completed_at: datetime | None
    project_dir: str | None
    current_stage: str | None
    edit_generation: int
    jianying_draft_zip_path: str | None
    service_mode: str | None  # 备查;Gateway PG 也有, 双源不一致时 PG 优先

    @property
    def is_succeeded(self) -> bool:
        return self.status == "succeeded"


def _jobs_dir() -> Path:
    return Path(os.environ.get(_JOBS_DIR_ENV, _DEFAULT_JOBS_DIR))


def _parse_completed_at(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _coerce_int(raw: object, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def iter_records() -> Iterator[JobJsonRecord]:
    """Iterate all JobJsonRecord in the jobs dir. Order undefined."""
    jobs_dir = _jobs_dir()
    if not jobs_dir.is_dir():
        return
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("job_store_reader: skip %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        yield JobJsonRecord(
            job_id=str(data.get("job_id") or path.stem),
            status=str(data.get("status") or ""),
            completed_at=_parse_completed_at(data.get("completed_at")),
            project_dir=data.get("project_dir") if isinstance(data.get("project_dir"), str) else None,
            current_stage=data.get("current_stage") if isinstance(data.get("current_stage"), str) else None,
            edit_generation=_coerce_int(data.get("edit_generation"), 0),
            jianying_draft_zip_path=(
                data.get("jianying_draft_zip_path")
                if isinstance(data.get("jianying_draft_zip_path"), str)
                else None
            ),
            service_mode=(
                data.get("service_mode")
                if isinstance(data.get("service_mode"), str)
                else None
            ),
        )


def iter_succeeded_in_grace(now: datetime, grace_s: int = 30) -> Iterator[JobJsonRecord]:
    """Yield succeeded records whose completed_at is older than now - grace_s."""
    cutoff = now.timestamp() - grace_s
    for rec in iter_records():
        if not rec.is_succeeded or rec.completed_at is None:
            continue
        if rec.completed_at.timestamp() >= cutoff:
            continue
        yield rec
```

**关键设计**:
- 纯 stdlib;不 import `services.jobs.*`;不 import `boto3`;不 import `fastapi`
- 数据 dataclass 是 Gateway-local 形态,与 `JobRecord` schema 解耦
- 容错:JSON 损坏跳过 + 日志,不拖垮 sweeper

### 4.4 publisher(A8)— v4 加 manifest 校验 + jianying

[`src/services/r2_publisher_lib/r2_publisher.py`](../../src/services/r2_publisher_lib/r2_publisher.py):

```python
"""主动把 succeeded 任务的 EAGER_PUSH artifact 推到 R2.

v4 P2.1 修复: manifest 缺失/异常时不再误判 skipped_missing, 整批 failed.
v4 P2.2: jianying 在 zip 已生成时纳入推送 (caller 通过 push_keys 指定).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from r2_publisher_lib.downloadable_keys import (
    content_type_for,
    eager_push_keys_for,
)

logger = logging.getLogger(__name__)

EntryState = Literal["pushed", "already_present", "skipped_missing", "failed"]


@dataclass
class ArtifactRegistryEntry:
    artifact_key: str
    edit_generation: int
    state: EntryState
    r2_key: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    source_mtime_ns: int | None = None
    error: str | None = None
    pushed_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "artifact_key": self.artifact_key,
            "edit_generation": self.edit_generation,
            "state": self.state,
            "pushed_at": self.pushed_at,
        }
        for f in ("r2_key", "filename", "content_type", "size", "source_mtime_ns", "error"):
            v = getattr(self, f)
            if v is not None:
                d[f] = v
        return d


@dataclass
class PublishResult:
    entries: list[ArtifactRegistryEntry] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return not any(e.state == "failed" for e in self.entries)


def _make_failed_batch(
    keys: Iterable[str], edit_generation: int, error: str, now: str
) -> list[ArtifactRegistryEntry]:
    return [
        ArtifactRegistryEntry(
            artifact_key=k, edit_generation=edit_generation,
            state="failed", error=error, pushed_at=now,
        )
        for k in keys
    ]


def publish_artifacts(
    *,
    job_id: str,
    service_mode: str | None,
    edit_generation: int,
    project_dir: Path,
    base_filename: str,
    has_jianying_draft: bool = False,    # v4: caller (sweeper) 决定
    push_keys: frozenset[str] | None = None,  # v4: caller 决定子集 (差量推 jianying 时只传 jianying)
) -> PublishResult:
    """
    Args:
        push_keys: None = 推全集 (eager + 可选 jianying);
                   非空 = 只推这些 key (差量, 例如刚生成的 jianying)
    """
    # 内部 import 避免污染调用方 (services.manifest_reader 不进 services.jobs 包)
    from services.manifest_reader import (
        load_manifest_artifact_index,
        resolve_manifest_artifact_path,
    )
    from storage import r2_client
    from storage.backend_router import r2_key_for

    result = PublishResult()
    now = datetime.now(timezone.utc).isoformat()

    # 决定要推哪些 key
    eligible: frozenset[str]
    if push_keys is not None:
        eligible = push_keys
    else:
        eligible = eager_push_keys_for(service_mode)
        if has_jianying_draft:
            eligible = eligible | frozenset({"editor.jianying_draft_zip"})

    if not eligible:
        return result

    # ---- P2.1: manifest 完整性硬校验 ----
    manifest_path = project_dir / "manifest.json"
    if not project_dir.is_dir():
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "project_dir missing", now,
        ))
        return result
    if not manifest_path.is_file():
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "manifest.json missing", now,
        ))
        return result
    try:
        artifact_index = load_manifest_artifact_index(project_dir=project_dir)
    except Exception as exc:
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, f"manifest load: {exc}", now,
        ))
        return result
    if not artifact_index:
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "manifest artifact_index empty", now,
        ))
        return result

    # ---- 逐个 key 处理 ----
    for key in sorted(eligible):
        try:
            local_path = resolve_manifest_artifact_path(
                project_dir, key, artifact_index=artifact_index,
            )
        except Exception as exc:
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key, edit_generation=edit_generation,
                state="failed", error=f"resolve: {exc}", pushed_at=now,
            ))
            continue

        if local_path is None or not local_path.exists():
            # P2.1: 这是合法的 skipped_missing — manifest 有效但 key 在 index 内缺
            # (如某些 Studio 任务确实没生成 subtitles_en)
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key, edit_generation=edit_generation,
                state="skipped_missing", pushed_at=now,
            ))
            continue

        ctype = content_type_for(key)
        r2_key = r2_key_for(
            job_id, key,
            local_path=local_path,
            edit_generation=edit_generation,
        )
        filename = _filename_for(key, base_filename, local_path)

        try:
            already = r2_client.head_artifact(r2_key)
            if not already:
                r2_client.upload_artifact(local_path, r2_key, content_type=ctype)
                state: EntryState = "pushed"
            else:
                state = "already_present"
        except Exception as exc:
            logger.warning(
                "publish_artifacts: PUT/HEAD failed job=%s key=%s (%s)",
                job_id, key, exc,
            )
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key, edit_generation=edit_generation,
                state="failed", error=str(exc), pushed_at=now,
            ))
            continue

        try:
            stat = local_path.stat()
        except OSError as exc:
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key, edit_generation=edit_generation,
                state="failed", error=f"stat: {exc}", pushed_at=now,
            ))
            continue

        result.entries.append(ArtifactRegistryEntry(
            artifact_key=key, edit_generation=edit_generation,
            state=state, r2_key=r2_key, filename=filename, content_type=ctype,
            size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
            pushed_at=now,
        ))

    return result


def _filename_for(artifact_key: str, base: str, local_path: Path) -> str:
    name = (base or local_path.stem).strip() or "download"
    if artifact_key == "publish.dubbed_video":
        return f"{name}.mp4"
    if artifact_key == "editor.dubbed_audio_complete":
        return f"{name}.wav"
    if artifact_key == "editor.subtitles":
        return f"{name}_zh.srt"
    if artifact_key == "editor.subtitles_en":
        return f"{name}_en.srt"
    if artifact_key == "editor.subtitles_bilingual":
        return f"{name}_bilingual.srt"
    if artifact_key == "editor.jianying_draft_zip":
        # ⚠️ 2026-05-11 production bug fix (commit a849fea):
        # 不能用 f"{name}_jianying.zip" — 剪映 desktop 把 zip stem 当作
        # internal folder 名找 materials,publisher 自定义后缀会破坏契约。
        # 直接复用 disk basename(jianying_draft_writer 已保证正确格式)。
        return local_path.name
    return f"{name}{local_path.suffix or ''}"
```

> **⚠️ R2 命名是消费契约,不是 publisher 自由发挥(2026-05-11 Day 3 教训)**
>
> `_filename_for` 几个 if 分支在 plan 初稿时凭直觉写成 `{name}_xxx.ext`。jianying 那一行被实战打脸:剪映 desktop app 通过 `draft_content.json` 里的 materials 路径找文件,这个路径用的是 zip 写到磁盘时的 stem(`jianying_draft_writer._resolve_zip_basename`)。Publisher 把下载文件名改成 `{title}_jianying.zip` → 用户解压成 `{title}_jianying/` → 剪映找 `{title}_{date}/materials/*.wav` 失败 → 「媒体丢失」弹窗,产物报废。
>
> **invariant**:R2 publisher 对任何 artifact 的 Save-As 文件名,**优先用 `local_path.name`**(disk 上的实际 basename)。disk 名是由产物生成方(writer / encoder / pipeline)和最终消费方(用户应用、播放器、剪辑软件)共同约定的契约,publisher 不该插手。其他 key 现在还用 `{name}_xx.ext` 是历史遗留,以后任何对消费方有路径敏感性的格式(zip / 文件夹结构 / 模板包)都必须直接用 disk basename。
>
> 与 §4.5 终态结算同源:**R2 publisher 是路过的快递员,不是包裹标签的定义者**。

### 4.5 共享 mirror helper(A11)— P1.1 修复

> **⚠️ 终态结算单一入口约束(2026-05-08 Day 2 教训,新增于 v4.1)**
>
> `mirror_job_terminal_state` 必须**完整**承载"任务进入 terminal"的全部副作用,**不能只做 status 字段同步 + 一个 quota settle**。任何把任务推进到 terminal 状态的入口(`intercept_list_jobs` / R2 sweeper / 未来的后台补偿任务 / Stage B cleanup parity)都必须**经过这个 helper**,不能各自实现一套结算逻辑。
>
> 第一次部署时只接了 `settle_job_quota`,导致 R2 sweeper 把任务推进到 succeeded 时漏了 `settle_job_credit_ledger` — 任务只留下创建时的 `job_reserve` 没有 capture/release,成本页严重少算用户扣点。修复见 `gateway/job_terminal_mirror.py` 现行版本。
>
> **未来添加 terminal side effect 的硬约束**:
> 1. 必须加进 `mirror_job_terminal_state` 内,不开第二条路径
> 2. 必须**幂等**(用 reason_code + 已结算检查)
> 3. 必须**容错**(side effect 失败不阻塞 status mirror;warning 而非 raise)
> 4. **R2 sweeper / parity 守门只发现事实,不重新定义结算口径** — 价格 / 配额 / 计点全部以 Gateway runtime pricing 为真源
>
> 当前 helper 已包含的 terminal side effects(顺序执行):
> - `settle_job_quota`(legacy 配额)
> - `settle_job_credit_ledger`(credits 账本,2026-05-08 Day 2 补)
>
> Stage B 启用 cleanup parity 守门时,**不要在 cleanup 路径里再调 settle** — cleanup 是状态消费方而非状态生产方,应该假设 mirror 已经把账算完了。

[`gateway/job_terminal_mirror.py`](../../gateway/job_terminal_mirror.py)(新建):

```python
"""共享 helper: 把 JSON store 的任务终态镜像到 Gateway PG.

v4 P1.1 修复: 抽出公共逻辑, sweeper 与 intercept_list_jobs 共用.
关键 invariants:
  1. 同步全字段: status, project_dir, completed_at, current_stage
  2. 终态过渡幂等 settle_job_quota (gateway/quota.py:131 自带 quota_state 守门)
  3. 不复活 purged 任务 (current Gateway authority)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from quota import TERMINAL_STATUSES, settle_job_quota

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Job
    from storage.job_store_reader import JobJsonRecord

logger = logging.getLogger(__name__)


async def mirror_job_terminal_state(
    db: "AsyncSession",
    db_job: "Job",
    upstream: "JobJsonRecord",
) -> bool:
    """Sync Gateway PG row from JSON store record.

    Returns True if any field was updated. Caller commits the session.

    NOTE: This intentionally writes only mirror fields (status / project_dir /
    completed_at / current_stage) + triggers quota settle on terminal entry.
    It does NOT touch r2_artifacts, display_name, expires_at, or other
    Gateway-owned fields.
    """
    if db_job.status == "purged":
        # Gateway cleanup is authoritative. Don't resurrect.
        return False

    changed = False
    old_status = db_job.status
    upstream_status = upstream.status

    if upstream_status and upstream_status != old_status:
        db_job.status = upstream_status
        changed = True
    if upstream.current_stage and upstream.current_stage != db_job.current_stage:
        db_job.current_stage = upstream.current_stage
        changed = True
    if upstream.project_dir and upstream.project_dir != db_job.project_dir:
        db_job.project_dir = upstream.project_dir
        changed = True
    if upstream.completed_at and upstream.completed_at != db_job.completed_at:
        db_job.completed_at = upstream.completed_at
        changed = True

    # 幂等 quota settle (quota.py:131 守门 quota_state ∈ {none, reserved})
    if upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES:
        try:
            await settle_job_quota(db, db_job, upstream_status)
        except Exception as exc:
            logger.warning(
                "mirror: settle_job_quota failed job=%s (%s); continue without settle",
                db_job.job_id, exc,
            )

    return changed
```

[`gateway/job_intercept.py:intercept_list_jobs`](../../gateway/job_intercept.py:537) 现有 mirror 段(595-650 行)改为调这个 helper(行为等价,目的是让 sweeper 复用同一份)。

### 4.6 sweeper 实现(A10)— v4 改

```python
"""扫 JSON store, 反向同步 PG, 触发 R2 推送.

v4 改动:
  - JSON 数据源走 gateway/storage/job_store_reader.py (不 import services.jobs.*)
  - 反向同步走 gateway/job_terminal_mirror.py (与 intercept_list_jobs 共用)
  - 候选谓词扩展: NULL OR jianying 差量
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

# Make src/ importable for r2_publisher_lib (NO services.jobs.* imports!)
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from database import async_session
from models import Job
from storage.job_store_reader import iter_succeeded_in_grace, JobJsonRecord
from job_terminal_mirror import mirror_job_terminal_state

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = int(os.environ.get("AVT_R2_SWEEPER_INTERVAL_S", "300"))  # v4 final: 5min default
SWEEP_BATCH_SIZE = int(os.environ.get("AVT_R2_SWEEPER_BATCH_SIZE", "5"))
BACKFILL_RATE_PER_MIN = int(os.environ.get("AVT_R2_SWEEPER_BACKFILL_RATE_PER_MIN", "2"))


def _is_enabled() -> bool:
    return (
        os.environ.get("AVT_DOWNLOAD_REDIRECT_BACKEND", "local") == "r2"
        and os.environ.get("AVT_R2_PROACTIVE_PUSH_ENABLED", "false").lower() == "true"
    )


def _registry_has_jianying(registry: list[dict] | None, edit_generation: int) -> bool:
    if not registry:
        return False
    for item in registry:
        if (
            item.get("artifact_key") == "editor.jianying_draft_zip"
            and item.get("edit_generation") == edit_generation
        ):
            return True
    return False


def _classify_candidate(
    json_rec: JobJsonRecord, db_job: Job
) -> tuple[bool, frozenset[str] | None]:
    """Return (should_publish, push_keys).
    push_keys=None means push full eager set (+ jianying if has_jianying_draft).
    push_keys=frozenset(...) means push only this subset (delta).
    """
    if db_job.r2_artifacts is None:
        # Never pushed → full set
        return True, None

    # Jianying delta scenario: registry exists but missing jianying entry
    expected_gen = db_job.edit_generation or 0
    if (
        json_rec.jianying_draft_zip_path
        and not _registry_has_jianying(db_job.r2_artifacts, expected_gen)
    ):
        return True, frozenset({"editor.jianying_draft_zip"})

    return False, None


async def sweep_once(now: datetime | None = None) -> int:
    if not _is_enabled():
        return 0
    now = now or datetime.now(timezone.utc)

    # 限流
    rate_limit_per_pass = max(1, BACKFILL_RATE_PER_MIN * SWEEP_INTERVAL_S // 60)
    batch_cap = min(SWEEP_BATCH_SIZE, rate_limit_per_pass)

    enqueued = 0
    for json_rec in iter_succeeded_in_grace(now):
        if enqueued >= batch_cap:
            break
        try:
            async with async_session() as db:
                result = await db.execute(select(Job).where(Job.job_id == json_rec.job_id))
                db_job = result.scalar_one_or_none()
                if db_job is None:
                    # Gateway 没收到 create 事件 — 跳过
                    continue
                # 反向同步 PG (P1.1: 全字段 + 幂等 quota settle)
                await mirror_job_terminal_state(db, db_job, json_rec)

                # retry 退避
                if db_job.r2_push_retry_after and db_job.r2_push_retry_after > now:
                    await db.commit()
                    continue

                should, push_keys = _classify_candidate(json_rec, db_job)
                if not should:
                    await db.commit()
                    continue

                # 占位 retry_after 防本轮 task 超时被下轮重入
                db_job.r2_push_retry_after = now + timedelta(minutes=5)
                await db.commit()

            asyncio.create_task(
                _run_publish(json_rec.job_id, push_keys, json_rec.jianying_draft_zip_path is not None),
                name=f"r2-publish-{json_rec.job_id}",
            )
            enqueued += 1
        except Exception:
            logger.exception("sweeper iteration failed job=%s", json_rec.job_id)

    return enqueued


async def _run_publish(
    job_id: str,
    push_keys: frozenset[str] | None,
    has_jianying: bool,
) -> None:
    from r2_publisher_lib.r2_publisher import publish_artifacts

    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None or not job.project_dir:
            return
        service_mode = job.service_mode
        edit_generation = job.edit_generation or 0
        base_filename = (
            getattr(job, "display_name", None) or getattr(job, "title", None) or job_id
        )
        project_dir = Path(job.project_dir)
        existing_registry: list[dict] | None = job.r2_artifacts

    pub = await asyncio.to_thread(
        publish_artifacts,
        job_id=job_id,
        service_mode=service_mode,
        edit_generation=edit_generation,
        project_dir=project_dir,
        base_filename=base_filename,
        has_jianying_draft=has_jianying,
        push_keys=push_keys,
    )

    # 决定怎么写回 PG
    new_entries: list[dict]
    if push_keys is None:
        # 全集推送 → 整体替换
        new_entries = [e.to_dict() for e in pub.entries]
    else:
        # 差量推送 (jianying) → merge: 保留旧 entries 中不在 push_keys 的, append 新的
        old = existing_registry or []
        new_entries = [
            e for e in old
            if e.get("artifact_key") not in push_keys
            or e.get("edit_generation") != (existing_registry[0].get("edit_generation") if existing_registry else 0)
        ]
        new_entries.extend(e.to_dict() for e in pub.entries)

    async with async_session() as db:
        await db.execute(
            update(Job).where(Job.job_id == job_id).values(
                r2_artifacts=new_entries,
                r2_push_retry_after=(
                    None if pub.all_ok
                    else datetime.now(timezone.utc) + timedelta(minutes=5)
                ),
            )
        )
        await db.commit()


async def sweeper_loop():
    logger.info(
        "r2_artifact_sweeper started (interval=%ss, batch=%d, rate=%d/min)",
        SWEEP_INTERVAL_S, SWEEP_BATCH_SIZE, BACKFILL_RATE_PER_MIN,
    )
    while True:
        try:
            n = await sweep_once()
            if n > 0:
                logger.info("sweeper enqueued %d publish tasks", n)
        except Exception:
            logger.exception("sweeper loop crashed; retrying")
        await asyncio.sleep(SWEEP_INTERVAL_S)
```

### 4.7 下载链路改造(A14)— v4 lazy 收紧

```python
async def _resolve_r2_redirect(
    db: AsyncSession,
    job_id: str,
    *,
    artifact_key: str,
) -> str | None:
    from r2_publisher_lib.downloadable_keys import download_keys_for

    if not is_r2_enabled():
        return None

    job = (await db.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
    if job is None or not job.project_dir:
        return None

    if artifact_key not in download_keys_for(job.service_mode):
        return None  # 让 Job API 兜 403

    expected_gen = job.edit_generation or 0
    registry: list[dict] | None = job.r2_artifacts
    entry = None
    if registry:
        for item in registry:
            if (
                item.get("artifact_key") == artifact_key
                and item.get("edit_generation") == expected_gen
            ):
                entry = item
                break

    if entry is not None:
        state = entry.get("state")
        if state in ("pushed", "already_present"):
            try:
                from storage import r2_client
                url = r2_client.generate_presigned_download_url(
                    entry["r2_key"],
                    entry.get("filename", artifact_key),
                    content_type=entry.get("content_type", "application/octet-stream"),
                )
                _emit_download_event(
                    job_id=job_id,
                    event_type="download.redirect.r2_registry",
                    artifact_key=artifact_key,
                )
                return url
            except Exception as exc:
                logger.warning("r2 registry presign failed (%s); fallback", exc)
                return None
        # state ∈ {skipped_missing, failed} → 进 lazy 判定

    # ---- v4 P1.3: lazy 兜底收紧 ----
    if artifact_key != "publish.dubbed_video":
        return None
    if expected_gen != 0:
        # 老 lazy r2_key 不带 generation → 命中老对象返旧产物, 不安全
        logger.info(
            "r2 lazy refused: job=%s edit_generation=%d > 0", job_id, expected_gen,
        )
        return None
    return await _legacy_lazy_resolve_publish_dubbed_video(db, job_id, job)
```

### 4.8 灰度

```
Day 0: 部署所有 Stage A 代码 (A1-A18 一起上线)
       env: AVT_R2_PROACTIVE_PUSH_ENABLED 不设 (默认 "false")
       → sweeper loop 启动后立即返回 0, 行为等同现状
Day 0+: 跑 alembic upgrade head 落 migration 025
Day 1: 跑生产 smoke
       - 浏览器下载老任务 final_video → 走 lazy 兜底 (老 R2 形状)
       - 跑一个新 Studio 任务到 succeeded → r2_artifacts 仍 NULL (flag 关)
       - 跑一个新 Express 任务到 succeeded → 同上
       - editing/commit overwrite → 旧 r2_artifacts 仍保留 (本就是 NULL)
Day 2: 开 AVT_R2_PROACTIVE_PUSH_ENABLED=true (BACKFILL_RATE_PER_MIN=2 默认)
       21 存量 succeeded 任务以 2/min 速率消化, 含已生成 jianying 的任务也会推
       sweeper 启动日志可见 "r2_artifact_sweeper started ..."
Day 3-7: 跟踪 R2 写次数 / failed 比例 / 武汉移动用户体感
       - 浏览器下载新 succeeded 任务 → 302 走 R2 origin
       - PG: SELECT job_id, jsonb_array_length(r2_artifacts) FROM jobs
            WHERE r2_artifacts IS NOT NULL ORDER BY completed_at DESC LIMIT 5
Day 7: Stage A 验收;评估 Stage B 启动时机
```

### 4.8a 部署具体动作(Day 0)— migration-first 顺序(P1-C 修复)

**关键约束**:`gateway/models.py:Job` 已声明 `r2_artifacts` / `r2_push_retry_after` 字段。任何 `select(Job)` 都会展开 SELECT 列含这两列。**如果 restart 把新代码切上来时 schema 还没 migrate,Gateway 第一个 SELECT Job 就 ProgrammingError(column does not exist)**。所以顺序必须是 cp → migrate → restart,而不是 restart → migrate。

```bash
# === 阶段 1: 上传代码,但 NOT restart ===
# 通过 D:\daili\scripts\Upload-Via-154.cmd 把 15 个文件 docker cp 到容器内对应路径。
# 此时容器内**旧 Python 进程仍在跑**(它没 reload 新代码),但磁盘上的
# alembic/versions/025_add_r2_artifacts.py 已就绪。

# === 阶段 2: 用新 alembic 文件跑 migration ===
# 旧 Python 进程不会读 alembic/versions/ 目录,alembic CLI 是新进程,
# 它启动时 scan versions/ 目录,会发现 025 并应用。
docker exec aivideotrans-gateway alembic upgrade head
# 期望输出: Running upgrade 024_announcement_popup -> 025_add_r2_artifacts

# 验证 schema 已落地
docker exec aivideotrans-postgres psql -U postgres -d aivideotrans \
  -c "\d jobs" | grep -E "r2_artifacts|r2_push_retry_after"
# 期望两行存在

# === 阶段 3: 此刻才 restart,让新代码接流量 ===
docker restart aivideotrans-gateway aivideotrans-app

# === 阶段 4: 启动日志验证 ===
docker logs --tail 50 aivideotrans-gateway 2>&1 | grep -i sweeper
# 期望: "r2_artifact_sweeper started (interval=300s, batch=5, rate=2/min)"
# (sweeper loop 启动但因 AVT_R2_PROACTIVE_PUSH_ENABLED 默认 false, 立即 return 0)

# === 阶段 5: 烟测 ===
# 现有用户访问列表页 / 详情页 / 老任务下载, 全部不应受影响。
```

**回滚阶段 1+2 后**(只 cp + migrate,未 restart):新 alembic 已加列,但旧 Python 进程不读这两列(没 import)。功能等价于现状。如阶段 3 restart 后发现问题,Migration 不需要 downgrade,只需 docker cp 旧代码 + restart 即可,旧代码忽略新列。

### 4.8b 灰度开关 Day 2

```bash
# /opt/aivideotrans/config/.env 加:
#   AVT_R2_PROACTIVE_PUSH_ENABLED=true

# 不重 build, 仅 force-recreate 让 env_file 重读
# (per feedback_tls_internal_trap.md: docker compose restart 不重读 env_file)
docker compose --env-file /opt/aivideotrans/config/.env up -d --force-recreate gateway

# 验证开关生效 (Day 2 +5min 内期望见首次 enqueue)
docker logs --tail 20 aivideotrans-gateway 2>&1 | grep -i sweeper
```

### 4.9 回滚

| 级别 | 触发 | 动作 |
|------|------|------|
| L1 | sweeper 行为异常 | `AVT_R2_PROACTIVE_PUSH_ENABLED=false` + restart gateway |
| L2 | 下载 registry 路径 bug | hot-patch:`_resolve_r2_redirect` 强制 entry=None → 全回 lazy(仅 g0)/ Job API |
| L3 | R2 不可用 | `AVT_DOWNLOAD_REDIRECT_BACKEND=local` |
| L4 | mirror helper 出错(quota 重复 / 字段错乱) | hot-patch 关 sweeper 内 mirror 调用,只走 list 路径同步;紧急情况 alembic downgrade -1 |
| L5 | Migration 出问题 | hot-patch 代码不读 r2_artifacts → `alembic downgrade -1` |

---

## 5. Stage B 设计

### 5.1 r2_parity_ok(B1)— v4 加 jianying 条件

[`src/services/r2_publisher_lib/r2_parity.py`](../../src/services/r2_publisher_lib/r2_parity.py):

```python
"""Stage B cleanup 守门 — v4 加 jianying 条件检查.

判据 (严格):
1. 对 service_mode 对应的 EAGER_PUSH_TO_R2_KEYS 全集 + (若 has_jianying 则加 jianying)
2. 每个 expected key 在 PG.r2_artifacts 中必须有 entry, edit_generation 匹配当前
3. entry.state ∈ {pushed, already_present, skipped_missing} (failed 不算)
4. state ∈ {pushed, already_present} 的 entry 还要 R2 HEAD 双重确认

Caller 提供 has_jianying_draft (从 JSON store reader 读, 不依赖 Gateway PG).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def r2_parity_ok(
    db, job_id: str, *, has_jianying_draft: bool
) -> bool:
    from sqlalchemy import select
    from models import Job
    from r2_publisher_lib.downloadable_keys import eager_push_keys_for

    try:
        job = (await db.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
    except Exception as exc:
        logger.warning("r2_parity: job lookup failed (%s); refuse", exc)
        return False
    if job is None:
        return False

    expected = set(eager_push_keys_for(job.service_mode))
    if has_jianying_draft:
        expected.add("editor.jianying_draft_zip")
    if not expected:
        return False

    registry: list[dict] | None = job.r2_artifacts
    if not registry:
        return False

    expected_gen = job.edit_generation or 0
    entries_by_key: dict[str, dict] = {}
    for item in registry:
        if item.get("edit_generation") != expected_gen:
            continue
        k = item.get("artifact_key")
        if k:
            entries_by_key[k] = item

    # 1. 每个 expected 必须有 entry 且非 failed
    for key in expected:
        entry = entries_by_key.get(key)
        if entry is None:
            logger.info("r2_parity: missing entry for %s in job=%s", key, job_id)
            return False
        state = entry.get("state")
        if state == "failed":
            logger.info("r2_parity: failed state for %s in job=%s", key, job_id)
            return False
        if state not in ("pushed", "already_present", "skipped_missing"):
            logger.info("r2_parity: unknown state %r for %s in job=%s", state, key, job_id)
            return False

    # 2. 实际 HEAD R2 双重确认
    from storage import r2_client
    for key in expected:
        entry = entries_by_key[key]
        if entry.get("state") not in ("pushed", "already_present"):
            continue
        try:
            if not r2_client.head_artifact(entry["r2_key"]):
                logger.warning(
                    "r2_parity: R2 missing %s for job=%s; refuse",
                    entry.get("r2_key"), job_id,
                )
                return False
        except Exception as exc:
            logger.warning("r2_parity: HEAD failed (%s); refuse", exc)
            return False

    return True
```

### 5.2 cleanup 接入(B2+B3)— v4 P1.4 修复

**Gateway 侧** [`gateway/project_cleanup.py`](../../gateway/project_cleanup.py):

```python
import os
from r2_publisher_lib.r2_parity import r2_parity_ok
from storage.job_store_reader import iter_records

REQUIRES_R2_PARITY = os.environ.get(
    "AVT_CLEANUP_REQUIRES_R2_PARITY", "false"
).lower() == "true"

# 现有循环内, _is_safe_project_dir 通过后, rmtree 前:
if _is_safe_project_dir(project_dir, safe_roots=effective_roots):
    if REQUIRES_R2_PARITY:
        # 读 JSON store 拿 jianying 信号
        jianying_present = any(
            rec.job_id == job.job_id and rec.jianying_draft_zip_path
            for rec in iter_records()
        )
        ok = await r2_parity_ok(db, job.job_id, has_jianying_draft=jianying_present)
        if not ok:
            logger.info(
                "project cleanup: skip rmtree+status job=%s (r2 parity not ok)",
                job.job_id,
            )
            continue
    if not dry_run and project_dir.is_dir():
        try:
            shutil.rmtree(project_dir, ignore_errors=False)
        except OSError as exc:
            ...
```

**Job API 侧 P1.4 修复** [`src/services/web_ui/cleanup.py`](../../src/services/web_ui/cleanup.py):

```python
import os

DELEGATE_RMTREE_TO_GATEWAY = os.environ.get(
    "AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY", "false"
).lower() == "true"

# 现有 rmtree 循环改为:
def _maybe_rmtree(project_dir: Path, *, safe: bool) -> bool:
    """Return True if we deleted, False if delegated / refused."""
    if DELEGATE_RMTREE_TO_GATEWAY:
        # Gateway 端 (project_cleanup.py + r2_parity_ok) 是 project_dir 唯一删除入口
        # 这里只删 JSON 存档行 / 翻 status, 把磁盘删交给 Gateway
        return False
    if not safe:
        return False
    try:
        shutil.rmtree(project_dir, ignore_errors=False)
        return True
    except OSError as exc:
        logger.warning("cleanup rmtree failed: %s", exc)
        return False
```

**关键不变量**(Stage B 启用后):
- Gateway `project_cleanup.cleanup_expired_projects` = project_dir 的**唯一** rmtree 调用方
- Job API `cleanup.py` 在 delegate 模式下**只动 JSON 文件**(`jobs/<id>.json` 删除标记)
- 两侧 status flip 都保留(都需要让 UI 看到 purged)

### 5.3 灰度

```
Week 2 Day 0: 部署 B1-B4
       AVT_CLEANUP_REQUIRES_R2_PARITY=false (Gateway 守门关闭)
       AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=false (Job API 仍 rmtree)
       → 行为 100% 等同现状
Week 2 Day 1: 切 AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=true 一阶
       → Job API 不再 rmtree, Gateway 仍按现有 7d 硬删 (parity 关闭)
       → 测试:任务过期后 Gateway 删盘正常
Week 2 Day 3: 切 AVT_CLEANUP_REQUIRES_R2_PARITY=true 二阶
       → Gateway 守门启用, parity 不通过的任务保留
       → dry_run 7 天观察通过率
Week 3: 真删除生效
```

### 5.4 Stage B 回滚

| 级别 | 触发 | 动作 |
|------|------|------|
| BL1 | parity 守门误判积压 | `AVT_CLEANUP_REQUIRES_R2_PARITY=false` → Gateway 退回 7d 硬删 |
| BL2 | Gateway rmtree 行为异常 | `AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=false` → Job API 恢复 rmtree(双 cleaner 共存) |
| BL3 | Stage B 整体下线 | BL1 + BL2 同时关 → 完全等同 v3 之前现状 |

---

## 6. 测试矩阵

### Stage A

`tests/test_r2_publisher.py`:
- 全 key 推 / 部分 R2 已存在 / 本地缺失 / R2 PUT 异常
- service_mode=express → 只推 1 个 key
- has_jianying_draft=True → eligible 含 jianying
- push_keys={jianying} → 只推这一个,其他 key 不出现在结果
- edit_generation=3 → r2_key 含 `/g3/`
- **manifest.json 不存在 → 整批 entries state=failed, error="manifest.json missing"**
- **artifact_index 为空 → 整批 entries state=failed, error="manifest artifact_index empty"**
- manifest 有效但 subtitles_en 在 index 内但本地文件不存在 → 单 entry skipped_missing(不传染)

`tests/test_job_store_reader.py`:
- 正常 JSON → JobJsonRecord 全字段填充
- 缺字段 / 损坏 JSON → 跳过 + 日志,不抛
- jianying_draft_zip_path 字段读到
- iter_succeeded_in_grace 过滤 grace_s

`tests/test_job_terminal_mirror.py`:
- old_status=running, upstream=succeeded → 同步 status + project_dir + completed_at + 调 settle_job_quota
- old_status=succeeded, upstream=succeeded → 不重复 settle(quota.py 内置守门)
- old_status=purged → 不复活
- settle_job_quota 抛异常 → mirror 仍返回 changed=True, 异常被 swallow

`tests/test_r2_sweeper.py`:
- iter_succeeded_in_grace 返 1 任务 → 触发 publish
- 任务 r2_artifacts=NULL → push_keys=None
- 任务 r2_artifacts 有但缺 jianying entry + jianying_draft_zip_path 不空 → push_keys={jianying}
- 任务 r2_artifacts 完整 → 跳过
- BACKFILL_RATE_PER_MIN=2 → 单轮 ≤ 2

`tests/test_phase2_download_backend.py`(扩):
- registry hit (state=pushed) → 302 + content_type 正确
- entry.state=skipped_missing → 返 None,Job API 404
- entry.state=failed + edit_generation=0 + key=publish.dubbed_video → lazy 兜底
- entry.state=failed + edit_generation=2 + key=publish.dubbed_video → **返 None**(P1.3 守门)
- registry miss + edit_generation>0 → 返 None
- 不在 services.jobs 包外的 import 守卫:`tests/test_legacy_cleanup_guards.py` AST 扫 gateway/ 不出现 `from services.jobs`

### Stage B

`tests/test_cleanup_r2_parity.py`:
- 全 expected + jianying 都 OK → True
- has_jianying_draft=True 但 registry 无 jianying entry → False
- 全 expected state=pushed + R2 HEAD 全 OK → True
- 一个 entry state=failed → False
- 一个 entry state=skipped_missing 其他 OK → True
- R2 HEAD 抛异常 → False

`tests/test_jobapi_cleanup_delegate.py`:
- DELEGATE=false → 现有 rmtree 行为
- DELEGATE=true → 不调 rmtree,只删 JSON / 翻 status
- DELEGATE=true 且任务 status flip 后 Gateway parity 通过 → Gateway 端最终删盘

---

## 7. 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `r2_publisher_lib` 子包未来误 import services.jobs | 中 | 拉 pydub 链 | 回归守卫:`tests/test_legacy_cleanup_guards.py` AST 扫 r2_publisher_lib 与 gateway/ 不许出现 `from services.jobs` |
| mirror_job_terminal_state 在并发场景下重复 settle | 低 | 用户额度被多扣 | quota.py:131 内置 `quota_state ∈ {none,reserved}` 守门;两个调用方(intercept_list_jobs / sweeper)走 PG row-level lock 也保证互斥 |
| JSON store reader 高频 glob 扫文件 IO 成本 | 低 | sweeper 慢 | 60s 一拍,jobs/ 稳态 <100 个 *.json,glob 在 ms 级;长期 >1000 时考虑 mtime 排序 + 仅扫最近修改 |
| jianying 差量推送写回 PG 时 race(两路同时改 r2_artifacts) | 低 | 一路覆盖另一路 | sweeper 拉 candidate 时已设 `r2_push_retry_after = now+5min` 占位,5min 内不会重入;另一路是 overwrite commit 清空 r2_artifacts,语义上"清空 > 差量",清空赢也没问题(差量丢了下一轮再推) |
| edit_generation>0 lazy 收紧后,某些老用户的 overwrite 任务下载 publish.dubbed_video 取不到产物 | 低 | 用户 404 | 这正是 P1.3 修复的用意 — 收紧后下游会触发 sweeper 推 g{N} → 用户重试一次即可下载到正确产物;Stage A 上线后 overwrite 任务 60-90s 内完成 R2 推送 |
| Job API cleanup delegate 模式下,若 Gateway 长期不删盘,磁盘不下降 | 中 | 磁盘止血失败 | 监控:扩展卷使用率 1 周下降 ≥ 5%;不达标 BL1+BL2 一键关 |
| 21 存量任务 backfill 中有 manifest 异常的(老格式)→ 整批 failed → 5min 后 retry → 持续刷 | 低 | 日志噪音 | manifest missing 是终态错误,sweeper 持续重试无意义;v4 优化:连续失败 3 次后把 r2_push_retry_after 改为 24h,运维介入查 |

---

## 8. 验收清单

### Stage A

代码 / 集成:
- [ ] Migration 落生产,partial index 存在
- [ ] `r2_publisher_lib` 子包独立,不 import services.jobs
- [ ] AST 守卫:`gateway/**/*.py` 无 `from services.jobs` import
- [ ] Job API api.py 引用共享常量 `EXPRESS_ALLOWED_DOWNLOAD_KEYS is r2_publisher_lib.downloadable_keys.EXPRESS_ALLOWED_DOWNLOAD_KEYS`
- [ ] sweeper 启动日志可见,JobJsonRecord 扫描成本 ms 级
- [ ] 跑一个新 Studio 任务 → 90s 内 PG `r2_artifacts` 5 个 entry
- [ ] 跑一个新 Studio 任务 + 生成 jianying → 第二轮 sweep(60s 后)推 jianying 进 registry
- [ ] **manifest.json 损坏的任务 → 全 entries state=failed,parity 拒绝**(防 P2.1 误删)
- [ ] 浏览器下载 final_video → content_type=video/mp4 + 中文文件名
- [ ] Express 任务请求 `editor.subtitles` → 403
- [ ] 编辑后 commit overwrite → r2_artifacts=NULL → 重推 → 新 entry edit_generation=N+1
- [ ] **edit_generation=2 任务的 publish.dubbed_video registry miss → Job API byte-passthrough(不签老 g0 r2_key)**(P1.3)
- [ ] L1 / L3 / L4 演练全部 OK
- [ ] mirror helper 在 sweeper / intercept_list_jobs 双调用下 quota 不重复 settle

### Stage B

- [ ] r2_parity_ok 单测全绿(7 个分支,含 jianying)
- [ ] DELEGATE=true 模式下 Job API 不再 rmtree,Gateway 唯一删盘
- [ ] dry_run 7 天:守门通过率 ≥ 90%
- [ ] 真删除后 7 天:扩展卷使用率下降 ≥ 5%
- [ ] **0 误删**(parity 失败任务 100% 留存)
- [ ] BL1 / BL2 / BL3 演练 OK

### 用户体验

- [ ] 新任务首次下载者不再等 lazy upload PUT
- [ ] 项目目录被 cleanup 后,EAGER_PUSH 集合 + 已生成 jianying 全部仍可下载
- [ ] post-edit overwrite 后下载到的是新产物
- [ ] manifest 损坏任务 cleanup 后保留磁盘,运维有机会恢复

---

## 9. 开放问题(2026-05-08 用户已拍板,全部按默认)

1. ✅ `tts_segments_zip` / materials_pack zip 不推 R2,7d cleanup 后 404 — 等独立专题
2. ✅ 武汉移动 8.5× 慢 — Stage A+B 落地后观察 1 个月再决定是否启 Worker HMAC 备胎
3. ✅ manifest 异常任务连续失败 3 次后 retry 改 24h(§7 风险表)
4. ✅ Stage B 灰度先 DELEGATE 一天再 PARITY,避免双开关同时影响

**额外共识(2026-05-08 本轮)**:
5. ✅ sweeper 默认间隔 60s → **300s**(白天负担更轻,体感无差异)
6. ✅ cleanup 调度 6h → **凌晨北京 3 点(UTC 19:00)单次执行**(B6)
7. ✅ 不接受"完全批处理只在凌晨推 R2"模式 — 当天下载用户体验不让步

---

## 10. Done Definition

6.5 工日 + 1-2 周 Stage A 灰度 + 1-2 周 Stage B 灰度。结束态:

- 扩展卷使用率长期稳定 < 50%
- R2 上每个 succeeded 任务有完整 EAGER_PUSH + jianying(如已生成)副本
- post-edit overwrite 一致性 100%
- 5 级 + 3 级回滚链路演练过
- 月 R2 成本 ≤ ¥5
- Stage B 守门 0 误删
- Gateway 包依赖隔离守卫 100% 通过 AST 扫描

---

## 11. Stage C — 覆盖剩余下载/stream 链路(2026-05-12 用户决策)

### 11.1 触发动机

Stage A+B 上线运行后,Day 3-4 实测发现:

1. **下载链路 R2 化只覆盖 4 个 `download` key**(`publish.dubbed_video` / `editor.dubbed_audio_complete` / `editor.subtitles{,_en,_bilingual}` + 条件 `editor.jianying_draft_zip`)
2. **`/stream/{kind}` 端点**(video / audio / poster)目前完全走本地 Range stream([src/services/jobs/api.py:447-490](../../src/services/jobs/api.py))
3. **`publish.dubbed_video_poster`** 在 `EXPRESS_ALLOWED_ARTIFACT_KEYS` / `STUDIO_ALLOWED_ARTIFACT_KEYS` 但**不在** EAGER_PUSH 集合 → R2 上没有副本
4. **admin 任务豁免 cleanup**([gateway/project_cleanup.py:121-122](../../gateway/project_cleanup.py)`if role_snapshot == "admin": return False`)→ admin 用户 disk 永久占用,Stage B parity gate 永远碰不到

实测后果(2026-05-12 磁盘 100%):141G 全部一个 admin 用户(`342bbde3-903b-4944-a53c-12a1de0b5ca9`)的 41 个 succeeded 任务。即使 Stage B parity gate 启用也救不了,因为 `_is_expired` 在 admin 豁免分支直接 return False。

### 11.2 用户决策(2026-05-12)

| 决策 | 内容 |
|------|------|
| **D42** | `/stream/{kind}` 接 R2 302 — plan v4 D35 的 "25min 上限" 约束**取消**(原约束是怕长视频签名 30min 过期,但 R2 支持 Range,浏览器从 R2 origin 拉 segment 在签名 TTL 内通常足够;长视频灰度后视情况微调) |
| **D43** | `publish.dubbed_video_poster` 加入 EAGER_PUSH 集合,publisher 推 poster + content_type=image/jpeg |
| **D44** | **admin 豁免维持原状**。141G 救急通过临时扩容 50G 处理。admin 长期备份能力转为 §11.8 follow-up |
| **D45** | Phase 2b CF Custom Domain(plan v4 D39)条件触发判据写入 §11.5,等大陆 stream 体感数据出来后决定 |
| **D46** | **不推中间产物到 R2**(`source/` / `transcription/` / `mfa/` / `tts_segments/` 等)— 维持 plan §1.3 现有非目标。理由:架构变更过大、收益与复杂度不匹配、违反 `feedback_r2_publisher_consumer_contract`(R2 不该重定义 pipeline IO 语义)|
| **D47** | **不推 `materials_pack` zip 到 R2** — 维持现有按需生成模式,Stage A §5.2.4 已经写过 "materials_pack 路径不在 /download/{key},留独立专题" |

### 11.3 任务清单

| ID | 文件 / 模块 | 改动类型 | 工时 |
|----|-------------|---------|------|
| C1 | `src/services/r2_publisher_lib/downloadable_keys.py` | EAGER_PUSH_TO_R2_KEYS_STUDIO 与 EXPRESS 同集合加 `publish.dubbed_video_poster`;`_CONTENT_TYPE_BY_KEY` 已含 `image/jpeg`(无需改) | 0.1d |
| C2 | `src/services/r2_publisher_lib/r2_publisher.py:_filename_for` | poster 文件名规则:`{base}_poster.jpg`(沿用现有 line 170 已有的规则,**确认仍生效**)| 0.0d(已有)|
| C3 | `gateway/job_intercept.py:_resolve_r2_redirect` | 把现有"仅 download key"扩到 stream key 复用同一个 helper。新增第二个 entry point `_resolve_r2_stream_redirect`,接 `kind: Literal["video","audio","poster"]`,内部转 artifact_key(video → `publish.dubbed_video`,audio → `editor.dubbed_audio_complete`,poster → `publish.dubbed_video_poster`),走同一个 registry 查找 + 302 跳转逻辑 | 0.3d |
| C4 | `gateway/job_intercept.py` 主分支 | 现有 `if download_match: ... 302` 段后新增 `if stream_match:` 分支,镜像同样的 R2 / lazy / fallback 三层;Express service_mode 仍然按 `EXPRESS_ALLOWED_STREAM_KINDS = {"video","poster"}` 守门 | 0.3d |
| C5 | `src/services/jobs/api.py:447-490` `/stream/{kind}` | **保持不动作 fallback** — Gateway 拦截 302 走 R2;Gateway 不可达时 Job API 仍能直接服务本地文件(symmetric with Stage A /download fallback 设计)| 0d |
| C6 | `gateway/storage/event_log.py` + `src/services/jobs/events.py` | 加 3 个新事件类型:`stream.redirect.r2_registry` / `stream.fallback.local` / `stream.local.direct`(对齐现有 `download.*` 命名)| 0.1d |
| C7 | `tests/test_phase2_download_backend.py`(扩) | 新增 stream 路径测试:`test_stream_video_r2_redirect_via_registry` / `test_stream_audio_express_forbidden_passthrough` / `test_stream_poster_r2_redirect` / `test_stream_fallback_when_registry_empty` | 0.3d |
| C8 | `tests/test_r2_publisher.py`(扩) | 新增 `test_poster_added_to_eager_push_studio` / `test_poster_pushed_with_image_jpeg_content_type` 守卫 | 0.2d |
| C9 | 部署 + 灰度 | nohup build gateway + force-recreate --no-deps + restart app,沿用 Stage A/B 已建立的安全流程 | 0.3d |
| **Stage C 合计** | | | **~1.6d** |

### 11.4 关键设计

**stream 端点 R2 化的 invariant 复用**(与 plan §4.5 终态结算单一入口同源):

- `_resolve_r2_stream_redirect` 路由职责清晰:只读 `Job.r2_artifacts` registry + 调 `r2_client.generate_presigned_download_url`
- 不重定义 content_type / filename — 复用 registry entry 已存的字段(避免 Stage A 出现过的 jianying naming bug 重演)
- 不调任何 settle helper(parity / cleanup 是状态消费方,plan §4.5 invariant)
- 不引入新的对长视频签名续期机制(浏览器 `<video>` 拿 302 后从 R2 拉,签名 120s 期内 Range 请求复用同一对象元数据;>120s 后 player 自动 re-request 触发新 302。如果实测 >120s 视频出现卡顿,降级 D42 改为 ≤ N 分钟 opt-in 阈值)

**poster 流转**(C1 一行 set 改动后自动生效):

```
现有 publish 阶段 → poster.jpg 写 project_dir/output/
现有 manifest builder 已经把 publish.dubbed_video_poster 写入 artifact_index(否则现有 /stream/poster 本地路径也找不到)
publisher 加 poster 到 EAGER_PUSH → sweeper 推 R2
download/stream 都从 PG.r2_artifacts 找到 entry → 302
```

### 11.5 Phase 2b 条件触发判据

启用 Phase 2b CF Custom Domain(plan v4 D39 / §10.3.1)的判据(D45):

```
Stage C 部署后 1 周内:
  IF (大陆用户 stream 卡顿投诉率 ≥ 5%)
     OR (Uptime Kuma 探针实测武汉移动 R2 stream < 1 MB/s 持续 3+ 天)
  THEN 启 Phase 2b:
       - DNS 加 files.aitrans.video CNAME 到 R2 bucket
       - artifacts bucket 设 public-via-custom-domain
       - Worker 验签(HMAC,plan v4 D39)
       - 改 r2_client.presign_get 路由 `files.aitrans.video` 而非 `<account>.r2.cloudflarestorage.com`
  ELSE 维持现状
```

工作量 ~1.5d,**不在 Stage C 主体范围**,只触发时启动。

### 11.6 灰度顺序

```
Day 0: 部署 C1-C9,Gateway 拿到新 image(stream R2 路由分支默认 follow R2 backend flag)
       验证:flag OFF (`AVT_DOWNLOAD_REDIRECT_BACKEND=local`) → stream 行为完全不变
       生产实际 flag 是 r2(Stage A 切过)→ stream 立即开始走 R2 302
       (无独立 stream feature flag — 复用 download 现有 flag,因为 stream 和 download 是同源对象)
Day 0+5min: 现网新任务 succeeded → sweeper 推 EAGER_PUSH(含新加的 poster)
            老任务的 poster 走 lazy upload 兜底(Stage A 老 r2_key 形状,publish.dubbed_video 用同样路径)
Day 1: 手动验证三种场景
       - 新 Studio 任务:在线播放视频 → Network 面板 302 到 R2
       - 老任务 poster:走 lazy(第一次访问触发 PUT)
       - Express 任务:试图 stream/audio → 仍 403(共享白名单生效)
Day 1-7: 监控指标
       - 武汉移动用户 stream 卡顿率(用户反馈 + Uptime Kuma 探针)
       - R2 PUT 次数(poster 加入后 +1 per task,可承受)
       - source 出站带宽下降比例(stream 不再吃 Gateway 进程带宽)
Day 7: Stage C 验收
       Phase 2b 触发判据评估
Day 14: Phase 2b 触发 OR 不触发,Stage C done
```

### 11.7 非目标(显式)

- ❌ 中间产物推 R2(D46)— 维持 plan §1.3 现状
- ❌ `materials_pack` zip 推 R2(D47)— 留独立专题
- ❌ admin 任务自动 cleanup 改造(用户 D44 决策)— 维持 admin 永久豁免;长期通过 §11.8 备份功能解决
- ❌ stream 端点的 25min 上限(D42 取消)— 不预设,实测决定
- ❌ 改 service_mode allowlist 语义 — `EXPRESS_ALLOWED_STREAM_KINDS` 保持原值

### 11.8 长期 follow-up:admin 一键备份到私有网盘

**触发**:Stage C 落地后,admin 任务的长期 disk 占用问题需要"用户主动归档"能力作为最终方案。

**需求**(用户 2026-05-12 提出):
- admin 可一键把"本账号所有视频任务的数据"打包备份到私有网盘(百度网盘 / OneDrive / Google Drive / 阿里云盘 等)
- 或者**单个视频任务**单独备份
- 备份后 admin 可手动删本地 project_dir 释放磁盘

**可行性分析**(不实施,只评估):

| 维度 | 评估 |
|------|------|
| **百度网盘 API** | 有官方 [OpenAPI](https://pan.baidu.com/union),OAuth2 + 大文件分片上传(单文件最大 4GB,分片 4MB 起)。每用户 token 7d 过期需 refresh。商用授权需"分发"申请(免费上限 200 QPS)|
| **数据规模** | 单任务 1-3GB 中间产物 + ~600MB final products = ~2-4GB;41 个任务 ~100-200GB,首次全备份 4-8 小时(取决于网盘上行) |
| **备份内容** | 已 publish 任务可以"R2 上的最终产物 + 本地中间产物 zip"。或者纯本地 project_dir tar.gz(更完整) |
| **触发方式** | admin UI 加按钮 "备份此任务" / "批量备份";后端起 background_task 类型 `backup_to_pan`,异步打包 + 上传 |
| **认证存储** | 用户 token 加密存 PG `users` 表新字段(`pan_provider` / `pan_refresh_token_encrypted`),开发期可手动跑命令注入 |
| **工作量预估** | OAuth2 + 单文件上传 demo:1d;分片 / 断点续传 / 多任务并发:+1d;UI 按钮 + 进度展示:+1d;**合计 ~3 工日** |
| **风险** | ① 百度网盘审核严格,可能拒"视频内容"分发授权;② 大文件上传不稳定要重试;③ token 过期管理 |

**设计草稿**(等 Stage C 稳定后再细化为正式 plan):

- 后端:`gateway/background_task_executors.py:execute_backup_to_pan` 新增 executor
  - 输入:`{job_ids: [...]}` / `{user_id, scope: "all"|"single"}`
  - 流程:1. 收集 project_dir + R2 副本(可选)→ 2. 打 tar.gz → 3. 分片上传 → 4. 写 PG `backup_records` 表
- 前端:admin 工作台加"备份"按钮,调 `/api/admin/backups`
- Schema:新建 `backup_records` (`id`/`user_id`/`job_id`/`provider`/`remote_path`/`size_bytes`/`status`/`created_at`)
- 鉴权:仅 admin 用户可见,前端 + 后端双侧守门

**何时实施**:Stage C 落地 + 灰度通过后,作为独立 plan `docs/plans/2026-XX-XX-admin-backup-to-pan.md` 启动。当前**不写代码**,只占位记录用户需求。

### 11.9 验收清单

代码 / 集成:
- [ ] C1: `EAGER_PUSH_TO_R2_KEYS_STUDIO` / `EAGER_PUSH_TO_R2_KEYS_EXPRESS` 都含 `publish.dubbed_video_poster`
- [ ] C2: publisher 推 poster 时 content_type=`image/jpeg`,filename=`{base}_poster.jpg`(由现有 `_filename_for` line 170 提供)
- [ ] C3-C4: `gateway/job_intercept.py` 加 `_DOWNLOAD_KEY_RE` 同款 `_STREAM_KIND_RE`,新增 `_resolve_r2_stream_redirect` helper,主分支添加 stream 拦截 + 302 + event emit
- [ ] C5: `/stream/{kind}` 老路径完全不动(fallback 完整保留)
- [ ] C6: `SUPPORTED_EVENT_TYPES` 加 `stream.redirect.r2_registry` / `stream.fallback.local` / `stream.local.direct`;`gateway/storage/event_log.py:_DOWNLOAD_EVENT_TYPES` 同步扩展(改名 `_REDIRECT_EVENT_TYPES`)
- [ ] C7: test_phase2_download_backend.py 新增 4 个 stream 测试全过
- [ ] C8: test_r2_publisher.py 新增 2 个 poster 守卫全过
- [ ] AST 守卫:`from services.jobs` top-level import 仍 0 处(plan §4.2 perimeter 不变)

数据 / 体验:
- [ ] 新任务完成 + sweeper 推完后:在线播放 video → Network 302 → `<account>.r2.cloudflarestorage.com/jobs/{id}/g0/publish.dubbed_video.mp4`
- [ ] poster 缩略图渲染:`<img src="/job-api/.../stream/poster">` → 302 → R2 image/jpeg
- [ ] audio 试听(Studio):同 video
- [ ] Express 任务的 audio 试听:返 403(不受 R2 化影响)
- [ ] 武汉移动用户实测视频卡顿率 < 5%(P95)— 或触发 Phase 2b

回滚:
- [ ] BL1 演练:`AVT_DOWNLOAD_REDIRECT_BACKEND=local` → stream 立即回本地 Range stream
- [ ] BL2 演练:hot-patch `_resolve_r2_stream_redirect` 强制返 None → 仅 stream 回 local;download 保持 R2

### 11.10 回滚

| 级别 | 触发 | 动作 |
|------|------|------|
| BL1 | stream R2 路径整体异常 | `AVT_DOWNLOAD_REDIRECT_BACKEND=local`(与 Stage A 同 flag)→ stream + download 都回 local |
| BL2 | 仅 stream 异常,download 正常 | hot-patch `_resolve_r2_stream_redirect` 强制返 None,download 保持 R2 |
| BL3 | poster 推送异常(content_type / filename) | 改 EAGER_PUSH 移除 poster + `AVT_R2_PROACTIVE_PUSH_ENABLED=false` 一轮 → 老任务 poster 仍走 lazy 不受影响 |
| BL4 | Phase 2b 切完发现 R2 public + HMAC 链路出问题 | `R2_PUBLIC_BASE=`(env 清空)+ restart gateway → presigned URL 回 R2 原生域名 |

---

## 12. 总进度索引(2026-05-13 更新)

| 阶段 | 范围 | 状态 | commit |
|------|------|------|--------|
| Stage A(§4)| R2 publisher + sweeper + registry-based download | ✅ 完成 + 部署 + 灰度通过 | `10b3e68` |
| Day 2 fix | JSONB none_as_null + mirror gen drift | ✅ 完成 + 部署 | `7d17347` / `3ba2988` |
| Day 3 fix | jianying naming = disk basename | ✅ 完成 + 部署 | `a849fea` |
| Stage B(§5)| parity gate + delegate rmtree + cron schedule | ✅ 代码部署 + flag 默认 OFF(灰度未启) | `f3958ca` |
| Stage C(§11) | stream/{video,audio,poster} R2 redirect + poster eager push | ✅ 完成 + 部署 + 灰度中(Day 1+) | `2a1ad9f` |
| Stage C P1/P2/P3 | events 词表容错 / stream 30min TTL + inline / `_STREAM_KIND_RE` 收窄 | ✅ 完成 + 部署(CodeX review 2026-05-12) | `0445222` |
| Stage C naming | `AVT_R2_STREAM_PRESIGNED_EXPIRES_S` 前缀对齐 | ✅ 完成 / 待部署(CodeX review 2026-05-13) | `66f1ee8` |
| Phase 2b(§11.5) | CF Custom Domain + Worker HMAC | ⏳ 灰度数据未到,判据未触发 | — |
| §11.8 admin 备份 | 长期 follow-up,占位草稿 | ⏳ 未实施 | — |
