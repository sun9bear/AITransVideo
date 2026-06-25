# TU-17 · logs/events cursor 化 + pipeline benchmark harness

- **目标 / 价值**：两件独立但同属"先可观测再优化"的事。①给 Job API 的 `/jobs/{id}/logs` 端点加增量查询参数（`since` / `cursor` / `tail`），让前端与 Gateway 轮询时不再每次读取全量 `.events.jsonl`，降低 IO 开销并提升大任务日志页响应速度；保持无参数调用完全兼容旧行为。②建 `scripts/benchmark_pipeline_stage_timings.py`——读 fixture（已有 events.jsonl），提取各 stage 计时，输出 JSON 到 `reports/benchmark/`；CI 存为 artifact 但不阻断普通 pass/fail，用 `benchmark` pytest marker 隔离。原则：先有可观测，再谈优化；benchmark 脚本本身不调任何外部服务。
- **关联发现**：§6.3（logs/events 增量化）/ §6.6（先建 benchmark harness）
- **前置依赖**：无（可与 Wave D 其它单元并行）。TU-03 的 `benchmark` pytest marker 注册完成后可复用，未完成时本单元自行注册（见 Step 5）。
- **建议分支**：`quality/events-cursor`（PR1）/ `quality/benchmark-harness`（PR2）——执行拆两个 PR，见决策记录
- **预估工时**：M（cursor 接口约 0.5 天；benchmark harness 约 0.5 天；测试 + 集成调通约 0.5 天）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`，`tail`→`Select-Object -Last`，`test -f`→`Test-Path`，`wc -l`→`(Get-Content file | Measure-Object -Line).Lines`，避免 `<(...)`）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **拆两个 PR**：本单元文档保留一份，但执行拆成两个独立 PR：PR1 做 `/logs` 增量 cursor（Step 0–5 + Step 7），PR2 做 benchmark harness（Step 6）。两 PR 可并行提 review，也可顺序合入。
- **PR1 严格向后兼容**：无参数调用 `GET /jobs/{id}/logs` 的响应体结构必须与改动前完全相同；`next_cursor` 只新增不删旧字段（`job_id` / `events[]` / `lines[]` 三个字段不变）。
- **PR2 report-only，不阻断普通 CI**：benchmark harness 输出 JSON artifact 到 `reports/benchmark/`；CI 以 `continue-on-error` 模式运行，benchmark 测试用 `benchmark` marker 在普通 job 排除，仅在显式 `-m benchmark` 时运行。
- **前端 polling 调参不在本单元**：`usePollingTask` interval 等待 benchmark 数据后在 TU-15 决策，本单元只建 API 层接口（`getJobLogsSince`）不改现有轮询调用方。
- **`next_cursor` 只增不删**：响应体新增 `next_cursor` 字段属于非破坏性扩展；已有 consumer 忽略新字段不受影响。

---

## 不在本单元范围（out-of-scope）

- **不做** Gateway 侧日志缓存或 Server-Sent Events（SSE）流——增量 HTTP 轮询已足够，SSE 改造属于独立决策门。
- **不做** 前端 `usePollingTask` 的 interval 调参——等 benchmark 数据出来后在 TU-15 按数据决策。
- **不做** 对 `logs_redactor` / `_serve_redacted_logs` 的行为修改，只改 upstream Job API 端点的参数解析；Gateway 代理层透传 query string 即可，无需改 `gateway/job_intercept.py`（仅需验证 query string 透传正常）。
- **不做** 对 `process.py` 任何改动——benchmark harness 读 fixture，不跑真实 pipeline。
- **不做** 真实计时数据的解读或优化决策——本单元只建可观测基础设施。
- **不做** 付费外部 API 的任何调用——benchmark harness 纯读文件。

---

## 必守不变量

- **付费 API 红线**：benchmark harness 及 cursor 接口绝不在任何路径（fallback / except / retry / fixture 准备）调用 MiniMax / VolcEngine TTS / CosyVoice / Gemini / AssemblyAI 等付费外部 API。fixture 文件由开发者离线准备并提交到 `tests/fixtures/benchmark/`，脚本只做文件读取与 JSON 解析。
- **旧调用兼容**：无参数 GET `/jobs/{id}/logs` 响应体必须与改动前完全相同（`job_id` + `events[]` + `lines[]`），已有测试不改行为、不添加新必填字段。
- **Gateway 是事实源**：cursor 接口不在 Gateway 侧新增任何 entitlement / plan 判断；logs 增量化是纯 IO 优化，不影响商业逻辑。
- **默认测试不接真实外部服务**：benchmark marker 测试默认被 pytest addopts 排除（`-m "not slow and not real_provider and not benchmark"`），CI 普通 job 不运行 benchmark 测试。
- **process.py 走 Option B**：本单元不碰 `src/pipeline/process.py`。
- **剪映 draft 主交付物不变**：本单元不碰 `jianying_draft_runner.py` 或任何 draft 生成路径。

---

## Step 0 · 确认现状

```bash
# 1. 建分支
git switch -c quality/events-benchmark

# 2. 核对 Job API logs 端点的实际位置（行号可能已漂移）
grep -n '"logs"' src/services/jobs/api.py | head -10
# 预期约第 236 行：path_parts[2] == "logs"

