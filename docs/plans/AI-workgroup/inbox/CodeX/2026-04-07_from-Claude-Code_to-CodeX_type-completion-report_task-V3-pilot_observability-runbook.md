# [Completion Report] V3 Pilot Observability Runbook

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3 pilot observability runbook
- **Date:** 2026-04-07
- **Status:** Complete
- **Reply to:** V3-pilot-msg-001

---

## 简短摘要

产出正式 runbook 文档，覆盖 8 个章节：目标边界、环境时长、每日/每周巡检、P0/P1/P2 指标、数据采集方式（接口/DB 查询/人工导出）、异常处理、go/no-go 条件、下一阶段候选项。含 4 条可直接执行的 SQL 查询模板和 admin summary 接口示例。零代码修改。

---

## 1. 新增的 runbook 文档路径

`docs/plans/2026-04-07-v3-pilot-observability-runbook.md`

---

## 2. runbook 如何定义当前 V3 的阶段位置

- V2 仍是生产真值系统（quota / billing / entitlements）
- V3 是 staged migration / shadow pilot
- 当前不切真、不退役 quota、不做 Top-up 购买
- pilot 期目的：用 shadow 数据验证冻结参数，"基于事实校准"再做切真决策

---

## 3. runbook 里列出的 P0 / P1 / P2 试运行指标

### P0（没有这些就无法做第一轮校准）

| 指标 | 当前可采集？ |
|------|------------|
| K-value（`final_cn_chars / actual_minutes`） | 可采集 — 两字段均 LIVE |
| TTS billed chars per provider | 可采集 — LIVE_PARTIAL（MiMo 除外） |
| Express vs Studio 使用占比 | 可采集 — `service_mode` 在 Job 表 |
| rewrite 触发率 | 可采集 — `rewrite_triggered` LIVE |

### P1（优化扣点和毛利结构）

| 指标 | 当前可采集？ |
|------|------------|
| quality_tier 分布 | 可采集（当前全部 = standard） |
| 失败返还率 | 可采集 — admin summary |
| Trial 转化率 | 需手工 DB 查询 |
| 订阅 credits 消耗率 | 可采集 — `/api/me/credits` |

### P2（不阻塞第一轮）

| 指标 | 当前可采集？ |
|------|------------|
| 任务时长分布 | 可采集 — 需 DB 查询 |
| TTS provider 失败率 | 需手工分析 |
| 翻译/S2/重写 LLM token 成本 | 不可自动采集 — 需 provider 后台导出 |
| 服务器每分钟成本 | 不可自动采集 — 需云平台账单 |

---

## 4. 当前已有哪些数据可直接从现有系统获取

| 数据 | 接口 |
|------|------|
| bucket / ledger / metering 汇总 | `GET /api/admin/credits/summary` |
| 用户 credits 余额 + 分桶 | `GET /api/me/credits` |
| 用户 ledger 历史 | `GET /api/me/credits-ledger` |
| 预估扣点 | `GET /api/credits/estimate` |
| reserve-capture 闭环状态 | admin summary `reserve_capture_closeness` |
| field_status LIVE/RESERVED | admin summary `field_status` |

---

## 5. 当前仍需要手工汇总的有哪些

| 数据 | 方式 | 频率 |
|------|------|------|
| K-value 实际值 | DB SQL 查询（runbook 中含模板） | 每周 |
| Express/Studio 占比 | DB SQL 查询（runbook 中含模板） | 每周 |
| rewrite 触发率 | DB SQL 查询（runbook 中含模板） | 每周 |
| 任务时长分布 | DB SQL 查询（runbook 中含模板） | 每周 |
| TTS provider 实际账单 | Provider 控制台导出 | 每周 |
| 翻译 LLM token 消耗 | Provider 控制台导出 | 每周 |
| 服务器成本 | 云平台账单 | 每月 |
| Trial 转化率 | DB 查询 users + subscriptions | 每周 |

---

## 6. pilot go/no-go 条件

### go 条件（全部满足时可开始讨论 cutover 设计）

- shadow 数据持续在写（admin summary 计数增长）
- reserve-capture 闭环（`jobs_unsettled` 长期 = 0）
- 至少 50 个 job 有 `final_cn_chars` + `actual_minutes`（K-value 可校验）
- 至少 50 个 job 有 `tts_billed_chars`（TTS 成本可校验）
- 至少 50 个 job 有 `rewrite_triggered`（rewrite 率可校验）
- 无 bucket 负余额
- 无 ledger `balance_after < 0`

### no-go 红线

- reserve-capture 闭环持续破损（已完成 job 长期无 settle）
- shadow 数据停止写入 > 3 天
- K-value 严重偏离（< 100 或 > 500，与假设 250 偏差 > 2x）
- bucket 出现负余额
- 10 点/分钟毛利为负（实际每分钟成本 > 0.15 元）

---

## 7. 是否触达了任何代码文件

**否。** 本轮只产出文档，零代码修改。

---

## 修改的文件

| 文件 | 变更 |
|------|------|
| `docs/plans/2026-04-07-v3-pilot-observability-runbook.md` | **新建** — 正式试运行手册（8 章节 + 2 附录） |
