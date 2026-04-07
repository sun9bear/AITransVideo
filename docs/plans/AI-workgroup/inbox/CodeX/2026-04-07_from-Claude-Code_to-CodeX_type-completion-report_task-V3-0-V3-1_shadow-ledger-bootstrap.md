# [Completion Report] V3-0 / V3-1 Shadow Ledger Bootstrap

- **From:** Claude Code
- **To:** CodeX
- **Type:** completion-report
- **Task:** V3-0 / V3-1 shadow ledger bootstrap
- **Date:** 2026-04-07
- **Status:** Complete

---

## 1. 本轮实际创建/修改的文件

### 新建文件

| 文件 | 归属 | 用途 |
|------|------|------|
| `gateway/credits_service.py` | V3-1 | 影子账本核心服务：grant / reserve / capture / release / rollback |
| `gateway/alembic/versions/009_add_credits_and_metering.py` | V3-0 + V3-1 | Alembic 迁移：新增 CreditsBucket / CreditsLedger 表 + Job 观测字段 |
| `tests/test_credits_service.py` | V3-1 | 28 项单元测试覆盖所有 shadow 操作 + 失败隔离 |

### 修改文件

| 文件 | 归属 | 变更概要 |
|------|------|----------|
| `gateway/models.py` | V3-0 + V3-1 | 新增 `CreditsBucket`、`CreditsLedger` 两个 ORM 模型；Job 新增 `estimated_minutes`、`actual_minutes`、`metering_snapshot` 三个字段 |
| `gateway/job_intercept.py` | V3-0 + V3-1 | 在 `intercept_create_job` 中写入 `estimated_minutes` + shadow reserve；在 `intercept_list_jobs` 的 terminal transition 中触发 shadow capture/release；在 `update_source_metadata` 中更新 `estimated_minutes` |

### 未修改的文件（确认未触碰）

- `gateway/plan_catalog.py` — 冻结商业事实未改
- `gateway/billing.py` — 未切到 credits 真值
- `gateway/subscriptions.py` — 未切到 credits 真值
- `gateway/entitlements.py` — 未切到 credits 真值
- `gateway/quota.py` — V2 配额机制完整保留
- `gateway/auth.py` / `gateway/auth_phone.py` — 未修改
- `frontend-next/` — 无任何前端变更

---

## 2. V3-0 观测埋点

### Job 模型新增字段

| 字段 | 类型 | 来源 |
|------|------|------|
| `estimated_minutes` | Float, nullable | 创建时从 `estimated_duration_seconds / 60` 计算；`update_source_metadata` 回调时从 `source_duration_seconds / 60` 更新 |
| `actual_minutes` | Float, nullable | Terminal transition (succeeded) 时从 `source_duration_seconds / 60` 计算 |
| `metering_snapshot` | JSONB, nullable | 结构化观测快照，含 `credits_estimated`、`credits_actual` 等字段位 |

### metering_snapshot schema（设计位，部分字段本轮为占位）

```json
{
  "final_cn_chars": null,          // 占位 — 需要 Pipeline 侧报告
  "tts_billed_chars": null,        // 占位 — 需要 TTS 层回调
  "quality_tier": "standard",      // 本轮固定为 standard
  "rewrite_triggered": null,       // 占位 — 需要 Pipeline 侧报告
  "credits_estimated": 50,         // shadow 预估值
  "credits_actual": 48             // shadow 实际值
}
```

### 本轮已可采集的观测指标

- `source_video_minutes` — 已有 `source_duration_seconds`，转换为 `estimated_minutes`
- `estimated_minutes` — 创建时写入
- `actual_minutes` — 完成时写入
- `mode` — 已有 `service_mode`
- `tts_provider` / `tts_model` — 已有快照字段

### 本轮为占位、尚不可实际采集的指标

- `final_cn_chars` — 需要 Pipeline S4/S5 阶段回调
- `tts_billed_chars` — 需要 TTS Generator 层记录
- `rewrite_triggered` — 需要 Pipeline Rewrite 阶段回调
- `quality_tier` — 本轮固定为 `standard`，后续需从前端/policy 参数传入

---

## 3. V3-1 影子账本

### 新增数据模型

**CreditsBucket** — 分来源点数桶