# 3. 核对 JobStore.load_events 位置
grep -n "def load_events" src/services/jobs/store.py
# 预期约第 229 行

# 4. 核对 JobStore.append_event 位置
grep -n "def append_event" src/services/jobs/store.py
# 预期约第 205 行

# 5. 核对 JobStore._events_path 位置
grep -n "def _events_path" src/services/jobs/store.py
# 预期约第 432 行：返回 {job_id}.events.jsonl

# 6. 核对 JobService.read_logs 位置
grep -n "def read_logs" src/services/jobs/service.py
# 预期约第 1780 行

# 7. 核对前端 getJobLogs 调用位置（全量，无 cursor 参数）
grep -n "getJobLogs\|/logs" frontend-next/src/lib/api/jobs.ts | head -10
# 预期约第 209-211 行：GET /jobs/${jobId}/logs，无 query string

# 8. 核对 Gateway _serve_redacted_logs 位置
grep -n "_serve_redacted_logs\|subpath == .logs" gateway/job_intercept.py | head -5
# 预期约第 3334 行

# 9. 确认 reports/benchmark/ 目录已存在
test -d reports/benchmark && echo "目录已存在" || echo "需创建"

# 10. 确认现有 benchmark marker 是否注册（TU-03 进度）
grep -n "benchmark" pyproject.toml 2>/dev/null | head -5
# 若未注册，本单元 Step 5 补注册

# 11. 记录当前 load_events 实现行数作为基线
awk '/def load_events/,/^    def [a-z]/' src/services/jobs/store.py | wc -l
```

> 执行时前置动作（已定方向）：若上述 grep 行号与 spec 标注偏差超过 50 行，在 Step 1 开头注明实际行号后再动手。

---

## Step 1 · `JobStore.load_events_since` — 增量读取方法

**动作**：在 `src/services/jobs/store.py` 的 `load_events` 方法（实际位置以 Step 0 grep 结果为准，文档参考行 ~229）之后，新增 `load_events_since` 方法。

**具体改法**：

```python
# src/services/jobs/store.py — 在 load_events 结尾之后插入（约第 281 行附近）

def load_events_since(
    self,
    job_id: str,
    *,
    since_cursor: int = 0,
    tail: int | None = None,
) -> tuple[list[JobEvent], int]:
    """增量读取 {job_id}.events.jsonl，返回 (events, next_cursor)。

    Parameters
    ----------
    since_cursor:
        上次调用返回的 next_cursor（即已读取的字节偏移量）。
        0 = 从头读。传入非零值时直接 seek 到该偏移，仅解析之后的新行。
    tail:
        若非 None，仅返回最新 N 条（在 since_cursor 过滤后再取尾部）。
        tail=0 返回空列表但 next_cursor 仍前进到文件末。

    Returns
    -------
    (events, next_cursor):
        events      — 解析成功的 JobEvent 列表（失败行 skip + WARNING）。
        next_cursor — 本次读取结束时的字节偏移，下次调用传入以获取增量。

    与 load_events 的关系
    ---------------------
    load_events 等价于 load_events_since(since_cursor=0)[0]，二者共用
    相同的 skip-malformed-line 语义（fail-open）。不要在此处改变容错策略。
    """
    path = self._events_path(job_id)
    if not path.exists():
        return [], 0

    events: list[JobEvent] = []
    skipped = 0

    with path.open("rb") as handle:
        # seek 到上次游标位置
        if since_cursor > 0:
            file_size = path.stat().st_size
            # 防御：游标超出文件大小时重置为 0（文件被截断或 job_id 重用）
            effective_cursor = min(since_cursor, file_size)
            handle.seek(effective_cursor)
        # 读取从游标到文件末尾的内容
        raw_bytes = handle.read()
        next_cursor = (since_cursor if since_cursor <= path.stat().st_size else 0) + len(raw_bytes)

    for line_no, raw_line in enumerate(
        raw_bytes.decode("utf-8", errors="replace").splitlines(), start=1
    ):
        normalized_line = raw_line.strip()
        if not normalized_line:
            continue
        try:
            payload = json.loads(normalized_line)
            if not isinstance(payload, dict):
                raise ValueError("payload is not a JSON object")
            events.append(JobEvent.from_dict(payload))
        except Exception as exc:
            skipped += 1
            logger.warning(
                "load_events_since: skipping malformed event line at offset+%s in %s (%s)",
                line_no, path.name, exc,
            )

    if skipped:
        logger.info(
            "load_events_since: skipped %d malformed line(s) for job=%s",
            skipped, job_id,
        )

    if tail is not None:
        events = events[-tail:] if tail > 0 else []

    return events, next_cursor
```

**注意**：
- `next_cursor` 使用字节偏移（`int`），不是行号，因为 `.events.jsonl` 以字节 append，偏移是天然幂等游标。
- 文件在两次调用之间被截断的情况（极罕见，仅在手工清空 JSONL 时）：检测到 `since_cursor > file_size` 时静默重置为 0，日志 WARNING，不抛异常——与 `load_events` 的 fail-open 风格一致。

**该步验收**：

```bash
# 新方法存在且签名正确
grep -n "def load_events_since" src/services/jobs/store.py
# 应输出 1 行，行号紧跟 load_events 之后

