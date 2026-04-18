# JobStatus 扩展 `editing` — 状态触点清单

- 生成日期：2026-04-18
- 方案：[`2026-04-18-studio-post-edit-plan.md`](../plans/2026-04-18-studio-post-edit-plan.md)
- 目标：在加入 `editing` 状态后，确保所有现有代码路径对新状态的行为正确
- 对应任务：**T0-1**（autoplan Phase 0 前置产物）
- 使用方式：T0-2 migration + T0-3 枚举扩展 + T0-5 cleanup + T0-6 UX 在动任何状态相关代码前，先对照本清单逐点确认改动

---

## 0. 设计基线：`editing` 状态的语义归类

在开始扫触点前，先确定 `editing` 在系统中的语义分类：

| 现有分类 | 是否包含 `editing` | 理由 |
|---------|------------------|------|
| `ACTIVE_JOB_STATUSES` | ✅ **包含** | editing 是"活跃中，cleanup 不可删"的一员 |
| `TERMINAL_STATUSES` (gateway/quota.py) | ❌ **不包含** | editing 非终态，不触发 quota 结算 |
| "需要 worker 进程的 active"（reap stale 逻辑用）| ❌ **不包含** | editing 没有后台进程，不算 stale 误伤 |
| 前端 `POLL_STATUSES`（列表页轮询）| ✅ **包含** | 列表需要显示 editing badge；另外闲置接近 24h 时需要 UI 提示 |
| 前端 `isProcessing` 判断 | ❌ **不包含** | editing 不是"系统正在处理"，是"用户正在编辑"，UI 应单独 `isEditing` 分支 |
| `isWaitingForReview` 判断 | ❌ **不包含** | editing 和 review gate 是两种不同的审核路径 |
| `continue_job` 允许的状态（old review gate）| ❌ **不包含** | editing 走独立 `editing/commit` 路径，不走 `/jobs/{id}/continue` |

**新增常量**（放到 `models.py` / `types/jobs.ts`，避免多处内联硬编码）：

```python
# src/services/jobs/models.py（新增）
JOB_STATUS_EDITING = "editing"

SUPPORTED_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,      # NEW
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
}

ACTIVE_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,      # NEW — editing is active, cleanup must skip
}

# NEW — 用于 reap stale 逻辑，和 ACTIVE 区分
WORKER_ACTIVE_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    # waiting_for_review / editing 没有 worker 进程，不算 stale
}
```

```ts
// frontend-next/src/types/jobs.ts（新增）
export const JOB_STATUS_LABELS = {
  // ... existing ...
  editing: '修改中',   // NEW
}

export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'waiting_for_review',
  'editing',   // NEW
]
```

---

## 1. 触点详情（8 个 CodeX 指出的文件 + 扩展发现的 4 处）

### 触点 1：[frontend-next/src/types/jobs.ts](../../frontend-next/src/types/jobs.ts)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L1-7 `JOB_STATUS_LABELS` | 6 种状态 → 中文 | 加 `editing: '修改中'` |
| L10 `JobStatus` type | `keyof typeof JOB_STATUS_LABELS` | 自动扩展（无需改类型定义，改 map 即可） |
| L148-151 `ACTIVE_JOB_STATUSES` | `['queued', 'running', 'waiting_for_review']` | 追加 `'editing'` |

**守卫**：AST 扫确保没有地方写 `['queued', 'running', 'waiting_for_review']` 字面量，都引用 `ACTIVE_JOB_STATUSES` 常量。

---

### 触点 2：[frontend-next/src/features/jobs/selectors.ts](../../frontend-next/src/features/jobs/selectors.ts)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L1 | `import { ACTIVE_JOB_STATUSES }` | 保持 |
| L10 `selectCurrentTaskJob` | `orderedJobs.find((job) => ACTIVE_JOB_STATUSES.includes(job.status))` | 自动扩展（通过 ACTIVE_JOB_STATUSES 的变化）✓ |
| **L22** 内联硬编码 | `['running', 'queued', 'waiting_for_review'].includes(j.status)` | **重构为 `ACTIVE_JOB_STATUSES.includes(j.status)`**（去硬编码 + 自动包含 editing） |
| **L26** `priorityOrder` | `['waiting_for_review', 'running', 'queued']` | **改为 `['editing', 'waiting_for_review', 'running', 'queued']`**（editing 是用户主动操作，优先级最高） |
| L28-29 | `priorityOrder.indexOf(a.status)` | 自动扩展 ✓ |

