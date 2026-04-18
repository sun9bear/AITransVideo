# V3 Pilot Observability Runbook

> 状态：可执行试运行手册
> 时间：2026-04-07
> 适用阶段：V3 shadow pilot（V3-0 ~ V3-6 已完成，V2 仍是生产真值）
> 目标：指导 2-4 周试运行期的数据采集、健康度巡检、异常处理和阶段评审

---

## 1. 目标与边界

### 1.1 为什么要先做 pilot

V3 点数体系的冻结参数（`K=250 cn_chars/min`、`10 点/分钟`、`Plus=3500` 等）目前是基于成本模型推算的估值，未经真实流量验证。如果直接切换真值，可能出现：

- 扣点锚点与真实成本偏差过大
- 用户体感与预期不符
- 商业毛利被高估或低估

pilot 期的目的是：用 shadow 数据验证这些假设，然后"基于事实校准"再做切真决策。

### 1.2 当前不做什么

- **不做** credits truth cutover（V2 quota/billing/entitlements 继续是生产真值）
- **不做** Top-up 充值购买流程
- **不做** V2 quota 退役
- **不做** 完整退款回滚产品化
- **不做** WeChat Pay
- **不做** 音色克隆计费

### 1.3 V2 / V3 真值边界

| 层 | 真值系统 | V3 角色 |
|----|---------|---------|
| 任务是否可执行 | V2 quota + plan gate | 不参与 gating |
| 用户套餐/权限 | V2 entitlements | 不参与判定 |
| 支付/订阅 | V2 billing + subscriptions | shadow grant 跟随 |
| 点数余额 | V3 shadow buckets | 只读展示 |
| 扣点/账本 | V3 shadow ledger | 只读展示 + 观测 |
| 费用预估 | V3 estimate API | 只读展示 |

---

## 2. 适用环境与建议时长

### 2.1 部署顺序

1. **Staging 环境**：先部署 V3 代码（含 migration 009），验证 shadow 数据能正常写入和读取
2. **Production 环境**：Staging 验证通过后部署到生产，开始真实流量的 shadow 采集

### 2.2 建议试运行时长

**2-4 周**，分两个阶段：

- **第 1 周**：每日巡检，确认 shadow 数据在写入、无系统异常
- **第 2-4 周**：每周汇总核心指标，积累足够数据量

### 2.3 灰度

当前不需要灰度。V3 shadow 数据写入对 V2 主路径完全透明（`shadow_safe()` 隔离），所有用户同时进入 shadow pilot。

---

## 3. 每日 / 每周观察项

### 3.1 每日巡检清单（第 1 周必做，之后可降频）

**接口：** `GET /api/admin/credits/summary`（需 admin 登录）

| 检查项 | 看什么 | 正常标准 |
|--------|--------|----------|
| bucket 写入 | `buckets[].count > 0` | 至少有 free 类型 bucket |
| ledger 写入 | `ledger.total_entries > 0` | 有新增 grant/reserve 记录 |
| reserve-capture 闭环 | `reserve_capture_closeness.jobs_unsettled` | 应 = 0 或只有当前进行中的 job |
| metering 覆盖 | `metering.with_estimated_minutes` / `total_jobs` | 新任务应 > 0 |
| 无 reserve 泄露 | `reserve_capture_closeness.unsettled_job_ids_sample` | 列表中不应有已完成的 job |

**操作：** 用浏览器或 `curl` 访问 admin summary，肉眼检查上述字段。

### 3.2 每周汇总（第 2 周开始）

每周末手工记录以下数据到一份简单表格（建议用 Excel 或 Notion）：

