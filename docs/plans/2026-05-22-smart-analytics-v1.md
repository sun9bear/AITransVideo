# 智能版监控 v1 设计（P5 — 数据分析）

**Date**: 2026-05-22
**Status**: 设计冻结，准备开始 PR-1
**Owner**: admin（单 admin 项目）
**Reviewed**: 用户已确认 3 个 Tab 内容 + 散点图 + 修改事件分布 + 入口文案

---

## 1. 目标（user-confirmed）

按 [`docs/plans/2026-05-04-smart-auto-pipeline-plan.md`](2026-05-04-smart-auto-pipeline-plan.md) §P5
列出的 4 件事，本 v1 聚焦回答 **3 个核心问题**：

1. **Handoff / 失败原因分布** —— 哪些 reason_code 高频？是否需要进一步优化？
2. **对齐质量分布** —— 哪些任务强制 DSP 占比异常？是 probe 不准还是 target_cps 设置问题？
3. **用户返工率** —— 智能版交付后用户进 editing 的比例 + 改了什么？评估 smart
   是否「足够好用」、是否达到产品预期。

不在 v1 范围（明确 deferred）：
- 真实毛利率 / 单次成本分析 —— 已有 `/admin/jobs/{id}/cost` 单任务页，v1 不重复
- 按内容类型阈值校准 —— 样本量 19 个，过早；至少 50+ 任务后再启动
- 多用户对比筛选 —— 当前只有 admin 主用户 + 1 个 plus 用户；不值得做多用户筛选

---

## 2. 数据源（全部已有，无新打点）

| 数据 | 来源 | 用途 |
|---|---|---|
| 任务基本信息 | PG `jobs` 表 | status, service_mode, source_duration, created_at, user_id, project_dir, smart_state |
| 用户信息 | PG `users` 表 | email, display_name 用于展示 |
| Smart 决策审计 | `{project_dir}/audit/smart_decisions.jsonl` | handoff reason_code, decision_type, evidence |
| Alignment 指标 | `{project_dir}/output/alignment_report.txt` | 解析 5 行统计（直接使用 / DSP变速 / 重写后直接 / 强制DSP / 短段保护） + 复查段数 |
| 用户修改事件 | `{project_dir}/audit/user_edit_events.jsonl` | text_changed / tts_regenerated / split_confirmed / speaker_changed / dubbing_mode_changed |

**注意**：`alignment_report.txt` 是人类可读文本，不是 JSON。需要正则解析。格式样例：

```
对齐方式统计：
  直接使用（误差<5%）：48段（37%）
  DSP变速：19段（15%）
  Gemini重写后直接使用：9段（7%）
  Gemini重写后DSP对齐：5段（4%）
  强制DSP兜底：18段（14%）
  短段听感保护DSP：27段（21%）

⚠️ 需要手工检查的段落（共16段）：
```

解析靠 6 行固定关键词锚点（中文标签）+ `数字段（百分号%）` 正则提取。

---

## 3. 后端 API（2 个新 endpoint）

### `GET /api/admin/smart-analytics/summary`

Query params:
- `days` (int, default 30, max 365): 时间窗
- `status` (str, default "all"): all / succeeded / failed / editing
- `user` (str/uuid, default "all"): all / 特定 user_id

Response (200):
```jsonc
{
  "window": {"days": 30, "from": "2026-04-22", "to": "2026-05-22"},
  "filters": {"status": "all", "user": "all"},
  "kpi": {
    "total_smart_jobs": 19,
    "succeeded": 15,
    "failed": 4,
    "editing": 0,
    "handoff_rate": 0.105,          // 2/19
    "top_handoff_reason": "uncertain_speaker_share",
    "avg_forced_dsp_pct": 0.185,    // 跨成功任务平均
    "p90_forced_dsp_pct": 0.35,
    "rework_rate": 0.26,            // 5/19 进了 editing
    "avg_edited_segments": 3.2      // 跨进入 editing 的任务
  },
  "handoff_distribution": [
    {"reason_code": "0_handoff_succeeded", "count": 15, "pct": 0.79, "sample_job_ids": []},
    {"reason_code": "uncertain_speaker_share", "count": 1, "pct": 0.05,
     "sample_job_ids": ["job_88bdca0966ce..."]},
    {"reason_code": "glossary_preservation", "count": 1, "pct": 0.05,
     "sample_job_ids": ["job_14989c5e9ec4..."]}
    // ...
  ],
  "alignment_quality": [
    // sorted by forced_dsp_pct desc
    {
      "job_id": "job_88bdca...",
      "display_name": "Google I/O 2026 主题演讲",
      "user_email": "admin@...",
      "source_duration_seconds": 4103.616,
      "total_segments": 122,
      "direct_pct": 0.25,
      "dsp_pct": 0.43,
      "rewrite_direct_pct": 0.07,
      "rewrite_dsp_pct": 0.04,
      "forced_dsp_pct": 0.14,
      "short_segment_dsp_pct": 0.09,
      "manual_review_segments": 12
    }
    // ...
  ],
  "rework_by_user": [
    {
      "user_id": "342bbde3-...",
      "user_email": "admin@...",
      "smart_job_count": 17,
      "entered_editing_count": 4,
      "rework_rate": 0.235,
      "avg_edited_segments": 3.2
    }
    // ...
  ],
  "edit_event_distribution": [
    {"event_type": "text_changed", "count": 18, "pct": 0.53},
    {"event_type": "tts_regenerated", "count": 8, "pct": 0.24}
    // ...
  ],
  "task_table": [
    {
      "job_id": "...",
      "user_email": "...",
      "display_name": "...",
      "status": "succeeded",
      "source_duration_minutes": 68.4,
      "total_segments": 107,
      "smart_handoff_reason": null,
      "forced_dsp_pct": 0.14,
      "entered_editing": true,
      "edit_event_count": 5,
      "created_at": "2026-05-18T04:14:37Z",
      "cost_view_url": "/admin/jobs/{job_id}/cost"
    }
    // ... up to N rows, paginated if needed
  ]
}
```

