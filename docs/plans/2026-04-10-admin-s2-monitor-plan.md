# 方案二：S2 审校效果监控

> 日期：2026-04-10
> 状态：方案（已审校 R2，待实施）
> 审校：Claude Opus + Codex，2026-04-10（R1 + R2）

---

## 1. 目标

新建管理员后台 `/admin/s2-monitor` 页面，将 S2 三段式拆分的效果从"每任务看 JSON artifact"升级为"聚合看板 + 异常标记 + 按需详情"。

核心回答 3 个问题：
1. **三段式工作正常吗** — legacy 回退率、模型降级率、Pass 3 成功率
2. **审校质量怎么样** — 纠错数、术语提取、行数变化
3. **哪些任务有问题** — legacy 回退的、越界多的、profile 缺失的

---

## 2. 现有数据基础

### 2.1 每任务 artifact（`transcript/` 目录下）

| 文件 | 已有字段 | 监控价值 |
|------|---------|---------|
| `s2_pass1_result.json` | review_model, has_audio, skipped, fallback_used, speakers, corrections, corrections_applied, sanity_applied, contract_violations, generated_at | 说话人识别质量、模型降级 |
| `s2_pass2_result.json` | review_model, fallback_used, glossary, corrections, corrections_applied, contract_violations, generated_at | 文本修正质量、术语提取 |
| `s2_pass3_result.json` | review_model, has_audio, fallback_used, speaker_profiles, clips_extracted, contract_violations, generated_at | 音色画像覆盖率 |
| `s2_review_result.json` | review_model, has_audio, speakers, glossary, corrections_applied, sanity_applied, line_counts.original/final | 聚合概览 |
| `s2_review_audit.json` | audit_events[].source (correction/sanity_check/post_processing) | 变更来源分析 |
| `s2_review_speaker_diff.json` | 各阶段 speaker 变更 diff | 排障 |
| `s2_pass{1,2,3}_attempt{N}_{label}.json` | success, error, model, response_length, response_text, timestamp | JSON 解析失败率、错误模式（**详情端点按需读取**） |

### 2.2 fallback 语义澄清（关键）

代码中存在三个层级的 fallback，语义不同，不可混淆：

| 层级 | 含义 | 判定方式 | 影响范围 |
|------|------|---------|---------|
| **Legacy 回退** | Pass 1/2 整体失败，`_orchestrate_three_pass()` 抛 `_PassFailure`，回退到 `legacy_review_transcript_single_pass()` | 只有 `s2_review_result.json`，没有 `s2_pass1_result.json` 和 `s2_pass2_result.json` | 整个 S2 流程退化 |
| **模型降级** | 单个 Pass 内 primary 模型失败，降级到 retry/cheapest 模型 | `s2_pass{N}_result.json` 中 `fallback_used: true`（**需先修复前置补丁，见 §3.1**） | 单个 Pass 质量可能下降 |
| **Pass 3 回退** | Pass 3 voice profile 生成失败，不回滚但缺画像 | 无 `s2_pass3_result.json` 或其中 `fallback_used: true` | 音色匹配质量下降 |

**注意**：Legacy 回退时不会写 `s2_pass1_result.json` / `s2_pass2_result.json`，因此不能从这些文件的 `fallback_used` 字段统计 legacy 回退率。

### 2.3 `fallback_used` 字段当前缺陷（R2 新增）

**问题**：`transcript_reviewer.py` 三处写入 `fallback_used` 均为硬编码 `False`：
- L821: `s2_pass1_result.json` → `"fallback_used": False`
- L836: `s2_pass2_result.json` → `"fallback_used": False`
- L1616: `s2_pass3_result.json` → `"fallback_used": False`

实际上每个 Pass 的 retry 链是 `primary → retry → cheapest`，成功时 `_label` 变量已记录了到底是哪一步成功的，但这个信息没写入 artifact。

**结论**：如不修复，第一版聚合端点的 `model_downgrade_count` 将永远为 0——这比"没有这个指标"更糟糕（会误导运营认为从未降级）。必须先做前置补丁（§3.1）。

### 2.4 orchestrator_mode 推断逻辑

当前 artifact 中没有显式的 `orchestrator_mode` 字段，需要从文件存在性推断：

| 情况 | 判定 |
|------|------|
| 存在 `s2_pass1_result.json` 或 `s2_pass2_result.json` | `three_pass` |
| 只有 `s2_review_result.json`，无 pass1/pass2 文件 | `legacy_or_old`（无法区分 legacy 回退 vs 三段式上线前的旧任务） |
| 无任何 S2 文件 | `no_s2_data` |