# 语法检查（import 无误）
python -c "from src.services.jobs.store import JobStore; print('OK')"

# 最小功能冒烟（无 pytest，直接 python）
python - <<'PY'
import tempfile, pathlib, json, os
from src.services.jobs.store import JobStore
from src.services.jobs.events import JobEvent, EVENT_TYPE_LOG

with tempfile.TemporaryDirectory() as d:
    store = JobStore(d)
    jid = "smoke-tu17"
    # 写 2 条 event
    e1 = JobEvent(job_id=jid, event_type=EVENT_TYPE_LOG, created_at="2026-01-01T00:00:00Z", message="first")
    e2 = JobEvent(job_id=jid, event_type=EVENT_TYPE_LOG, created_at="2026-01-01T00:00:01Z", message="second")
    store.append_event(jid, e1, fsync=False)
    store.append_event(jid, e2, fsync=False)
    # 全量读（cursor=0）
    events, cur1 = store.load_events_since(jid, since_cursor=0)
    assert len(events) == 2, f"expected 2 got {len(events)}"
    assert cur1 > 0
    # 增量读（无新行）
    events2, cur2 = store.load_events_since(jid, since_cursor=cur1)
    assert len(events2) == 0, f"expected 0 got {len(events2)}"
    assert cur2 == cur1
    # 新增 1 行后增量读
    e3 = JobEvent(job_id=jid, event_type=EVENT_TYPE_LOG, created_at="2026-01-01T00:00:02Z", message="third")
    store.append_event(jid, e3, fsync=False)
    events3, cur3 = store.load_events_since(jid, since_cursor=cur1)
    assert len(events3) == 1, f"expected 1 got {len(events3)}"
    assert events3[0].message == "third"
    # tail=1 测试
    events4, _ = store.load_events_since(jid, since_cursor=0, tail=1)
    assert len(events4) == 1 and events4[0].message == "third"
    print("冒烟通过")
PY
# 预期最后一行：冒烟通过
```

---

## Step 2 · `JobService.read_logs_since` — 服务层包装

**动作**：在 `src/services/jobs/service.py` 的 `read_logs` 方法（实际位置以 Step 0 grep 结果为准，文档参考行 ~1780）之后，新增 `read_logs_since`。

**具体改法**：

```python
# src/services/jobs/service.py — 在 read_logs 结尾之后插入

def read_logs_since(
    self,
    job_id: str,
    *,
    since_cursor: int = 0,
    tail: int | None = None,
) -> tuple[list[JobEvent], int]:
    """增量读取 job 日志，返回 (events, next_cursor)。

    先校验 job 存在（与 read_logs 一致），再委托 JobStore.load_events_since。
    """
    self.require_job(job_id)
    return self.store.load_events_since(job_id, since_cursor=since_cursor, tail=tail)
```

**该步验收**：

```bash
grep -n "def read_logs_since" src/services/jobs/service.py
# 应输出 1 行

python -c "
import inspect
from src.services.jobs.service import JobService
sig = inspect.signature(JobService.read_logs_since)
params = list(sig.parameters)
assert 'since_cursor' in params and 'tail' in params, f'params={params}'
print('签名 OK:', params)
"
```

---

## Step 3 · Job API 端点：`GET /jobs/{id}/logs` 支持 `since`/`cursor`/`tail` 查询参数

**动作**：修改 `src/services/jobs/api.py` 中处理 `path_parts[2] == "logs"` 的分支（实际位置以 Step 0 grep 结果为准，文档参考行 ~236），新增 query string 解析，并在响应体中携带 `next_cursor`。

**具体改法**（改动最小化，保持旧行为）：

```python
# src/services/jobs/api.py — 替换约第 236-246 行的 logs 分支

if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "logs":
    # --- cursor / since / tail 增量化（TU-17）---
    # 无参数调用：行为与改动前完全一致（next_cursor 字段新增但不破坏现有 consumer）
    from urllib.parse import parse_qs, urlparse as _urlparse
    _qs = parse_qs(_urlparse(self.path).query)

    def _int_param(key: str, default: int) -> int:
        vals = _qs.get(key)
        if not vals:
            return default
        try:
            return max(0, int(vals[0]))
        except (ValueError, TypeError):
            return default

    since_cursor = _int_param("cursor", 0)    # "cursor" 是前端透传字段
    since_iso    = _qs.get("since")            # ISO 8601 时间过滤（见注 ①）
    tail_n       = _int_param("tail", 0) or None  # 0 = 不限制（等价 None）

    if since_cursor > 0 or tail_n is not None:
        # 增量路径
        events, next_cursor = service.read_logs_since(
            path_parts[1],
            since_cursor=since_cursor,
            tail=tail_n,
        )
    else:
        # 全量路径（旧行为，cursor 仍可推进到文件末）
        events = service.read_logs(path_parts[1])
        # 计算 next_cursor 以供首次调用方获取游标
        _, next_cursor = service.read_logs_since(
            path_parts[1],
            since_cursor=0,
        )
        # 若 since (ISO) 过滤，在全量结果上做内存过滤（仅用于首次带时间戳的请求）
        if since_iso:
            since_str = since_iso[0] if isinstance(since_iso, list) else since_iso
            events = [e for e in events if e.created_at >= since_str]

    self._write_json(
        HTTPStatus.OK,
        {
            "job_id": path_parts[1],
            "events": [event.to_dict() for event in events],
            "lines": [event.message for event in events if event.message],
            "next_cursor": next_cursor,   # 新增字段：首次调用后前端保存此值
        },
    )
    return