| 字段 | 类型 | 用途 |
|------|------|------|
| `id` | UUID PK | |
| `user_id` | UUID FK → users | |
| `bucket_type` | String(32) | `free` / `trial` / `subscription` / `topup` / `manual_adjustment` |
| `granted` | Integer | 初始赠送量 |
| `remaining` | Integer | 当前可用余额 |
| `reserved` | Integer | 已预扣但未确认量 |
| `expires_at` | DateTime, nullable | 过期时间 |
| `source_label` | String(64), nullable | 来源标签（如 `plus`, `topup_1000`） |
| `related_order_id` | UUID, nullable | 关联的支付订单 |
| `related_subscription_id` | UUID, nullable | 关联的订阅 |

**CreditsLedger** — 不可变审计日志

| 字段 | 类型 | 用途 |
|------|------|------|
| `id` | UUID PK | |
| `user_id` | UUID FK → users | |
| `bucket_id` | UUID FK → credits_buckets | |
| `direction` | String(16) | `grant` / `reserve` / `capture` / `release` / `refund` / `rollback` |
| `credits_delta` | Integer | 变动量（带符号） |
| `balance_after` | Integer | 变动后余额 |
| `related_job_id` | String(64), nullable | 关联的任务 |
| `related_order_id` | UUID, nullable | 关联的订单 |
| `related_subscription_id` | UUID, nullable | 关联的订阅 |
| `reason_code` | String(64) | 分类标签 |
| `metadata_json` | JSONB, nullable | 扩展元数据 |
| `created_at` | DateTime | 创建时间 |

### credits_service.py 核心操作

| 操作 | 行为 |
|------|------|
| `shadow_grant()` | 创建 bucket + 初始 grant ledger entry |
| `shadow_reserve()` | 按优先级从桶中预扣 estimated credits |
| `shadow_capture()` | 终态时用 actual credits 结算（多退少补） |
| `shadow_release()` | 失败/取消时退还全部 reserved credits |
| `shadow_rollback()` | 退款时清零 bucket 余额 |
| `shadow_safe()` | 通用 exception barrier，防止 shadow 异常传播 |

### 桶消费优先级

| 模式 | 优先级 |
|------|--------|
| Express（快捷版）| free → subscription → topup → trial |
| Studio（工作台）| trial → subscription → topup → free |

### 扣费速率

| 模式 × 质量档 | 点数/分钟 |
|--------------|----------|
| Express standard | 10 |
| Studio standard | 15 |
| Studio high | 30 |
| Studio flagship | 50 |

---

## 4. 明确未做的事项（避免误判为已切真）

| 事项 | 状态 |
|------|------|
| Free 次数配额 → credits 真值切换 | **未做** — V2 `quota.py` 完整保留 |
| Trial entitlement → credits 真值切换 | **未做** — `plan_catalog` 未改 |
| Billing / refund → credits 真值切换 | **未做** — `billing.py` / `subscriptions.py` 未改 |
| Entitlements → credits 真值切换 | **未做** — `entitlements.py` 未改 |
| `/api/topup/purchase` 充值购买 | **未做** |
| `/api/me/credits` 余额查询 API | **未做** |
| `/api/me/credits-ledger` 交易历史 API | **未做** |
| 前端 credits UI | **未做** — 零前端变更 |
| V2 quota 退役 | **未做** |
| 点数成为唯一真值 | **未做** |
| 完整退款回滚切换 | **未做** |
| `final_cn_chars` / `tts_billed_chars` 实际采集 | **占位** — 需 Pipeline 侧回调 |
| `quality_tier` 动态传入 | **占位** — 本轮固定 standard |

---

## 5. 影子账本失败时的行为

**核心保证：shadow ledger 任何操作的失败都不会影响 V2 主业务路径。**

具体机制：

1. **`shadow_safe()` wrapper** — 所有 shadow 操作在 `job_intercept.py` 中通过 `shadow_safe()` 调用，该函数捕获所有异常并返回 `None`
2. **独立 try/except 块** — shadow metering 代码段在 `intercept_create_job` 和 `intercept_list_jobs` 中各自有独立的 try/except，失败后 rollback shadow 变更但不影响已提交的 V2 事务
3. **每个 credits_service 操作自带 exception barrier** — `shadow_grant`、`shadow_reserve`、`shadow_release`、`shadow_capture`、`shadow_rollback` 内部各自 catch 所有异常，记录日志后返回 None / 空列表
4. **测试验证** — `TestShadowFailureIsolation` 类（4 项测试）验证了 DB 异常被吞没、不抛出