**设计决策**：editing 在列表页"最新活跃任务"选择中的优先级**最高**（高于 waiting_for_review）。理由：editing 是用户主动发起的编辑会话，在 UX 上优先"继续修改"按钮。

---

### 触点 3：[frontend-next/src/app/(app)/projects/page.tsx](../../frontend-next/src/app/(app)/projects/page.tsx)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| **L41** `POLL_STATUSES` 常量 | `["running", "queued", "waiting_for_review"]` | **加 `"editing"`**。列表页需要监听 editing 任务的状态变化（admin cancel / idle auto-cancel / 用户点"继续修改"后切换） |
| **L108** 内联硬编码 | `['running', 'queued', 'waiting_for_review'].includes(j.status) && ...` | **重构为 `ACTIVE_JOB_STATUSES.includes(j.status)`** |
| L133 | `jobs.some((j) => POLL_STATUSES.includes(j.status))` | 自动扩展 ✓（依赖 L41 改动）|
| L359 `hasWaiting` | `activeTask?.status === "waiting_for_review"` | **保持不动**（这是严格的 waiting_for_review 判断，与 editing 语义不同） |
| L437 `<StatusBadge>` | 读 job.status | 需要 StatusBadge 组件支持 `editing`（紫色 D46）|
| **L494-587** `switch (job.status)` 卡片内容渲染 | 6 个 case（succeeded/running/waiting_for_review/failed/queued/cancelled）| **加 `case "editing":`**（紫色 badge + 继续修改 CTA + 异常段数提示）|
| **L625-643** `switch (job.status)` CTA 按钮 | 6 个 case | **加 `case "editing":`** return "继续修改"按钮，跳 `/workspace/{id}/edit` |

---

### 触点 4：[frontend-next/src/app/(app)/workspace/[jobId]/page.tsx](../../frontend-next/src/app/(app)/workspace/[jobId]/page.tsx)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L45-47 `sendBrowserNotification` 文案 | succeeded / failed / cancelled 三种通知 | **加 `editing: null`**（editing 转换不发通知，避免噪音） |
| L82 | `status === 'succeeded' \|\| 'failed' \|\| 'cancelled'` 终态判断 | **保持**（editing 非终态）|
| L123 | `if (job.status !== 'waiting_for_review') return` | **保持**（这是 review gate 专用钩子）|
| L168 `isWaitingForReview` | `status === 'waiting_for_review'` | **保持** |
| **L169** `isProcessing` | `status === 'running' \|\| status === 'queued'` | **保持不加 editing**。editing 不应走 "正在处理" 分支 |
| **L169 新增** `isEditing` | N/A | **新增** `const isEditing = job.status === 'editing'` |
| L170 `isSucceeded` / L171 `isFailed` | 单状态判断 | 保持 |
| **L195/219/232** 分支渲染 | `isWaitingForReview \|\| isProcessing` / `isProcessing` / `isProcessing` | **新增 editing 分支**："你正在修改此任务 · 已修改 N 次" + "继续修改" CTA |

---

### 触点 5：[src/services/jobs/models.py](../../src/services/jobs/models.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| **L29-34** 状态常量 | 6 个 `JOB_STATUS_*` | **加 `JOB_STATUS_EDITING = "editing"`** |
| **L35-42** `SUPPORTED_JOB_STATUSES` | 6 个 | 加 `JOB_STATUS_EDITING` |
| **L43-47** `ACTIVE_JOB_STATUSES` | 3 个 | 加 `JOB_STATUS_EDITING` |
| **新增** `WORKER_ACTIVE_STATUSES` | N/A | **新增 set**: `{QUEUED, RUNNING}`（给 reap stale 用） |

**设计决策**：`WORKER_ACTIVE_STATUSES` 是**新集合**，专用于判断"是否应该有 worker 进程"。区分于 `ACTIVE_JOB_STATUSES`（更宽，包含 waiting_for_review / editing）。

---