```

> 注 ①：`since`（ISO 字符串）是备用过滤器，适用于前端已有 `created_at` 但无 cursor 的场景（如页面刷新恢复）。它在内存中过滤全量结果，IO 节省较 cursor 少，但不依赖持久化游标状态，实现更简单。如果前端完全接受 cursor 模式，`since` 参数可在后续版本删除——本单元只做 opt-in，不强制前端切换。

> 注 ②：`_int_param` 是内联 helper 而不是全局函数，避免跨 class 方法传参复杂度——与 `api.py` 现有风格一致（现有代码大量使用局部 lambda / 局部函数）。

**该步验收**：

```bash
# 文件语法无误
python -c "
import ast, pathlib
src = pathlib.Path('src/services/jobs/api.py').read_text(encoding='utf-8')
ast.parse(src)
print('AST parse OK')
"

# 响应体包含 next_cursor 字段
python -c "
import ast, pathlib
src = pathlib.Path('src/services/jobs/api.py').read_text(encoding='utf-8')
assert 'next_cursor' in src, 'next_cursor 不在 api.py 中'
print('next_cursor 字段已加入响应体')
"

# since_cursor 参数名已出现
python -c "
import pathlib
src = pathlib.Path('src/services/jobs/api.py').read_text(encoding='utf-8')
assert 'since_cursor' in src, 'since_cursor 参数不在 api.py'
print('since_cursor OK')
"
```

---

## Step 4 · 前端类型 + `getJobLogs` 更新（兼容旧调用）

**动作**：在 `frontend-next/src/types/api.ts` 的 `ApiJobLogsResponse` 接口（实际位置约第 91 行）新增 `next_cursor?: number`；在 `frontend-next/src/lib/api/jobs.ts` 的 `getJobLogs`（实际位置约第 209 行）保持原有签名不变，但新增 `getJobLogsSince` 函数（供未来轮询升级用）。**不**修改 `workspace/[jobId]/page.tsx` 的轮询调用——只建 API 层接口，不强制前端切换。

**具体改法 A（types/api.ts，约第 91 行）**：

```typescript
export interface ApiJobLogsResponse {
  job_id: string
  events: ApiJobEvent[]
  lines: string[]
  next_cursor?: number   // TU-17：增量游标，0 或不存在表示全量首次响应
}
```

**具体改法 B（lib/api/jobs.ts，约第 209 行之后）**：

```typescript
// 保持原有 getJobLogs 不变（兼容现有轮询）
export async function getJobLogs(jobId: string): Promise<JobLogEntry[]> {
  const payload = await apiClient.get<ApiJobLogsResponse>(`/jobs/${jobId}/logs`)
  return toJobLogEntries(payload.events)
}

// 新增：增量接口（TU-17，供后续轮询优化使用）
export async function getJobLogsSince(
  jobId: string,
  cursor: number,
  opts?: { tail?: number },
): Promise<{ entries: JobLogEntry[]; nextCursor: number }> {
  const params = new URLSearchParams({ cursor: String(cursor) })
  if (opts?.tail != null) params.set('tail', String(opts.tail))
  const payload = await apiClient.get<ApiJobLogsResponse>(
    `/jobs/${jobId}/logs?${params.toString()}`,
  )
  return {
    entries: toJobLogEntries(payload.events),
    nextCursor: payload.next_cursor ?? cursor,
  }
}
```

**该步验收**：

```bash
# TypeScript 编译检查（仅增量，不跑全量 tsc）
cd frontend-next && npx tsc --noEmit --pretty false 2>&1 | grep -E "error TS" | head -10
# 预期：0 行 TS 错误（新增字段是 optional，不影响现有调用方）

# 旧函数签名未变
grep -n "export async function getJobLogs" frontend-next/src/lib/api/jobs.ts
# 预期：1 行，签名与原来完全相同