### `GET /api/admin/smart-analytics/csv`

Query params: 同 summary。

Response (200, `Content-Type: text/csv; charset=utf-8`):
返回 task_table 的全部行，CSV 格式，UTF-8 BOM 保证 Excel 正确显示中文。

列：`job_id, user_email, display_name, status, source_duration_minutes, total_segments,
smart_handoff_reason, forced_dsp_pct, dsp_pct, direct_pct, manual_review_segments,
entered_editing, edit_event_count, created_at`

---

## 4. 前端页面结构

### 路由 + 文件
- 路由：`/admin/smart-analytics`
- 文件：`frontend-next/src/app/(app)/admin/smart-analytics/page.tsx`
- 侧边栏入口：app-shell.tsx 「管理」分组新增「**智能版监控**」（用户已确认文案）

### 布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ 智能版监控                                       [刷新] [导出 CSV]    │
│ 时间窗：[最近30天 ▼]  状态：[全部 ▼]  用户：[全部 ▼]                │
├──────────────────────────────────────────────────────────────────────┤
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│ │ 总任务数  │ │Handoff率 │ │ 平均强制 │ │ 用户返工 │                 │
│ │ 19       │ │ 11%      │ │  DSP     │ │ 率 26%   │                 │
│ │ ✓15 ✗4   │ │ top: ... │ │ 18.5%    │ │ 平均3.2段│                │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘                 │
├──────────────────────────────────────────────────────────────────────┤
│ [Handoff 分布] [对齐质量] [用户返工]                                  │
│                                                                       │
│ ... 当前 Tab 内容 ...                                                │
├──────────────────────────────────────────────────────────────────────┤
│ 所有 Smart 任务表（点击行进 /admin/jobs/{id}/cost）                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Tab 1: Handoff 分布

- 表格：reason_code | 次数 | 占比 | 示例任务（最多 2 个 job_id link）
- 饼图：reason_code 占比（recharts `<PieChart>`），失败和成功不混在一起，分两个饼图
- 工具栏：无（已被顶部全局筛选覆盖）

### Tab 2: 对齐质量分布

- 表格（按 forced_dsp_pct 降序）：所有 succeeded smart 任务
- 列：job_id, 时长, 段数, 直接 %, DSP %, 强制DSP %, 短段保护 %, 复查段数
- 行点击：跳 `/admin/jobs/{id}/cost`
- 散点图：x = source_duration_minutes, y = forced_dsp_pct（recharts `<ScatterChart>`）
  - 用于检测「长视频是否更容易强制 DSP」假设

### Tab 3: 用户返工

- 两个并列表格：
  - 左：按用户聚合（user_email | smart 任务数 | 进 editing | 返工率 | 平均改段数）
  - 右：修改事件类型分布（event_type | 次数 | 占比）
- 不画图（条目少，表格够清楚）

### 完整任务表（底部）

跨所有 Tab 始终展示：
- 列：job_id, 用户, 时长, 状态, 强制DSP%, handoff_reason, 进 editing, 修改事件数, 创建时间
- 行点击：跳 `/admin/jobs/{id}/cost`
- 按 created_at desc

---

## 5. Sub-PR 拆分

| PR | 内容 | 工作量 | 价值 |
|---|---|---|---|
| **PR-1** | 后端 `gateway/admin_smart_analytics_api.py` 双 endpoint + TDD | 1.5 天 | 数据可拉取，curl 验证 |
| **PR-2** | 前端骨架 + KPI 卡片 + Tab 1（Handoff）+ 1 饼图 | 1 天 | 第一可见看板 |
| **PR-3** | Tab 2（Alignment）+ 散点图 | 1 天 | 看到质量分布 |
| **PR-4** | Tab 3（Rework）+ 用户聚合 + 完整任务表 | 1 天 | 看到返工模式 |
| **PR-5** | 时间/状态/用户筛选 + CSV 导出 + 侧边栏入口 + 部署 | 0.5 天 | 上线投产 |