### 触点 6：[src/services/jobs/service.py](../../src/services/jobs/service.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L10 `from .models import ACTIVE_JOB_STATUSES` | 导入 | **加 `WORKER_ACTIVE_STATUSES`** |
| **L157-158** `continue_job` 校验 | `if record.status != JOB_STATUS_WAITING_FOR_REVIEW: raise 409` | **保持不动**。continue 路径是老 review gate，editing 不走这里，走新端点 `editing/commit` |
| **L205** reap stale 早返回 | `if record.status in {QUEUED, RUNNING}` | **改为 `in WORKER_ACTIVE_STATUSES`**（语义更清晰，虽然值相同） |
| **L213** reap stale 主体 | `if record.status in ACTIVE_JOB_STATUSES` + 无进程 → mark failed | **⚠ 关键修复**：改为 `if record.status in WORKER_ACTIVE_STATUSES`。否则 editing 任务会被误判"无进程 = stale"全部标 failed |

**⚠ 关键守卫**：`test_editing_not_marked_stale_by_reaper`：构造 editing 态 job 且无 worker 进程 → 跑 reaper → 断言 status 仍是 editing，**未被标 failed**。

---

### 触点 7：[src/services/jobs/process_runner.py](../../src/services/jobs/process_runner.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L94 `"failed": STAGE_FAILED` | 映射 | **无需改**（stage 映射，与 job.status 无关）|
| L119-199 `running_job` 主流程 | 启动 worker 进程并写 status="running" | **无需改**（editing 不走此路径） |
| L201 `is_process_active` | 判断进程是否在 | **无需改** |
| L209 `kill process` | 杀进程 | **无需改** |
| L704+ 失败阶段识别 | stage-level 失败 | **无需改** |

**结论**：process_runner 全程**不需要感知 editing 状态**。editing 不启动 worker 进程。

**新增**（D28 / T1-8）：`submit_job_from_existing_project_dir(...)` 入口放 [`src/services/jobs/runner_extensions.py`](../../src/services/jobs/runner_extensions.py)（新文件），**不污染 process_runner.py**。

---

### 触点 8：[gateway/job_intercept.py](../../gateway/job_intercept.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L28 `from quota import ... TERMINAL_STATUSES` | 导入 | **保持**（editing 非终态，不改 TERMINAL）|
| L241 `upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES` | 终态转换结算 quota | **保持** |
| L245 `if upstream_status == "succeeded"` | 结算分支 | **保持** |
| **L351** SQL `Job.status.in_(["queued", "running", "waiting_for_review"])` | 硬编码列表 | **加 `"editing"`**，或抽成 Python 端常量 `from models import ACTIVE_JOB_STATUSES` 复用 |
| L461 `status=job_data.get("status", "queued")` | 创建时默认 queued | **保持** |
| L539-646 `continue_job` 拦截 | 检查 status == waiting_for_review / 提升为 running | **保持不动**。editing 走独立 Gateway 端点 `editing/commit`，不走 `/continue` |
| L630 `job.status != "waiting_for_review"` | 校验 | 保持（这是 `continue` 专用） |
| L647 `job.status = "running"` | 转换 | 保持 |

**⚠ 新增 ownership 校验**（非状态触点，但与 editing 端点相关）：

新增的 gateway 端点 `POST /jobs/{id}/enter-edit` / `/editing/commit` / `/editing/cancel` / `PATCH /jobs/{id}` 必须走现有 ownership 中间件（`verify_job_owner`），保持与 `/continue` / `/cancel` 一致的鉴权层。T1-1 落实。

---

## 2. 扩展发现的 4 处触点（CodeX 8 点清单之外）

### 触点 9：[src/services/web_ui/job_managers.py](../../src/services/web_ui/job_managers.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L18 `from ... import ACTIVE_JOB_STATUSES` | 导入 | 保持 |
| L436 `if ... not in ACTIVE_JOB_STATUSES:` | 跟踪逻辑 | **自动扩展**（ACTIVE 已包含 editing，但要确认该 tracker 对 editing 的 expected behavior） |
| L624 `raw_job.get("status") in ACTIVE_JOB_STATUSES` | 列表过滤 | **自动扩展** |

**审核点**：这两处引用 ACTIVE_JOB_STATUSES 后自动包含 editing。T0-3 需要 code review 确认两处 callers 对 editing 态的行为正确（不会误把 editing 视作 "pipeline 跑中"）。

---