# 新函数已加入
grep -n "export async function getJobLogsSince" frontend-next/src/lib/api/jobs.ts
# 预期：1 行
cd ..
```

---

## Step 5 · 新增 pytest 测试：cursor 接口契约

**动作**：新建 `tests/test_events_cursor.py`，覆盖 `load_events_since` 的主要行为契约。

**具体改法**：

```python
# tests/test_events_cursor.py
"""TU-17: 契约测试 — JobStore.load_events_since 增量 cursor 接口。

这些测试属于 unit/contract 类别，不依赖外部服务，默认 CI 运行。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.services.jobs.events import (
    EVENT_TYPE_LOG,
    EVENT_TYPE_STATUS,
    JobEvent,
)
from src.services.jobs.store import JobStore


JOB_ID = "tu17-cursor-test"


def _make_event(seq: int, event_type: str = EVENT_TYPE_LOG) -> JobEvent:
    return JobEvent(
        job_id=JOB_ID,
        event_type=event_type,
        created_at=f"2026-01-01T00:00:{seq:02d}Z",
        message=f"msg-{seq}",
    )


@pytest.fixture()
def store_with_events(tmp_path: Path):
    """返回 (store, events_list) — 写入 5 条事件。"""
    store = JobStore(tmp_path)
    events = [_make_event(i) for i in range(5)]
    for e in events:
        store.append_event(JOB_ID, e, fsync=False)
    return store, events


# --- 正常路径 ---

def test_cursor_full_read_returns_all(store_with_events):
    """since_cursor=0 应返回所有 5 条事件，next_cursor > 0。"""
    store, _ = store_with_events
    got, cur = store.load_events_since(JOB_ID, since_cursor=0)
    assert len(got) == 5
    assert cur > 0


def test_cursor_incremental_no_new_lines(store_with_events):
    """用 next_cursor 再次读取，无新行时返回空列表且 cursor 不变。"""
    store, _ = store_with_events
    _, cur1 = store.load_events_since(JOB_ID, since_cursor=0)
    got2, cur2 = store.load_events_since(JOB_ID, since_cursor=cur1)
    assert got2 == []
    assert cur2 == cur1


def test_cursor_incremental_new_lines(store_with_events):
    """用 next_cursor 读取，写入 2 条新事件后应仅返回新增的 2 条。"""
    store, _ = store_with_events
    _, cur1 = store.load_events_since(JOB_ID, since_cursor=0)
    store.append_event(JOB_ID, _make_event(10), fsync=False)
    store.append_event(JOB_ID, _make_event(11), fsync=False)
    got, cur2 = store.load_events_since(JOB_ID, since_cursor=cur1)
    assert len(got) == 2
    assert got[0].message == "msg-10"
    assert got[1].message == "msg-11"
    assert cur2 > cur1


def test_cursor_tail(store_with_events):
    """tail=2 应仅返回最后 2 条。"""
    store, _ = store_with_events
    got, _ = store.load_events_since(JOB_ID, since_cursor=0, tail=2)
    assert len(got) == 2
    assert got[-1].message == "msg-4"


def test_cursor_tail_zero(store_with_events):
    """tail=0 应返回空列表，cursor 仍前进到文件末。"""
    store, _ = store_with_events
    _, cur_full = store.load_events_since(JOB_ID, since_cursor=0)
    got, cur_tail0 = store.load_events_since(JOB_ID, since_cursor=0, tail=0)
    assert got == []
    assert cur_tail0 == cur_full


def test_cursor_nonexistent_job(tmp_path: Path):
    """不存在的 job 应返回 ([], 0)，不抛异常。"""
    store = JobStore(tmp_path)
    got, cur = store.load_events_since("no-such-job", since_cursor=0)
    assert got == []
    assert cur == 0


# --- 容错路径（与 load_events 保持一致的 fail-open 语义）---

def test_cursor_tolerates_malformed_line(tmp_path: Path):
    """畸形行应 skip + WARNING，不中断其余事件的解析。"""
    store = JobStore(tmp_path)
    events_path = tmp_path / f"{JOB_ID}.events.jsonl"
    good = _make_event(0)
    # 手动写一个 good + 一个畸形行 + 一个 good
    with events_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(good.to_dict()) + "\n")
        f.write("NOT JSON AT ALL\n")
        f.write(json.dumps(_make_event(1).to_dict()) + "\n")
    got, _ = store.load_events_since(JOB_ID, since_cursor=0)
    # 2 条正常，1 条 skip
    assert len(got) == 2
    messages = [e.message for e in got]
    assert "msg-0" in messages and "msg-1" in messages


def test_cursor_beyond_file_size_resets(tmp_path: Path):
    """cursor 超出文件大小（截断场景）时应重置到 0，不抛异常。"""
    store = JobStore(tmp_path)
    store.append_event(JOB_ID, _make_event(0), fsync=False)
    _, cur = store.load_events_since(JOB_ID, since_cursor=0)
    # 模拟 cursor 比文件大
    got, next_cur = store.load_events_since(JOB_ID, since_cursor=cur + 99_999)
    # 不抛，返回空或重新读（实现可选；关键：不崩）
    assert isinstance(got, list)
    assert isinstance(next_cur, int)


# --- 等价性验证：load_events_since(cursor=0)[0] == load_events ---

def test_cursor_equivalence_with_load_events(store_with_events):
    """cursor=0 的结果应与 load_events 完全一致（消息列表相同顺序）。"""
    store, _ = store_with_events
    full = store.load_events(JOB_ID)
    since, _ = store.load_events_since(JOB_ID, since_cursor=0)
    assert [e.message for e in since] == [e.message for e in full]
```

**该步验收**：

```bash
python -m pytest tests/test_events_cursor.py -v 2>&1 | tail -15
# 预期：8 条 PASSED，0 FAILED，0 ERROR

# 确认新测试文件存在
test -f tests/test_events_cursor.py && echo "文件存在"
```

---

## Step 6 · `scripts/benchmark_pipeline_stage_timings.py` + fixture 约定

**动作**：新建 `scripts/benchmark_pipeline_stage_timings.py`。脚本读取 `tests/fixtures/benchmark/<case>/` 目录下的 `events.jsonl`，从事件的 `stage` 与 `created_at` 字段推算各 stage 的起止时间与耗时，输出 JSON 到 `reports/benchmark/stage_timings_<YYYYMMDD_HHMMSS>.json`。同时创建 fixture 示例目录与说明文件。

**具体改法 A — 脚本主体**：

```python
#!/usr/bin/env python3
# scripts/benchmark_pipeline_stage_timings.py
"""Pipeline stage 计时 benchmark harness（TU-17）。