第一版不强行区分 legacy 回退和旧任务，统一归入 `legacy_or_old` 桶。

### 2.5 Express 模式的 Pass 1

Express 模式下 Pass 1 写入 `skipped: true`。聚合统计时：
- `skipped: true` 的任务**不计入** pass1.total（否则 downgrade_rate 偏低）
- 前端明细表 Pass 1 列显示"跳过"

### 2.6 当前缺口

- 没有聚合 API — 每次只能看单个任务的 JSON
- 没有趋势统计 — 无法观察模型/prompt 改动后效果变化
- 没有异常标记 — 不知道哪些 job 退回 legacy 了
- 没有耗时监控 — 有 `generated_at` 但没有 pass 级耗时

---

## 3. 后端改动

### 3.1 前置补丁：修复 `fallback_used` 写入（改 `transcript_reviewer.py`）

**目的**：让 `s2_pass{1,2,3}_result.json` 的 `fallback_used` 反映真实的模型降级情况。

**改动**：每个 Pass 的 retry 循环成功后，`_label` 变量已存在（值为 `"primary"` / `"retry"` / `"cheapest"`）。在写入 artifact 时：
- `"fallback_used": _label != "primary"`（Pass 1: L821, Pass 2: L836, Pass 3: L1616）
- 新增 `"success_attempt_label": _label`（记录具体是哪步成功的）
- 新增 `"success_attempt_model": _model_id`（记录最终使用的模型 ID）

**影响范围**：仅改 3 处 artifact 写入，不影响 pipeline 逻辑、不影响 ReviewResult 返回值。

**注意**：此补丁对已有的旧 artifact 无效（它们的 `fallback_used` 仍为 `False`）。聚合端点对旧 artifact 需容忍这种不准确——在 `model_downgrade_count` 统计说明中标注"仅统计补丁后的任务"。

### 3.2 新建文件：`gateway/s2_monitor_api.py`

`admin_settings.py` 已 1000+ 行，不再往里塞。新建独立模块，在 `gateway/main.py` 注册 router。

### 3.3 端点一：`GET /api/admin/s2-stats`（聚合 + 任务摘要列表）

**参数**：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 50 | 返回任务数上限（仅影响 `jobs` 列表） |
| `offset` | int | 0 | 分页偏移（仅影响 `jobs` 列表） |
| `days` | int | 7 | 只统计最近 N 天的任务（按 created_at） |

**实现流程**：
1. `_require_admin(user)` 校验
2. 复用现有 admin jobs 的 Job API + DB 合并逻辑，获取 job 列表 + project_dir 映射
3. 按 `created_at` 过滤（最近 `days` 天）
4. **状态分流**（R2 新增）：
   - **参与 S2 质量聚合**的状态：`succeeded`、`failed`（已跑完或跑挂，S2 阶段已经过或已跳过）
   - **不参与聚合、仅出现在列表**的状态：`queued`、`running`、`cancelled`（未到 S2 或中途放弃）
   - 返回中标注 `jobs_not_eligible`（不参与聚合的任务数），避免把未跑到 S2 的任务算入 `no_s2_data`
5. 对 eligible 任务中有 `project_dir` 的，读 4 个 result 文件：`s2_review_result.json` + `s2_pass1_result.json` + `s2_pass2_result.json` + `s2_pass3_result.json`
6. **不读 attempt 文件**（attempt 级数据由详情端点按需提供）
7. **先聚合全量 eligible 任务**（R2 修正：aggregate 覆盖整个 `days` 范围，不受分页影响）
8. 再对 `jobs` 列表做 `offset` / `limit` 分页
9. 返回

**返回结构**：