### 触点 10：[frontend-next/src/components/workspace/TranslationForm.tsx](../../frontend-next/src/components/workspace/TranslationForm.tsx)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L14 `import { ACTIVE_JOB_STATUSES }` | 导入 | 保持 |
| L58 `allJobs.filter((j) => ACTIVE_JOB_STATUSES.includes(j.status))` | 过滤活跃任务 | **自动扩展**（editing 视为活跃，防止用户同时发起两个任务） |

---

### 触点 11：[frontend-next/src/app/(app)/tasks/current/page.tsx](../../frontend-next/src/app/(app)/tasks/current/page.tsx)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L10 `import { ACTIVE_JOB_STATUSES }` | 导入 | 保持 |
| L38 `if (ACTIVE_JOB_STATUSES.includes(latestJob.status))` | 当前任务判断 | **自动扩展** |

---

### 触点 12：[gateway/quota.py](../../gateway/quota.py)

| 位置 | 当前行为 | 改动 |
|------|---------|------|
| L23 `TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}` | 终态集 | **保持**（editing 非终态） |

---

## 3. T0-3 验证清单

T0-3 上线前，必须逐项 ✓：

- [ ] `models.py` 新增 `JOB_STATUS_EDITING` / 补 `SUPPORTED_JOB_STATUSES` / 补 `ACTIVE_JOB_STATUSES` / 新增 `WORKER_ACTIVE_STATUSES`
- [ ] `types/jobs.ts` 新增 `editing: '修改中'` 到 `JOB_STATUS_LABELS` / 补 `ACTIVE_JOB_STATUSES` 数组
- [ ] `selectors.ts` L22 去硬编码用 `ACTIVE_JOB_STATUSES` 常量
- [ ] `selectors.ts` L26 `priorityOrder` 首位插入 `'editing'`
- [ ] `projects/page.tsx` L41 `POLL_STATUSES` 加 `'editing'`
- [ ] `projects/page.tsx` L108 去硬编码用 `ACTIVE_JOB_STATUSES`
- [ ] `projects/page.tsx` L494 switch 加 `case "editing":` 紫色 badge + "继续修改" CTA
- [ ] `projects/page.tsx` L625 switch 加 `case "editing":` CTA 跳转
- [ ] `workspace/[jobId]/page.tsx` L169 后新增 `isEditing` 常量
- [ ] `workspace/[jobId]/page.tsx` L195/219/232 新增 editing 分支渲染
- [ ] `service.py` L213 **从 `ACTIVE_JOB_STATUSES` 改为 `WORKER_ACTIVE_STATUSES`**（关键修复）
- [ ] `job_intercept.py` L351 SQL 加 `"editing"`
- [ ] 新建 `runner_extensions.py` 放 `submit_job_from_existing_project_dir`（不污染 process_runner）
- [ ] AST 守卫测试：所有 status 相关代码必须使用枚举常量 / `ACTIVE_JOB_STATUSES` / `WORKER_ACTIVE_STATUSES`，不得写字符串字面量或 inline 数组
- [ ] 契约测试：`test_editing_not_marked_stale_by_reaper` 确保 editing 不被 reap stale 误杀
- [ ] 契约测试：`test_editing_included_in_active_statuses` 前后端 `ACTIVE_JOB_STATUSES` 都包含 editing

---

## 4. 关键设计决策总结

1. **editing ∈ ACTIVE_JOB_STATUSES 但 ∉ WORKER_ACTIVE_STATUSES**：新增 set 是必要的（不是 over-engineering），避免 editing 被 reap stale 误杀
2. **editing 不改 `TERMINAL_STATUSES`**：quota 结算逻辑不受影响
3. **editing 不走 `continue_job` 路径**：走独立 gateway 端点 `enter-edit` / `editing/commit` / `editing/cancel`
4. **editing 在前端 `isProcessing` 判断里单独处理**：UI 分支 `isEditing` 而不是合入 isProcessing（因为语义是"用户编辑"而非"系统处理"）
5. **`selectors.ts` priorityOrder 首位插入 editing**：列表页"最新活跃任务"选 editing 优先，UX 对齐"继续你未完成的修改"
6. **所有 inline 字面量重构为常量引用**：AST 守卫避免未来漂移

---

**本清单将在 T0-3（JobStatus 枚举扩展）实施时作为 checklist 逐点执行。所有未来涉及 `job.status` 的代码改动都应回查本清单是否有新增触点遗漏。**