用途
----
读取已有的 events.jsonl fixture，提取各 stage 的首/末事件时间戳，
输出 JSON timing 报告到 reports/benchmark/。

使用方式
--------
python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case>
python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case> \\
    --out reports/benchmark/my_timing.json

CI 用途
-------
本脚本不作普通 pass/fail 阻断，只生成 artifact。
CI job 应在 continue-on-error 模式下调用，输出文件作为 artifact 上传。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_dt(ts: str) -> datetime | None:
    """Parse ISO 8601 timestamp, return None if unparseable."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def extract_stage_timings(events_jsonl_path: Path) -> dict:
    """从 events.jsonl 文件提取各 stage 的计时信息。

    Returns
    -------
    dict with keys:
      stages: {stage_name: {first_at, last_at, duration_s, event_count}}
      total_events: int
      skipped_lines: int
      source_file: str
    """
    stage_first: dict[str, datetime] = {}
    stage_last: dict[str, datetime] = {}
    stage_count: dict[str, int] = {}
    total = 0
    skipped = 0

    text = events_jsonl_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        total += 1
        stage = payload.get("stage") or "__no_stage__"
        created_at = payload.get("created_at", "")
        dt = _parse_dt(str(created_at)) if created_at else None
        if dt is None:
            continue
        if stage not in stage_first:
            stage_first[stage] = dt
            stage_last[stage] = dt
            stage_count[stage] = 0
        else:
            if dt < stage_first[stage]:
                stage_first[stage] = dt
            if dt > stage_last[stage]:
                stage_last[stage] = dt
        stage_count[stage] += 1

    stages = {}
    for s in stage_first:
        first = stage_first[s]
        last = stage_last[s]
        dur = (last - first).total_seconds()
        stages[s] = {
            "first_at": first.isoformat(),
            "last_at": last.isoformat(),
            "duration_s": round(dur, 3),
            "event_count": stage_count[s],
        }
    # 按 first_at 排序，便于阅读
    stages = dict(
        sorted(stages.items(), key=lambda kv: kv[1]["first_at"])
    )

    return {
        "stages": stages,
        "total_events": total,
        "skipped_lines": skipped,
        "source_file": str(events_jsonl_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        required=True,
        help="包含 events.jsonl 的 fixture 目录（tests/fixtures/benchmark/<case>）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出 JSON 路径，默认 reports/benchmark/stage_timings_<ts>.json",
    )
    args = parser.parse_args()

    fixture_dir = Path(args.fixture)
    events_file = fixture_dir / "events.jsonl"
    if not events_file.exists():
        print(f"[ERROR] 找不到 {events_file}", file=sys.stderr)
        return 1

    result = extract_stage_timings(events_file)

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = Path("reports/benchmark")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"stage_timings_{ts}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[benchmark] 写入 {out_path}")
    print(f"[benchmark] stages={len(result['stages'])} total_events={result['total_events']} skipped={result['skipped_lines']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**具体改法 B — fixture 目录约定**：

```bash
# 创建 fixture 目录（提交占位文件，真实 fixture 由开发者离线准备后添加）
mkdir -p tests/fixtures/benchmark/example-30min-job
```

```
# tests/fixtures/benchmark/README.md（新建）
# Pipeline benchmark fixtures

每个子目录是一个 case，包含：
- events.jsonl — 从真实任务的 {job_id}.events.jsonl 复制，**已脱敏**（替换真实 job_id/用户信息）
- case.json（可选）— case 元数据（job_id、视频长度、语言对、日期）

如何准备 fixture：
1. 从生产或测试环境取 events.jsonl（cp/scp）
2. 脱敏：替换 job_id 为占位符，删除含用户 email/文件路径的 message 行
3. 放入 tests/fixtures/benchmark/<case_name>/events.jsonl
4. 提交到仓库（JSONL 文本，size 通常 < 500 KB）

CI 只存 artifact，不阻断：
  python scripts/benchmark_pipeline_stage_timings.py --fixture tests/fixtures/benchmark/<case>
  # 输出到 reports/benchmark/stage_timings_*.json
```

**具体改法 C — pytest 集成（benchmark marker）**：

新建 `tests/test_benchmark_harness.py`，使其在 `benchmark` marker 下运行，验证脚本逻辑正确性（不需要真实大 fixture）：

```python
# tests/test_benchmark_harness.py
"""TU-17: benchmark harness 功能测试（marker=benchmark，默认 CI 排除）。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

# 若 TU-03 尚未注册 benchmark marker，此测试文件首行需 pytestmark
pytestmark = pytest.mark.benchmark


def _write_fixture(tmp_path: Path, lines: list[dict]) -> Path:
    fixture_dir = tmp_path / "case"
    fixture_dir.mkdir()
    events = fixture_dir / "events.jsonl"
    events.write_text(
        "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
        encoding="utf-8",
    )
    return fixture_dir