| 指标 | 数据来源 | 记录方式 |
|------|----------|----------|
| 本周新增 job 数 | admin summary `metering.total_jobs` 本周增量 | 手工差值 |
| 有 estimated_minutes 的 job 数 | `metering.with_estimated_minutes` | 直接读 |
| 有 actual_minutes 的 job 数 | `metering.with_actual_minutes` | 直接读 |
| 有 credits_estimated 的 job 数 | `metering.with_credits_estimated` | 直接读 |
| 有 credits_actual 的 job 数 | `metering.with_credits_actual` | 直接读 |
| 有 final_cn_chars 的 job 数 | 需 DB 查询（见 §5） | 手工查询 |
| reserve-capture 闭环状态 | `reserve_capture_closeness` | 直接读 |
| 各 bucket 类型 remaining 汇总 | `buckets[]` | 直接读 |

---

## 4. 核心试运行指标

### 4.1 P0 指标：没有这些就无法做第一轮校准

| 指标 | 冻结假设 | 数据来源 | 当前可采集？ |
|------|----------|----------|------------|
| `K_cn_chars_per_src_min_actual` | 250 | `final_cn_chars / actual_minutes` 按 job 聚合 | **可采集** — 两个字段均为 LIVE |
| 实际 TTS billed chars | 按 provider 2x/1x | `tts_billed_chars` per job | **可采集（LIVE_PARTIAL）** — MiMo 除外 |
| Express vs Studio 使用占比 | — | `service_mode` per job（Job 表已有字段） | **可采集** — 直接查 DB |
| rewrite 触发率 | 30% 估算 | `rewrite_triggered` per job | **可采集** — LIVE |

### 4.2 P1 指标：有了这些才能优化扣点和毛利结构

| 指标 | 数据来源 | 当前可采集？ |
|------|----------|------------|
| quality_tier 分布 | `metering_snapshot.quality_tier` | **可采集** — 当前全部 = standard |
| 失败返还率 | ledger `direction=release` count / total reserves | **可采集** — admin summary |
| Trial 转化率 | `users.trial_granted_at` + `subscriptions` | **需手工 DB 查询** |
| 订阅 credits 消耗率 | bucket remaining vs granted | **可采集** — `/api/me/credits` |

### 4.3 P2 指标：有助于第二轮优化，不阻塞第一轮

| 指标 | 数据来源 | 当前可采集？ |
|------|----------|------------|
| 任务时长分布 (P50/P75/P90) | `actual_minutes` per job | **可采集** — 需 DB 查询 |
| TTS provider 失败率 | job error_summary | **需手工分析** |
| 翻译/S2/重写 LLM token 成本 | Pipeline 日志 / provider 账单 | **当前不可自动采集** — 需人工从 provider 后台导出 |
| 服务器每分钟成本 | 云账单 | **需人工从云平台导出** |

---

## 5. 数据采集方式

### 5.1 已有接口可直接提供

| 数据 | 接口 | 说明 |
|------|------|------|
| bucket / ledger / metering 汇总 | `GET /api/admin/credits/summary` | admin-only，返回 JSON |
| 用户 credits 余额 + 分桶 | `GET /api/me/credits` | 按用户，前端已展示 |
| 用户 ledger 历史 | `GET /api/me/credits-ledger` | 按用户 |
| 预估扣点 | `GET /api/credits/estimate?minutes=X&service_mode=Y` | 公开，无需登录 |

### 5.2 需要手工 DB 查询

以下指标需要直接查 PostgreSQL。建议用 `psql` 或数据库管理工具执行：

**K-value 实际值：**
```sql
SELECT
  AVG((metering_snapshot->>'final_cn_chars')::float / NULLIF(actual_minutes, 0)) AS k_actual_avg,
  PERCENTILE_CONT(0.5) WITHIN GROUP (
    ORDER BY (metering_snapshot->>'final_cn_chars')::float / NULLIF(actual_minutes, 0)
  ) AS k_actual_p50
FROM jobs
WHERE actual_minutes > 0
  AND metering_snapshot->>'final_cn_chars' IS NOT NULL;
```

**Express vs Studio 占比：**
```sql
SELECT service_mode, COUNT(*) AS count,
  ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100, 1) AS pct
FROM jobs
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY service_mode;
```

