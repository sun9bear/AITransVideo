# 方案二：S2 审校效果监控

> 日期：2026-04-10
> 状态：方案（待审批）

---

## 1. 目标

新建管理员后台 `/admin/s2-monitor` 页面，将 S2 三段式拆分的效果从"每任务看 JSON artifact"升级为"聚合看板 + 异常队列 + 任务详情"。

核心回答 3 个问题：
1. **三段式工作正常吗** — fallback 率、contract 越界率、Pass 3 成功率
2. **审校质量怎么样** — 纠错数、术语提取、行数变化
3. **哪些任务有问题** — fallback 的、越界多的、profile 缺失的

---

## 2. 现有数据基础

### 2.1 每任务 artifact（`transcript/` 目录下）

| 文件 | 已有字段 | 监控价值 |
|------|---------|---------|
| `s2_pass1_result.json` | review_model, has_audio, fallback_used, speakers, corrections, corrections_applied, sanity_applied, contract_violations, generated_at | 说话人识别质量、模型降级 |
| `s2_pass2_result.json` | review_model, fallback_used, glossary, corrections, corrections_applied, contract_violations, generated_at | 文本修正质量、术语提取 |
| `s2_pass3_result.json` | review_model, has_audio, fallback_used, speaker_profiles, clips_extracted, contract_violations, generated_at | 音色画像覆盖率 |
| `s2_review_result.json` | review_model, has_audio, speakers, glossary, corrections_applied, sanity_applied, line_counts.original/final | 聚合概览 |
| `s2_review_audit.json` | audit_events[].source (correction/sanity_check/post_processing) | 变更来源分析 |
| `s2_review_speaker_diff.json` | 各阶段 speaker 变更 diff | 排障 |
| `s2_pass{1,2,3}_attempt{N}_{label}.json` | 每次 LLM 调用的原始输出（含失败），字段：success, error, model, response_length, response_text, timestamp | JSON 解析失败率、错误模式、模型输出质量 |

### 2.2 attempt 级重试记录（`_dump_retry_response()`）

每个 Pass 的每次 LLM 调用（无论成功/失败）都保存为独立文件，例如：
```
transcript/
  s2_pass1_attempt1_primary.json     ← gemini-3.1-pro, success=false, error="Unterminated string..."
  s2_pass1_attempt2_retry.json       ← gemini-3.1-pro, success=false, error="Expecting value..."
  s2_pass1_attempt3_cheapest.json    ← gemini-2.5-flash-lite, success=true
  s2_pass2_attempt1_primary.json     ← gemini-3.1-pro, success=false
  s2_pass2_attempt2_retry.json       ← gemini-3.1-pro, success=true
```

每个文件包含：
- `success`: bool — 是否 JSON 解析成功
- `error`: string | null — 解析错误信息（如 "Unterminated string at line 19 column 17"）
- `model`: string — 使用的模型 ID
- `response_length`: int — 原始响应字符长度（可判断是否截断）
- `response_text`: string — 完整的模型原始输出
- `timestamp`: ISO8601

这些数据的监控价值：
- **JSON 解析失败率**：哪个模型/哪个 Pass 最容易输出坏 JSON
- **错误模式分析**：是截断（Unterminated string）还是格式错误（Expecting value）
- **响应长度分布**：判断是否 output token 限制导致截断
- **降级链路径**：primary → retry → cheapest 的实际走通率

### 2.3 当前缺口

- 没有聚合 API — 每次只能看单个任务的 JSON
- 没有趋势统计 — 无法观察模型/prompt 改动后效果变化
- 没有异常队列 — 不知道哪些 job fallback 了、哪些越界多
- 没有耗时监控 — 有 `generated_at` 但没有 pass 级耗时

---

## 3. 后端改动

### 3.1 新增端点：`GET /api/admin/s2-stats`

**位置**：`gateway/admin_settings.py`，admin jobs 端点附近