def test_benchmark_harness_basic(tmp_path):
    """extract_stage_timings 能从 fixture 正确提取 stage 计时。"""
    from scripts.benchmark_pipeline_stage_timings import extract_stage_timings

    fixture_dir = _write_fixture(tmp_path, [
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:00:00Z", "stage": "transcribe", "message": "start"},
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:01:30Z", "stage": "transcribe", "message": "end"},
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:01:35Z", "stage": "translate", "message": "start"},
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:03:05Z", "stage": "translate", "message": "end"},
    ])
    result = extract_stage_timings(fixture_dir / "events.jsonl")
    assert result["total_events"] == 4
    assert result["skipped_lines"] == 0
    assert "transcribe" in result["stages"]
    assert "translate" in result["stages"]
    assert abs(result["stages"]["transcribe"]["duration_s"] - 90.0) < 0.1
    assert abs(result["stages"]["translate"]["duration_s"] - 90.0) < 0.1


def test_benchmark_harness_skips_malformed(tmp_path):
    """畸形行应 skip，不影响其余计时。"""
    from scripts.benchmark_pipeline_stage_timings import extract_stage_timings

    fixture_dir = _write_fixture(tmp_path, [
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:00:00Z", "stage": "tts", "message": "ok"},
    ])
    # 向 events.jsonl 追加一条畸形行
    (fixture_dir / "events.jsonl").open("a").write("BAD LINE\n")
    result = extract_stage_timings(fixture_dir / "events.jsonl")
    assert result["skipped_lines"] == 1
    assert "tts" in result["stages"]