**Rewrite 触发率：**
```sql
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE (metering_snapshot->>'rewrite_triggered')::boolean = true) AS with_rewrite,
  ROUND(
    COUNT(*) FILTER (WHERE (metering_snapshot->>'rewrite_triggered')::boolean = true)::numeric
    / NULLIF(COUNT(*), 0) * 100, 1
  ) AS rewrite_rate_pct
FROM jobs
WHERE metering_snapshot IS NOT NULL;
```

**任务时长分布：**
```sql
SELECT
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY actual_minutes) AS p50,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY actual_minutes) AS p75,
  PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY actual_minutes) AS p90
FROM jobs
WHERE actual_minutes > 0;
```

### 5.3 需要人工从外部系统采集

| 数据 | 来源 | 频率 |
|------|------|------|
| TTS provider 实际账单 | MiniMax / 阿里云 / 火山引擎 控制台 | 每周一次 |
| 翻译 LLM 实际 token 消耗 | Gemini / DeepSeek 控制台 | 每周一次 |
| 服务器成本 | 云平台账单 | 每月一次 |

---

## 6. 异常 / 告警处理

### 6.1 reserve 无 capture/release

**表现：** admin summary `reserve_capture_closeness.jobs_unsettled > 0` 且 `unsettled_job_ids_sample` 包含已完成的 job。

**排查步骤：**
1. 检查 `unsettled_job_ids_sample` 中的 job 当前状态
2. 如果 job 状态是 `running` / `queued` / `waiting_for_review` — 正常（尚未终态）
3. 如果 job 状态是 `succeeded` / `failed` / `cancelled` 但无 capture/release — 异常
4. 查 Gateway 日志中是否有 `V3 shadow settle failed` 警告

**处理：** 不影响 V2 生产。记录异常 job ID，分析 shadow settle 失败原因。

### 6.2 metering 关键字段为空

**表现：** `metering.with_credits_estimated` 远低于 `total_jobs`。

**排查步骤：**
1. 检查是否有 job 在 V3 代码部署前创建（这些 job 不会有 shadow 数据）
2. 检查 Gateway 日志中是否有 `V3 shadow metering failed` 警告
3. 检查 `estimated_duration_seconds` 是否为 NULL（如果前端未传且 yt-dlp probe 失败）

**处理：** 部署前的 job 无 shadow 数据是预期行为。部署后的 job 如果持续无 shadow 数据，需排查 Gateway 代码路径。

### 6.3 bucket / ledger 明显不一致

**表现：** bucket `remaining` 为负数，或 ledger `balance_after` 出现负值。

**排查步骤：**
1. 查具体 user 的 bucket 和 ledger 记录
2. 检查是否有并发 reserve 导致的 race condition
3. 检查 `shadow_capture` 是否在 `actual > reserved` 场景下正确执行了 additional debit

**处理：** 不影响 V2 生产。记录异常 user/bucket，分析 credits_service 逻辑。

---

## 7. 阶段性评审与 go/no-go

### 7.1 pilot 完成条件（建议 2-4 周后评审）

**go 条件 — 以下全部满足时，可以开始讨论 credits truth cutover 设计：**

| 条件 | 判定标准 |
|------|----------|
| shadow 数据持续在写 | admin summary 各计数持续增长 |
| reserve-capture 闭环 | `jobs_unsettled` 长期 = 0（排除进行中 job） |
| K-value 可校验 | 至少 50 个 job 有 `final_cn_chars` + `actual_minutes` |
| TTS billed chars 可校验 | 至少 50 个 job 有 `tts_billed_chars` |
| rewrite 触发率可校验 | 至少 50 个 job 有 `rewrite_triggered` |
| 无 bucket 负余额 | 所有 bucket `remaining >= 0` |
| 无 ledger 异常 | 无 `balance_after < 0` 的记录 |