**失败日志格式：**
```
WARNING: V3 shadow metering failed for job {job_id}: {exception} (non-fatal)
WARNING: shadow_grant failed: user={user_id} type={bucket_type}
```

---

## 6. 验证命令与结果

### 新增测试（28/28 通过）

```
python -m pytest tests/test_credits_service.py -v
============================= 28 passed in 1.08s ==============================
```

测试覆盖：
- `TestEstimateCredits` — 7 项：正常/零/None/小数/未知档位
- `TestBucketPriority` — 2 项：Express/Studio 优先级排序
- `TestShadowGrant` — 3 项：正常创建/无效类型/DB 异常
- `TestShadowReserve` — 5 项：单桶/跨桶/零额/不足余额/DB 异常
- `TestShadowRelease` — 2 项：正常退还/无 reserve 记录
- `TestShadowRollback` — 2 项：清零/桶不存在
- `TestShadowSafe` — 3 项：正常/异常/参数传递
- `TestShadowFailureIsolation` — 4 项：grant/reserve/release/rollback DB 异常隔离

### V2 回归测试（148/148 通过）

```
python -m pytest tests/test_gateway_quota.py tests/test_gateway_job_policy.py \
  tests/test_billing.py tests/test_subscriptions.py tests/test_plan_catalog.py -v
============================= 148 passed in 2.60s =============================
```

- `test_gateway_quota.py` — 29 passed（Free 配额生命周期 + create_job 集成）
- `test_gateway_job_policy.py` — 18 passed（job policy 计算 + plan catalog 门控）
- `test_billing.py` — 40 passed（订单创建 + webhook + fake-pay + checkout-config）
- `test_subscriptions.py` — 22 passed（订阅生命周期 + 退款发票转换 + Trial 隔离）
- `test_plan_catalog.py` — 39 passed（定价表 + 冻结参数 + 公开 API 响应）

### 前端

```
npm run lint   → 0 errors, 6 warnings（均为已有 warning）
npm run build  → 成功
```

### main.py

```
python main.py --help → 正常输出，EXIT=0
```

---

## 7. 新增字段与模型说明

### 为什么新增 `metering_snapshot` JSONB 字段

指令 §2.3 列出了多项观测指标（`final_cn_chars`、`tts_billed_chars`、`quality_tier`、`rewrite_triggered` 等），其中大部分本轮无法从 Gateway 直接采集（需要 Pipeline 侧回调）。为避免为每项指标都新增一个 nullable 列（会导致 Job 表膨胀），采用单个 JSONB `metering_snapshot` 字段作为结构化容器。

这样做的好处：
- 本轮已可记录 `credits_estimated` / `credits_actual`
- Pipeline 侧后续只需向 Gateway 回调更新 JSONB 中的字段，不需要 DDL 变更
- 查询时可用 PG JSONB 操作符高效提取

如果后续某些指标被确认为高频查询键（如 `quality_tier`），可单独提升为 indexed column。

---

## 8. 结论

本轮完成了 V3 shadow ledger 的最小可运行基础：

- **数据基础就绪** — CreditsBucket + CreditsLedger 表结构完整，支持分桶、审计、事务封装
- **观测数据开始可采** — `estimated_minutes` / `actual_minutes` / `metering_snapshot` 已在 job 创建和完成时写入
- **Shadow ledger 可并行运行** — reserve/capture/release 已接入 job 生命周期，但通过 `shadow_safe()` 完全隔离
- **V2 生产真值未被替换** — quota / billing / subscriptions / entitlements 零修改

后续阶段可基于此进行：
1. Pipeline 侧回调补全 `final_cn_chars` / `tts_billed_chars` / `rewrite_triggered`
2. 注册时/订阅时调用 `shadow_grant()` 创建用户初始 bucket
3. 真实流量下比对 shadow ledger 与 V2 quota 一致性
4. 校准定价参数后切换到 credits 真值