```json
{
  "filter": { "days": 7, "limit": 50, "offset": 0 },

  "total_jobs_in_range": 30,
  "jobs_eligible": 25,
  "jobs_not_eligible": 5,

  "aggregate": {
    "eligible_total": 25,
    "three_pass_count": 14,
    "legacy_or_old_count": 6,
    "no_s2_data_count": 5,

    "pass1": {
      "total": 12,
      "skipped_count": 2,
      "model_downgrade_count": 3,
      "model_downgrade_rate_pct": 25.0,
      "avg_corrections": 4.2,
      "avg_sanity_applied": 1.1,
      "total_contract_violations": 3,
      "models_used": { "gemini-3.1-pro-preview": 4, "gemini-2.5-flash-lite": 8 }
    },
    "pass2": {
      "total": 20,
      "model_downgrade_count": 2,
      "model_downgrade_rate_pct": 10.0,
      "avg_corrections": 8.5,
      "avg_glossary_terms": 7.2,
      "avg_line_change": 1.6,
      "total_contract_violations": 1,
      "models_used": { "gemini-3.1-pro-preview": 15, "gemini-2.5-flash-lite": 5 }
    },
    "pass3": {
      "total": 18,
      "missing_count": 2,
      "success_rate_pct": 88.9,
      "avg_profiles_generated": 2.8,
      "avg_clips_extracted": 2.5,
      "total_contract_violations": 0
    }
  },

  "jobs": [
    {
      "job_id": "job_abc...",
      "video_title": "CNN News",
      "service_mode": "studio",
      "status": "succeeded",
      "created_at": "2026-04-10T12:00:00Z",
      "eligible": true,

      "orchestrator_mode": "three_pass",
      "speakers_count": 3,

      "pass1_model": "gemini-2.5-flash-lite",
      "pass1_skipped": false,
      "pass1_model_downgrade": true,
      "pass1_corrections": 4,
      "pass1_sanity": 1,
      "pass1_violations": 0,
      "pass1_has_audio": true,

      "pass2_model": "gemini-3.1-pro-preview",
      "pass2_model_downgrade": false,
      "pass2_corrections": 8,
      "pass2_glossary_terms": 6,
      "pass2_violations": 0,

      "pass3_success": true,
      "pass3_profiles": 3,
      "pass3_clips": 3,
      "pass3_violations": 0,

      "lines_before": 10,
      "lines_after": 12
    },
    {
      "job_id": "job_xyz...",
      "video_title": "Processing...",
      "service_mode": "studio",
      "status": "running",
      "created_at": "2026-04-10T14:00:00Z",
      "eligible": false,

      "orchestrator_mode": null,
      "note": "任务仍在运行中，不参与 S2 质量统计"
    }
  ]
}
```

**关键设计决策**：
- **aggregate 覆盖全量**：`aggregate` 统计覆盖整个 `days` 范围内所有 eligible 任务，不受 `limit`/`offset` 分页影响（R2 修正）
- **状态边界**：只有 `succeeded`/`failed` 参与 S2 质量聚合；`queued`/`running`/`cancelled` 标记 `eligible: false`，不进聚合但出现在列表中（R2 新增）
- `pass1.total` 排除 `skipped: true` 的任务（Express 模式）
- `model_downgrade` 对应 artifact 中的 `fallback_used: true`（需前置补丁 §3.1 生效后才准确；旧 artifact 统计不准确）
- `legacy_or_old_count` 包含 legacy 回退和旧任务，不强行区分
- `no_s2_data_count` 作为可见指标，不静默隐藏
- `video_title` 从 job payload 取，取不到留空

**性能考虑**：
- 分页 + 时间过滤后，通常只扫描 ~50 个 job 的 4 个文件 = ~200 次文件读取（aggregate 扫全量，但 7 天内通常不多）
- 文件读取失败静默跳过
- 未来如果仍不够快，可在 job 完成时将 S2 摘要写入 DB（第二步优化）

### 3.4 端点二：`GET /api/admin/s2-stats/{job_id}`（单任务详情）

**用途**：点击任务行时按需加载 Pass 详情。

**实现流程**：
1. `_require_admin(user)` 校验
2. 从 DB 获取 `project_dir`
3. 读取该任务的所有 S2 artifact：result 文件 + attempt 文件 + audit + speaker_diff

**返回结构**：

```json
{
  "job_id": "job_abc...",

  "pass1": {
    "result": { /* s2_pass1_result.json 完整内容 */ },
    "attempts": [
      {
        "attempt": 1, "label": "primary", "model": "gemini-3.1-pro",
        "success": false, "error": "Unterminated string at line 19",
        "response_length": 1234, "timestamp": "..."
      },
      {
        "attempt": 2, "label": "retry", "model": "gemini-3.1-pro",
        "success": false, "error": "Expecting value at line 19",
        "response_length": 890, "timestamp": "..."
      },
      {
        "attempt": 3, "label": "cheapest", "model": "gemini-2.5-flash-lite",
        "success": true, "error": null,
        "response_length": 2100, "timestamp": "..."
      }
    ]
  },
  "pass2": { "result": {}, "attempts": [] },
  "pass3": { "result": {}, "attempts": [] },

  "review_result": { /* s2_review_result.json */ },
  "audit": { /* s2_review_audit.json */ },
  "speaker_diff": { /* s2_review_speaker_diff.json */ }
}
```