**总计 5 天**，每个 PR 独立 ship。

---

## 6. Acceptance Criteria

v1 上线条件（PR-5 后）：

### 后端
- [ ] `GET /api/admin/smart-analytics/summary?days=30` 返回 19 个真实任务的聚合
- [ ] `GET /api/admin/smart-analytics/csv?days=30` 返回 CSV 文件，Excel 能正确显示中文
- [ ] 非 admin 访问返回 401/403
- [ ] alignment_report.txt 解析覆盖 5 类（直接/DSP/重写直接/重写DSP/强制DSP/短段保护）
- [ ] smart_decisions.jsonl 解析能识别所有当前出现过的 reason_code
- [ ] user_edit_events.jsonl 解析能识别 5 种 event_type
- [ ] TDD 覆盖：合成 fixture 目录 + 解析单元测试 + 集成测试（一个 fake project dir 跑全聚合）

### 前端
- [ ] `/admin/smart-analytics` 渲染 4 KPI 卡 + 3 Tab + 底部任务表
- [ ] 顶部筛选：日期窗 / 状态 / 用户（status / user 默认全部）
- [ ] Tab 1 饼图 + 表格联动
- [ ] Tab 2 散点图 + 表格联动
- [ ] Tab 3 双表格
- [ ] CSV 导出按钮触发下载
- [ ] 侧边栏「管理」组出现「智能版监控」入口（admin 可见）
- [ ] 行点击跳 `/admin/jobs/{id}/cost`

### 部署
- [ ] gateway 镜像重建 + recreate
- [ ] next 镜像重建 + recreate
- [ ] 生产环境 admin 账号访问验证

---

## 7. 失败 / Reason Code 词表（PR-1 实现依据）

从历史 19 个任务的 `smart_decisions.jsonl` 抽样确认实际出现过的 reason_code：

| reason_code | 来源 stage | v1 spec 状态 |
|---|---|---|
| `uncertain_speaker_share` | translation_review | 2026-05-20 已改成 audit-only，新任务不再出现 |
| `glossary_preservation` | translation_review | 同上 |
| `reused_user_voice` | voice_clone | 正常状态，不是 handoff |
| `clone_succeeded` | voice_clone | 正常状态，不是 handoff |
| 其他 | 各 stage 早期失败 | 历史数据 |

特殊状态：
- `0_handoff_succeeded` — 完全跑通的成功任务，handoff 表用此 reason 占位（合成的，非来自数据）
- `pipeline_failed` — status=failed 但无 smart_state.reason 的任务

PR-1 实现的解析逻辑：

```
def classify_smart_outcome(job_row, smart_decisions: list) -> str:
    if job_row.status == "failed":
        return "pipeline_failed_" + (job_row.error_summary.error_type or "unknown")
    if job_row.status == "succeeded":
        # smart_state.reason 在 7aa0abc 之后会保留 handoff 历史
        # （即使 status=completed 也可能 reason 非空）
        reason = (job_row.smart_state or {}).get("reason")
        if reason:
            return reason  # 历史 handoff 但用户手动通过
        return "0_handoff_succeeded"
    return "in_flight"
```

---

## 8. Edge Cases / 备忘

1. **任务无 project_dir**：跳过，不进任何聚合
2. **project_dir 存在但 alignment_report.txt 缺失**：alignment 字段全 None，task_table 仍显示
3. **smart_decisions.jsonl 缺失**：默认无 handoff，按 status 推断
4. **user_edit_events.jsonl 缺失**：edit_event_count=0，entered_editing 仍按 jobs.status='editing' 或 jobs.edit_generation>0 判断
5. **user.email 缺失（trial 用户）**：fallback 到 user_id 前 8 字符
6. **alignment_report.txt 旧格式**：解析失败时 graceful 降级，不抛异常
7. **CSV 导出大量数据**：v1 不分页，N=365 days 时假设 < 1000 任务（远低）

---

## 9. Out of Scope（明确后续）

- 实时刷新 / WebSocket：v1 手动「刷新」按钮，不做 long-polling
- 历史回溯 backfill：v1 只看现有 audit 数据，不补打点
- 多用户横向对比：v1 是按 user 聚合表，不画用户对比图
- 阈值校准建议：v1 只展示数据，不主动建议「调到 X」
- Cost 分析：已有 `/admin/jobs/{id}/cost` 单任务页，v1 不重复
- alignment_report.txt 旧格式（如果有）：v1 假设当前格式稳定

---

## 10. 当 design 错时

如 PR-1 实现时发现新 reason_code 或字段不全，**直接更新本文档 + 同 commit 落库**，不写新 plan。
单一真源原则。