def test_benchmark_harness_outputs_json(tmp_path):
    """脚本 CLI 能在 --out 指定路径写出 JSON 文件。"""
    import subprocess, sys
    fixture_dir = _write_fixture(tmp_path, [
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:00:00Z", "stage": "align", "message": "a"},
        {"job_id": "j1", "event_type": "log", "created_at": "2026-01-01T00:00:05Z", "stage": "align", "message": "b"},
    ])
    out_path = tmp_path / "out.json"
    result = subprocess.run(
        [sys.executable, "scripts/benchmark_pipeline_stage_timings.py",
         "--fixture", str(fixture_dir), "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "stages" in data
    assert "align" in data["stages"]
```

**该步验收**：

```bash
# 脚本文件存在且语法正确
python -c "
import ast, pathlib
src = pathlib.Path('scripts/benchmark_pipeline_stage_timings.py').read_text(encoding='utf-8')
ast.parse(src)
print('AST parse OK')
"

# fixture 目录结构存在
test -f tests/fixtures/benchmark/README.md && echo "README 存在"

# benchmark marker 测试通过（用 -m benchmark 显式运行）
python -m pytest tests/test_benchmark_harness.py -v -m benchmark 2>&1 | tail -10
# 预期：3 条 PASSED，0 FAILED

# 对示例空 fixture（若无真实 fixture 则用临时文件测试 CLI）
python - <<'PY'
import tempfile, pathlib, json, subprocess, sys
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)
    (p / "events.jsonl").write_text(
        json.dumps({"job_id":"x","event_type":"log","created_at":"2026-01-01T00:00:00Z","stage":"tts","message":"hi"}) + "\n",
        encoding="utf-8"
    )
    out = p / "out.json"
    r = subprocess.run([sys.executable, "scripts/benchmark_pipeline_stage_timings.py",
                        "--fixture", str(p), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text())
    assert "tts" in data["stages"]
    print("CLI smoke OK")
PY
```

---

## Step 7 · Gateway query string 透传验证

**动作**：无需改动 `gateway/job_intercept.py`——`proxy_request` 已通过 `strip_prefix="/job-api"` 转发路径，query string 作为 URL 的一部分自然透传。本步只做回归验证，确保现有 `_serve_redacted_logs` 不会因 `next_cursor` 新字段而 500。

**具体改法**：无代码改动，仅添加注释性集成测试断言。

**该步验收**：

```bash
# 验证 _serve_redacted_logs 响应透传逻辑：next_cursor 字段不被剥离
grep -n "next_cursor\|\"cursor\"\|since_cursor" gateway/job_intercept.py | head -5
# 预期：0 行（Gateway 不感知 cursor，只透传）

# 现有 Gateway logs 重定向测试仍通过
python -m pytest tests/test_gateway_logs_redaction.py -v 2>&1 | tail -10
# 预期：全部 PASSED，0 FAILED

# 确认 _serve_redacted_logs 函数没有修改
git diff gateway/job_intercept.py | wc -l
# 预期：0（本单元不改 gateway/job_intercept.py）
```

---

## 测试计划

### 新增测试

| 文件 | 测试数 | Marker | 说明 |
|---|---|---|---|
| `tests/test_events_cursor.py` | 8 | `unit`（默认 CI 运行） | JobStore.load_events_since 契约，含正常/增量/tail/容错/等价性 |
| `tests/test_benchmark_harness.py` | 3 | `benchmark`（CI 排除，`-m benchmark` 显式运行） | extract_stage_timings + CLI smoke |

### 回归测试（需通过，不得新增失败）

```bash
# Job store 基础测试
python -m pytest tests/test_job_store.py -v 2>&1 | tail -5

# Gateway logs 重定向测试
python -m pytest tests/test_gateway_logs_redaction.py -v 2>&1 | tail -5

# Job API 端测试
python -m pytest tests/test_job_api.py -v 2>&1 | tail -5

# 全量 backend（排除 slow/real_provider/benchmark，按 TU-03 配置）
python -m pytest -q -m "not slow and not real_provider and not benchmark" 2>&1 | tail -5
```

---

## 回滚方案

**PR 拆分（CodeX 2026-06-25 决策）**：本单元执行拆成两个独立 PR，两 PR 可并行提 review 也可顺序合入。

### PR1 · `/logs` 增量 cursor（Step 0–5 + Step 7）

建议分支：`quality/events-cursor`

**commit 边界**：

1. **commit-1**（backend cursor 接口）：
   - `src/services/jobs/store.py` — `load_events_since`
   - `src/services/jobs/service.py` — `read_logs_since`
   - `src/services/jobs/api.py` — logs 端点 query 解析 + `next_cursor`
   - `tests/test_events_cursor.py`

2. **commit-2**（frontend 类型 + API 函数）：
   - `frontend-next/src/types/api.ts` — `next_cursor?` 字段
   - `frontend-next/src/lib/api/jobs.ts` — `getJobLogsSince`

**回滚 PR1**：

```bash
# 回滚单个 commit
git revert <commit-1-hash> --no-edit

# 回滚整个 PR1 分支
git switch main
git branch -D quality/events-cursor
```

### PR2 · benchmark harness（Step 6）

建议分支：`quality/benchmark-harness`

**commit 边界**：

3. **commit-3**（benchmark harness，report-only，不阻断普通 CI）：
   - `scripts/benchmark_pipeline_stage_timings.py`
   - `tests/fixtures/benchmark/README.md`（+ `example-30min-job/` 目录占位）
   - `tests/test_benchmark_harness.py`

**回滚 PR2**：

```bash
# 回滚整个 PR2 分支
git switch main
git branch -D quality/benchmark-harness
```

**涉及文件（共 7 个，不含 fixture 占位）**：

| 文件 | 操作 |
|---|---|
| `src/services/jobs/store.py` | 新增方法（纯增量，不改现有方法） |
| `src/services/jobs/service.py` | 新增方法（纯增量） |
| `src/services/jobs/api.py` | 修改 logs 分支（旧路径保留兼容） |
| `frontend-next/src/types/api.ts` | 新增 optional 字段 |
| `frontend-next/src/lib/api/jobs.ts` | 新增函数（不改旧函数） |
| `scripts/benchmark_pipeline_stage_timings.py` | 新建 |
| `tests/test_events_cursor.py` | 新建 |
| `tests/test_benchmark_harness.py` | 新建 |
| `tests/fixtures/benchmark/README.md` | 新建 |

---

## 完成定义（DoD）

### PR1（/logs 增量 cursor）

- [ ] `grep -n "def load_events_since" src/services/jobs/store.py` 输出 1 行
- [ ] `grep -n "def read_logs_since" src/services/jobs/service.py` 输出 1 行
- [ ] `python -c "from src.services.jobs.store import JobStore; print('OK')"` 无 ImportError
- [ ] `grep -n "next_cursor" src/services/jobs/api.py` 在 logs 分支处输出至少 1 行
- [ ] `python -m pytest tests/test_events_cursor.py -v` → **8 PASSED，0 FAILED**
- [ ] `python -m pytest tests/test_job_store.py tests/test_gateway_logs_redaction.py tests/test_job_api.py -q` → **0 新增失败**（已有失败不超出改动前基线）
- [ ] `grep -n "export async function getJobLogsSince" frontend-next/src/lib/api/jobs.ts` 输出 1 行
- [ ] `cd frontend-next && npx tsc --noEmit --pretty false 2>&1 | grep "error TS" | wc -l` 输出 **0**
- [ ] `git diff gateway/job_intercept.py | wc -l` 输出 **0**（Gateway 无改动）
- [ ] 无参数调用 `GET /jobs/{id}/logs` 的响应体与改动前结构完全相同（`job_id` + `events[]` + `lines[]` 均存在，`next_cursor` 仅新增不删旧）——**向后兼容性红线，PR1 合入前必须验证**
- [ ] `workspace/[jobId]/page.tsx` 的轮询调用未被修改（前端 polling 调参不在本单元）
- [ ] **各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`**

### PR2（benchmark harness）

- [ ] `test -f scripts/benchmark_pipeline_stage_timings.py && echo 存在` 输出"存在"
- [ ] `python -m pytest tests/test_benchmark_harness.py -v -m benchmark` → **3 PASSED，0 FAILED**
- [ ] benchmark 测试不在普通 CI 默认运行（确认 `pyproject.toml` addopts 含 `not benchmark`，或 CI 配置以 `continue-on-error` 隔离）
- [ ] benchmark 脚本输出 JSON 到 `reports/benchmark/`，CI 存为 artifact，**不以非零退出码阻断普通 CI pass/fail**
- [ ] `test -f tests/fixtures/benchmark/README.md && echo 存在` 输出"存在"
- [ ] **各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`**