**实现策略**：从每个 job 的 `project_dir/transcript/` 读 artifact 文件并聚合。不改数据库，不改 pipeline 写入逻辑。

**流程**：
1. `_require_admin(user)` 校验
2. 从 Job API 获取所有 job 列表：`GET {JOB_API_BASE}/jobs`
3. 从 gateway DB 获取 project_dir 映射
4. 对每个有 `project_dir` 的 job，尝试读 `s2_review_result.json` + `s2_pass1_result.json` + `s2_pass2_result.json` + `s2_pass3_result.json`
5. 聚合统计 + 返回

**返回结构**：

```json
{
  "total_jobs_scanned": 25,
  "jobs_with_s2_data": 20,

  "aggregate": {
    "three_pass_count": 16,
    "legacy_fallback_count": 4,
    "three_pass_rate_pct": 80.0,

    "pass1": {
      "total": 16,
      "fallback_count": 6,
      "fallback_rate_pct": 37.5,
      "avg_corrections": 4.2,
      "avg_sanity_applied": 1.1,
      "total_contract_violations": 3,
      "models_used": { "gemini-3.1-pro-preview": 4, "gemini-2.5-flash-lite": 12 },
      "json_parse_failures": 12,
      "json_parse_failure_rate_pct": 50.0,
      "avg_attempts_to_success": 2.4
    },
    "pass2": {
      "total": 20,
      "fallback_count": 2,
      "fallback_rate_pct": 10.0,
      "avg_corrections": 8.5,
      "avg_glossary_terms": 7.2,
      "avg_line_change": 1.6,
      "total_contract_violations": 1,
      "models_used": { "gemini-3.1-pro-preview": 15, "gemini-2.5-flash-lite": 5 },
      "json_parse_failures": 3,
      "json_parse_failure_rate_pct": 10.0,
      "avg_attempts_to_success": 1.2
    },
    "pass3": {
      "total": 18,
      "fallback_count": 2,
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

      "orchestrator_mode": "three_pass",
      "speakers_count": 3,

      "pass1_model": "gemini-2.5-flash-lite",
      "pass1_fallback": true,
      "pass1_corrections": 4,
      "pass1_sanity": 1,
      "pass1_violations": 0,
      "pass1_has_audio": true,
      "pass1_attempts": 3,
      "pass1_parse_failures": 2,
      "pass1_errors": ["Unterminated string at line 19", "Expecting value at line 19"],

      "pass2_model": "gemini-3.1-pro-preview",
      "pass2_fallback": false,
      "pass2_corrections": 8,
      "pass2_glossary_terms": 6,
      "pass2_violations": 0,
      "pass2_attempts": 1,
      "pass2_parse_failures": 0,

      "pass3_success": true,
      "pass3_profiles": 3,
      "pass3_clips": 3,
      "pass3_violations": 0,

      "lines_before": 10,
      "lines_after": 12
    }
  ]
}
```

**性能考虑**：
- 扫描文件 I/O 可能慢（job 多时）。第一版直接扫描，可接受（admin 页面不需要秒级响应）
- 文件读取失败（权限/不存在）静默跳过，不影响其他 job
- 未来如果 job 过多，可在 job 完成时将 S2 摘要写入 DB（第二步优化）

---

## 4. 前端改动

### 4.1 新建 `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx`

**页面布局**：