**no-go 红线 — 以下任一出现时，不应推进 cutover：**

| 红线 | 说明 |
|------|------|
| reserve-capture 闭环持续破损 | 已完成 job 长期无 capture/release |
| shadow 数据停止写入 | admin summary 计数停滞超过 3 天 |
| K-value 严重偏离 | 实际 K < 100 或 K > 500（与假设 250 偏差 > 2x） |
| bucket 出现负余额 | credits_service 存在并发/逻辑 bug |
| 10 点/分钟 毛利为负 | 实际每分钟成本 > 0.15 元（= 10 × 0.015） |

### 7.2 评审输出

pilot 评审应产出一份简短的"校准报告"，至少回答 [2026-04-06-v3-pilot-observability-checklist.md §7](./2026-04-06-v3-pilot-observability-checklist.md) 中的 7 个问题：

1. `K=250` 是否准确？
2. 最低成本 TTS 池是哪组模型？
3. `1 点 ≈ 0.015 元` 是否成立？
4. `快捷版 10 点/分钟` 是否有合理毛利？
5. `工作台基础版 15 点/分钟` 是否需要调整？
6. `Plus 3500` / `Pro 12000` 是否过高或过低？
7. Top-up 定价是否合理？

---

## 8. 下一阶段候选项

pilot 完成且评审通过后，优先可能进入以下候选项之一（需项目 owner 决策）：

| 候选项 | 前提条件 | 说明 |
|--------|----------|------|
| **Credits truth cutover 设计** | pilot 数据充分 + 校准报告通过 | 设计 credits 替代 V2 quota 的切换方案 |
| **Top-up 充值购买** | cutover 方案明确后 | 构建充值包购买流程 |
| **定价参数调整** | K-value / TTS 成本与假设偏差 > 20% | 调整冻结参数 |
| **V2 quota 退役** | cutover 完成 + 充值购买上线后 | 移除旧 quota 逻辑 |

这些是"候选项"，不是直接立项实现。具体优先级由项目 owner 根据 pilot 数据决定。

---

## 附录 A：关键文件索引

| 文件 | 用途 |
|------|------|
| `gateway/credits_service.py` | shadow ledger 核心（grant/reserve/capture/release/rollback） |
| `gateway/credits_read.py` | 用户 credits 只读 API |
| `gateway/credits_observability.py` | admin summary + field_status |
| `gateway/job_intercept.py` | shadow metering 埋点（create/settle） |
| `gateway/models.py` | CreditsBucket / CreditsLedger / Job.metering_snapshot |
| `gateway/alembic/versions/009_add_credits_and_metering.py` | V3 DB migration |
| `src/pipeline/process.py` | Pipeline metering writeback（_report_job_metering） |
| `src/services/tts/tts_generator.py` | TTS billed_chars 采集 |
| `docs/plans/2026-04-06-v3-credits-ledger-and-metering-plan.md` | V3 冻结参数与成本模型 |
| `docs/plans/2026-04-06-v3-pilot-observability-checklist.md` | 试运行观测指标清单 |

## 附录 B：admin summary 接口示例

```bash
curl -s -b cookies.txt https://your-domain/api/admin/credits/summary | python -m json.tool
```

返回结构：
```json
{
  "buckets": [...],
  "ledger": { "by_direction": {...}, "total_entries": N, "recent": [...] },
  "metering": {
    "total_jobs": N,
    "with_estimated_minutes": N,
    "with_actual_minutes": N,
    "with_metering_snapshot": N,
    "with_credits_estimated": N,
    "with_credits_actual": N
  },
  "reserve_capture_closeness": {
    "jobs_with_reserve": N,
    "jobs_with_settle": N,
    "jobs_unsettled": N,
    "unsettled_job_ids_sample": [...],
    "note": "healthy / partial",
    "methodology": "set-diff: ..."
  },
  "field_status": { ... }
}
```