**注意**：attempt 文件中的 `response_text` 可能很长，返回时截断到前 500 字符 + 总长度，避免 payload 过大。

---

## 4. 前端改动

### 4.1 新建 `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx`

**第一版页面布局（精简）**：

```
┌──────────────────────────────────────────────────────────┐
│ S2 审校效果监控                              最近 7 天 ▼ │
├──────────────────────────────────────────────────────────┤
│ 总览卡片（1×5）                                          │
│ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ │
│ │S2任务数│ │三段式率│ │Legacy/ │ │无S2数据│ │Pass3  │ │
│ │  20    │ │ 70.0%  │ │旧任务  │ │  5     │ │成功率  │ │
│ │/25合格 │ │ 14/20  │ │ 6(30%) │ │        │ │ 88.9% │ │
│ └────────┘ └────────┘ └────────┘ └────────┘ └────────┘ │
│ 不参与统计：5 个任务（running/queued/cancelled）          │
├──────────────────────────────────────────────────────────┤
│ 任务明细                                                 │
│ ┌──────┬──────┬────┬───────┬───────┬───────┬──────┬────┐│
│ │JobID │标题  │模式│P1模型 │P1纠正 │P2修正 │P3   │行数││
│ │abc.. │CNN.. │stud│flash ↓│  4    │  8    │ ✓   │10→12│
│ │def.. │Fox.. │expr│(跳过) │  —    │  5    │ ✓   │8→8 ││
│ │ghi.. │BBC.. │stud│pro    │  0    │  3    │ ✗   │6→6 ││
│ │jkl.. │旧任务│stud│legacy/旧                    │5→5 ││
│ │mno.. │进行中│stud│—（运行中，不参与统计）—           ││
│ └──────┴──────┴────┴───────┴───────┴───────┴──────┴────┘│
│                                                 < 1 2 > │
│ ↓ = 模型降级（primary 失败，降到更便宜模型）              │
│ ✗ = Pass 3 失败/缺失                                     │
│ legacy/旧 = legacy 回退或三段式上线前的旧任务              │
│                                                          │
│ 点击行 → 弹出 Drawer 显示 Pass 详情 + attempt 链          │
└──────────────────────────────────────────────────────────┘
```

**交互**：
- 加载时调 `GET /api/admin/s2-stats?days=7&limit=50`
- 5 个总览卡片：从 `aggregate` 字段计算，**始终反映整个时间范围**（不受分页影响）
- 卡片下方小字标注不参与统计的任务数
- 任务明细表：从 `jobs` 数组渲染，分页
- `eligible: false` 的任务灰显，S2 列显示"—"
- 右上角时间范围下拉：7天 / 30天 / 全部
- 模型降级标 ↓，Pass 3 失败标 ✗，legacy/旧任务整行灰显
- 点击 eligible 行 → 调 `GET /api/admin/s2-stats/{job_id}` → Drawer 显示 Pass 详情

### 4.2 导航入口

在 `frontend-next/src/components/app-shell.tsx` 的 admin nav group 中加 `/admin/s2-monitor`，标签"审校监控"。

---

## 5. 与方案一的关系

方案一（任务监控 + AI 分析）和方案二是独立的：
- 方案一：在 `/admin/jobs` 看**单个任务**的日志 + AI 分析
- 方案二：在 `/admin/s2-monitor` 看**所有任务**的 S2 聚合效果

两者可以交叉导航：方案二的任务行点击 job_id 跳转到方案一的日志面板。

---

## 6. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/services/transcript_reviewer.py` | 修改 | **前置补丁**：修复 `fallback_used` 硬编码 + 新增 `success_attempt_label`/`success_attempt_model` 字段（3 处，各 ~3 行） |
| `gateway/s2_monitor_api.py` | **新建** | S2 聚合 + 详情两个端点 |
| `gateway/main.py` | 修改 | 注册 s2_monitor_api router |
| `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx` | **新建** | S2 审校效果监控页 |
| `frontend-next/src/components/app-shell.tsx` | 修改 | 加 `/admin/s2-monitor` 导航入口 |

**不改动**：`process.py`、`admin_settings.py`、pipeline 编排逻辑

---

## 7. 第二步优化