```
┌──────────────────────────────────────────────────────────┐
│ S2 审校效果监控                                           │
├──────────────────────────────────────────────────────────┤
│ 总览卡片（2×4 网格）                                      │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│ │ S2任务数 │ │三段式率 │ │Legacy率 │ │Pass1降级│        │
│ │   20    │ │  80.0%  │ │  20.0%  │ │  37.5% │        │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│ │JSON失败 │ │Pass2修正│ │Contract │ │Pass3成功│        │
│ │ 15次/50%│ │ 8.5/job │ │越界 4次 │ │  88.9% │        │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
├──────────────────────────────────────────────────────────┤
│ Pass 分布                                                 │
│                                                          │
│ Pass 1 模型使用                                           │
│ gemini-3.1-pro: 4 (25%)  |  gemini-2.5-flash-lite: 12   │
│                                                          │
│ Pass 2 模型使用                                           │
│ gemini-3.1-pro: 15 (75%) |  gemini-2.5-flash-lite: 5    │
├──────────────────────────────────────────────────────────┤
│ 任务明细                                                  │
│ ┌──────┬──────┬────┬───────┬───────┬───────┬──────┬────┐│
│ │JobID │标题  │模式│P1模型 │P1纠正 │P2修正 │P3   │行数││
│ │abc.. │CNN.. │stud│flash ⚠│  4    │  8    │ ✅  │10→12│
│ │def.. │Fox.. │expr│(跳过) │  —    │  5    │ ✅  │8→8 ││
│ │ghi.. │BBC.. │stud│pro    │  0    │  3    │ ❌  │6→6 ││
│ └──────┴──────┴────┴───────┴───────┴───────┴──────┴────┘│
│                                                          │
│ ⚠ = fallback（降级到更便宜模型）                           │
│ ❌ = Pass 3 失败/fallback                                 │
│                                                          │
│ 点击行展开 → Pass 1/2/3 详细数据                           │
└──────────────────────────────────────────────────────────┘
```

**交互**：
- 加载时调 `GET /api/admin/s2-stats`
- 8 个总览卡片：从 `aggregate` 字段计算
- Pass 模型分布：从 `aggregate.pass1.models_used` / `pass2.models_used`
- 任务明细表：从 `jobs` 数组渲染
- 点击行可展开显示该任务的详细 Pass 数据（纠正列表、术语表、speaker 变更等）
- fallback 标记用 ⚠ 图标，Pass 3 失败用 ❌

### 4.2 导航入口

在 `frontend-next/src/components/app-shell.tsx` 的 admin nav group 中加 `/admin/s2-monitor`，标签"审校监控"。

---

## 5. 与方案一的关系

方案一（任务监控 + AI 分析）和方案二是独立的：
- 方案一：在 `/admin/jobs` 看**单个任务**的日志 + AI 分析
- 方案二：在 `/admin/s2-monitor` 看**所有任务**的 S2 聚合效果

两者可以交叉导航：方案二的异常任务点击 job_id 跳转到方案一的日志面板。

---

## 6. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `gateway/admin_settings.py` | 修改 | 新增 `s2-stats` 端点 |
| `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx` | **新建** | S2 审校效果监控页 |
| `frontend-next/src/components/app-shell.tsx` | 修改 | 加 `/admin/s2-monitor` 导航入口 |

**不改动**：`transcript_reviewer.py`、`process.py`、S2 artifact 写入逻辑

---

## 7. 第二步优化

| 优化 | 说明 |
|------|------|
| 新增 6 个 summary 字段 | `orchestrator_mode`, `fallback_reason`, `pass1/2/3_duration_ms`, `pass3_cache_hit`（需改 transcript_reviewer.py 写入逻辑） |
| 趋势图（recharts） | 每日 fallback rate、contract violation rate、Pass 3 success rate |
| 维度筛选 | 按 service_mode（Studio/Express）、review_model 切分 |
| DB 持久化 | job 完成时写 S2 摘要到 gateway DB，避免文件扫描性能问题 |
| 任务详情页 | Pass 1/2/3 摘要卡片 + speaker diff 时间线 + audit events + raw response 折叠 |

---

## 8. 验证

1. 访问 `/admin/s2-monitor` → 8 个卡片有数据
2. Pass 模型分布显示正确（与实际 artifact 一致）
3. 任务明细表：fallback 任务标 ⚠，Pass 3 失败标 ❌
4. 点击行展开 → 显示 Pass 详细数据
5. Express 任务 Pass 1 列显示"跳过"
6. 非 admin → 403
7. 无 S2 数据的 job → 明细表中不显示