| 优化 | 说明 |
|------|------|
| `orchestrator_mode` 写入 artifact | 在 `_write_pass_artifact()` / `_write_review_debug_artifacts()` 中显式写入 `orchestrator_mode` 字段，消除推断歧义 |
| `attempts_count` / `parse_failures` 写入 result | 在 result 文件中增加字段，聚合时不需要扫 attempt 文件 |
| Pass 耗时 | 新增 `pass1/2/3_duration_ms` 字段（需改 transcript_reviewer.py） |
| 趋势图（recharts） | 每日 legacy 率、模型降级率、Pass 3 成功率 |
| 维度筛选 | 按 service_mode（Studio/Express）、review_model 切分 |
| DB 持久化 | job 完成时写 S2 摘要到 gateway DB，避免文件扫描 |
| 任务详情增强 | Pass 1/2/3 摘要卡片 + speaker diff 时间线 + audit events + raw response 折叠 |
| JSON 解析失败聚合 | 在聚合端点加 attempt 级统计（依赖 result 文件增加字段或单独扫 attempt） |

---

## 8. 验证

1. 访问 `/admin/s2-monitor` → 5 个卡片有数据
2. `legacy_or_old` 和 `no_s2_data` 卡片正确显示（不静默隐藏）
3. `jobs_not_eligible` 行标注在卡片下方，值与 `running`/`queued`/`cancelled` 数量一致
4. **切换分页后卡片数字不变**（aggregate 覆盖全量，不受分页影响）
5. 任务明细表：模型降级标 ↓，Pass 3 失败标 ✗，legacy/旧任务灰显
6. `eligible: false` 的任务灰显，S2 列显示"—"
7. Express 任务 Pass 1 列显示"跳过"，不计入 pass1.total
8. 切换时间范围（7天/30天/全部）→ 数据刷新
9. 点击 eligible 任务行 → Drawer 弹出 → 显示 attempt 链 + Pass 详情
10. 非 admin → 403
11. 分页正常工作
12. **前置补丁验证**：新任务完成后，`s2_pass{1,2,3}_result.json` 中 `fallback_used` 不再始终为 `False`；如果实际降级了，值为 `true` 且 `success_attempt_label` 为 `"retry"` 或 `"cheapest"`

---

## 附录：审校记录

### 审校来源
- Claude Opus 审校（2026-04-10）
- Codex 审校 R1（2026-04-10）
- Codex 审校 R2（2026-04-10）

### R1 合并采纳的建议

| 来源 | 建议 | 处理 |
|------|------|------|
| **Codex** | fallback 三层语义拆分（legacy 回退 / 模型降级 / Pass 3 回退） | ✅ 采纳，重新定义所有 fallback 指标 |
| **Codex** | 加 `unknown` 桶，不强行区分 legacy 和旧任务 | ✅ 采纳，改为 `legacy_or_old_count` |
| **Codex** | attempt 扫描与返回结构不一致 | ✅ 采纳，主端点砍掉 attempt 指标 |
| **Codex** | 列表/详情拆两个端点 | ✅ 采纳，新增 `/{job_id}` 详情端点 |
| **Codex** | 从 `admin_settings.py` 拆出独立模块 | ✅ 采纳，新建 `s2_monitor_api.py` |
| **Codex** | 加 `jobs_without_s2_data` 卡片 | ✅ 采纳 |
| **Codex** | 复用 admin jobs 合并逻辑 | ✅ 采纳 |
| **Codex** | `video_title` fallback 链 | ✅ 采纳（取不到留空） |
| **Opus** | 分页参数 `limit`/`offset` | ✅ 采纳 |
| **Opus** | 时间范围 `days` 参数 | ✅ 采纳 |
| **Opus** | 前端精简到 4-5 卡片 + 1 表格 | ✅ 采纳（5 卡片 + 表格） |
| **Opus** | Express skip 排除 pass1 计数 | ✅ 采纳 |
| **Opus** | attempt 做独立详情端点 | ✅ 采纳（与 Codex 拆端点建议合并） |

### R2 合并采纳的建议

| 来源 | 建议 | 处理 |
|------|------|------|
| **Codex R2** | `fallback_used` 当前硬编码 `False`，`model_downgrade` 指标不准 | ✅ 采纳，新增前置补丁 §3.1 修复 `transcript_reviewer.py`，改动文件清单同步更新 |
| **Codex R2** | aggregate 被分页截断，卡片变成"当前页统计" | ✅ 采纳，明确 aggregate 先于分页计算，覆盖整个 `days` 范围；验证项 #4 增加"切换分页卡片不变"检查 |
| **Codex R2** | `no_s2_data` 被 `queued`/`running`/`cancelled` 任务污染 | ✅ 采纳，新增状态边界定义：只有 `succeeded`/`failed` 参与聚合；新增 `jobs_eligible`/`jobs_not_eligible` 字段；前端灰显不参与统计的任务 |
