# 方案：正式付费智能版克隆 600 点抵扣进分钟点（max(600, 分钟×100) 计费模型）

**状态：** IMPLEMENTED v1.6（TDD 实现已落 worktree `claude/anon-clone-enable`，commit `7e7d4e02` + advisory-lock 后续；内部 5-lens 对抗审查 0 must-fix；CodeX 三轮审计 78→82→82，三项全已修） / **下一步：CodeX 复审 → 合并审查（默认 OFF）→ 项目主休眠态部署**

> **v1.6（2026-06-15 并发硬化）：**
> - **convert 并发 advisory lock 已实现**（`_acquire_convert_singleflight_lock`，[job_intercept.py](../../gateway/job_intercept.py)）：在 pre-check **之前**取派生自 `_convert_job_id` 的事务级 `pg_advisory_xact_lock`，串行同一预览的并发 convert → 第二个在第一个 commit 后 pre-check 命中现有 F → 幂等返回、不再 forward → **关掉 transient 重跑窗口**（CodeX P2 残留）。key 用 `sha256(convert_job_id)`（跨进程稳定，禁 `hash()`）；非 PG no-op；锁顺序最早、无死锁；contention 仅限真双击。**→ 激活前 gate「并发 convert 压测**或**advisory lock」已由 advisory lock 满足。**

> **v1.5（2026-06-15 实现 + CodeX 78/100 审计回应）：**
> - **P1（cost_summary 审计落地）已修**：原 `clone_carryover_applied_credits` 只进 `Job.metering_snapshot`、未进 `smart_cost_summary.json`。现 `cost_summary_backfill.backfill_smart_cost_summary` 加 `carryover_*` 参数写 `cost_breakdown_internal_only`，`job_terminal_mirror` 从 `metering_snapshot` 读并传入 → convert 减免可审计（+2 测试）。
> - **P2（措辞精确）已修**：convert 不是「严格幂等单飞」，而是 **best-effort 单飞 + 账本 single-use 兜底**（pre-check 在 PG insert 前，真并发双击仍可能两 forward 同 job_id → 上游 worker transient 重跑；**钱仍安全**：PG job_id PK 原子回滚 loser 分钟 reserve → 一次 settle）。代码注释已更正；advisory lock 串行 check+forward 列为**翻旗激活前**硬化（§4.6 step 1.5「视压测决定」）。
> - **P3（单一来源）已修**：reserve-offset R_own 改用 `_SMART_CLONE_RESERVE_CREDITS` 常量（非裸 600）；克隆 reserve 字面量 600 保留并加交叉引用注释（既有钱-守卫钉真值）；清理 unused import `update`。
> - **激活前 gate（CodeX 建议，项目主把关）**：alembic 037/038/039 在目标环境跑完 + cost_summary 审计字段已补 ✓ + ~~并发 convert 压测或 advisory lock~~ **advisory lock 已实现 ✓（v1.6）** + 真金小额 E2E 覆盖 preview→full 与 full smart auto-clone，**之后**才翻 `smart_preview_clone_enabled`。默认 OFF inert 可先合 main。
> - **测试**：`test_p3e_dc_clone_offset.py`(20 STYLE-B 真账本 + cost_summary)、`test_p3e_dc_convert_plumbing.py`(12 AST 守卫)；广扫 1267 passed 0 回归（4 failed=process.py/whitelist 预存 drift，stash 基线证实无关）。

> **v1.4 修订（回应 CodeX 三审 82/100 的 2 小点）：**
> 1. **单飞表述更正（CodeX #1）**：`submit_job()` **无**通用 existing-check（带已存在 job_id 会覆盖+重启原 job）。改为 **Gateway pre-forward existing-check 为主**（命中 → 映射返回现有 F、不 forward），PG `job_id` 主键作后备；并发极小窗口可加 advisory lock（§4.6 步骤 1.5）。
> 2. 修掉 §11 残留的「v1.2」笔误 → v1.3/本轮。

> **v1.3 修订（回应 CodeX 复审 73/100，全在 §4.6 D-C）：**
> 1. **PG smart_state 落盘（CodeX #1）**：convert 无 reservation → 现有「只在有 reservation 才写 PG `Job.smart_state`」分支会让 marker 漏写、settle 读不到。泛化该写入，convert marker 同时落 forwarded JobRecord + PG Job。
> 2. **reservation id 权威来源（CodeX #2）**：扩展 `PreviewReuseResolution` 返回 `preview_reservation_id` + `preview_credit_amount`，结转金额取自 reservation（不写死 600）。
> 3. **convert single-flight（CodeX #3）**：现 create 无 idempotency dedup → 双击/重发产生两个收费完整任务、第二个静默全额扣分钟。改为 convert **best-effort 单飞**（确定性 job_id + Option-C 预供 + **gateway pre-forward existing-check**）+ **账本 single-use 兜底**：一预览正常收敛到一完整任务（真并发双击仍可能 transient 重跑，但钱由 single-use 保证不双扣，详见 v1.5/§4.6）；再做别的视频走普通 create 选库音色。
> 4. **cost_summary 口径（CodeX #5）**：明确 per-job 净值语义 + convert F stamp `clone_carryover_applied_credits` 使减免可审计。

> **v1.2 决策锁定（项目主 2026-06-15）：**
> - **D-D = 采用 `max(600, 分钟×100)`，替换 6/01 §12.4 旧平滑公式**（短/中任务对用户更便宜、可解释）。
> - **D-B = 做，不推迟**：create reserve + late reserve **两处都**按 offset 减少分钟 reserve，避免 over-gating（§4.5）。
> - **D-C = 现在就做**：预览→完整 convert 时，预览已扣的 600 **结转抵扣**进完整任务分钟，使「预览后转完整」与「直接做完整」一致 = `max(600, 分钟×100)`；用 **single-use carryover**（一次预览只能抵一个完整任务）防双扣（§4.6）。
> - D-A（settle-time 抵扣）、D-E（finding 1 灰度沟通）维持。

> **v1.1 修订（回应 CodeX 2026-06-15 首轮审核）：**
> 1. **平滑算法措辞**（CodeX #1）：改为「代码未实现；6/01 §12.4 文档曾有**另一套**平滑公式（从第 10 min 起 50/min 抵、最多 600）；本方案 `max(600,分钟×100)` 是**新产品决策**替换它」。新增 §3.3 两公式营收对比表。
> 2. **取消 defer，改读 `CloneBillingEvent`**（CodeX #2）：原「克隆未结算 → `return []` 推迟」会让 mirror 误判 settle 成功 → backfill 写错 `pending_credits_charged`/`settled_at`。改为 C 直接读 pipeline 阶段已写的 chargeable billing event（权威计费信号），**无顺序依赖、无 defer、不碰 backfill**（§4.2）。
> 3. **D-B 覆盖两处 reserve**（CodeX #3）：若做 over-gating 优化，必须 create reserve（[job_intercept.py:2243](../../gateway/job_intercept.py)）+ late reserve（[5757](../../gateway/job_intercept.py)）都改，不能只改一处（§5 D-B）。
**日期：** 2026-06-15
**分支：** `claude/anon-clone-enable`（worktree `D:\Claude\worktrees\anon-clone-enable`，HEAD `9f123521`，比 main +57）
**任务卡：** `task_c78819c6`（项目主 2026-06-15 提出）
**父方案 / 关联：**
- [`2026-06-14-p3-smart-clone-600-credit-subplan.md`](2026-06-14-p3-smart-clone-600-credit-subplan.md)（P3 子方案 v3 — 600 reserve→bill→settle 钱核心）
- [`2026-06-14-p3e2-preview-lane-design.md`](2026-06-14-p3e2-preview-lane-design.md)（预览 lane / smart_preview_mode 设计）
- [`2026-05-13-smart-mvp-p2-implementation-plan.md`](2026-05-13-smart-mvp-p2-implementation-plan.md) §4.3/§5.2（结算语义 + fail_and_refund 封顶语义）

> ⚠️ **本方案动真钱**：改变「正式付费智能版（含自动克隆）」用户的总扣点。算错（双扣 / 扣了不退 / 漏扣 / 退多）是严重 bug。这是 **CLAUDE.md「✅ 用户显式触发的知情付费」豁免路径**（用户登录→选智能版→勾选克隆→600 弹窗→确认），不违反付费 API 硬约束，但**必须计费正确**。批准前不写动钱代码。

---

## 1. 背景与目标

### 1.1 项目主原话（诉求）

> 「正式用户克隆音色，也扣 600 点数，然后从按分钟计算的 100 点/分钟点数总扣点数从扣除这个 600 点，可行吗？我记得之前做了个短时长智能版扣点的平滑计费算法的，短时长需要计算音色克隆的 600 点，长时长就按 100 点/分钟计算，中间的平滑过渡扣点。」

### 1.2 目标计费模型

正式付费智能版任务（**含自动 MiniMax 克隆**）的**总扣点**：

```
总扣点 = max(600, 分钟 × 100)
```

- **短任务（< 6 分钟）**：总扣 600（600 保底吸收克隆成本；分钟点被 600 吸收，等效不另收分钟）。
- **长任务（≥ 6 分钟）**：总扣 分钟×100（克隆 600 被**抵扣进**分钟点，等效不另收 600）。
- **过渡（= 6 分钟）**：两段都 = 600，**连续无跳变**（见 §3.2）。

**现状是「叠加」**：正式付费智能版 + 自动克隆 = `分钟×100 + 600`（两笔独立结算累加）。本方案把它改成「**抵扣**」= `max(600, 分钟×100)`。

### 1.3 「之前做过平滑算法」澄清（CodeX 修订）

两个层面分开说：

- **代码层**：分钟费率**纯线性**，没有任何已实现的平滑/克隆保底/抵扣逻辑（`estimate_credits` = `max(1, round(分钟×费率))`，[credits_service.py:154](../../gateway/credits_service.py)）。
- **文档层**：项目主记忆的「平滑算法」**确有出处**，但是**另一套公式**——[`2026-06-01-...-plan.md §12.4`](2026-06-01-anonymous-preview-funnel-ux-plan.md)（约 405 行）写过单调抵扣公式：`总点数 = 分钟×100 + 克隆600 − 长视频抵扣`，**从第 10 分钟起每分钟抵 50、每克隆最多抵 600、抵扣 ≤ 实际克隆点**。示例：3min=900、9min=1500、12min=1700。该公式**从未实现**（apf plan 2026-06-10:33 「平滑抵扣…全部后置」）。

→ **本方案的 `max(600, 分钟×100)` 是 2026-06-15 的新产品决策，替换（不是延续）6/01 §12.4 的旧公式。** 两者差异巨大（短/中任务旧公式贵得多，见 §3.3 对比表）——属**产品决策**，需项目主有意识确认（§5 决策 D-D 已升级为实质决策）。

> 另区分一个**不相关**的 cap 概念：`fail_and_refund` 的 `partial_capture` 封顶 = `source_minutes × studio.standard_rate`（[smart-mvp-p2 §4.3/§5.2]）。那是「**失败退款**封顶」，不是「600 抵扣分钟」，且该路径目前是 STUB + hard-gate（§2.6），不在本方案范围。

---

## 2. 扣点现状全图（逐文件核实，2026-06-15）

正式付费智能版 + 自动克隆任务，从 create → reserve → pipeline → settle 的**每一笔点数**。所有引用为 worktree `claude/anon-clone-enable` 当前态。

### 2.1 两个完全独立的 reservation 系统

| 维度 | 分钟 job reserve | 克隆 600 reserve |
|------|------------------|------------------|
| 服务模块 | `gateway/credits_service.py` | `gateway/smart_clone_reservation_service.py` |
| 金额 | `分钟 × 100`（`estimate_credits`） | `600`（`voice_clone_cost_credits`） |
| reserve `reason_code` | `job_reserve` | `smart_clone_reserve_{reservation_id}` |
| reserve `related_job_id` | `job_id` | `task_id`（= `job_id`，同值） |
| capture `reason_code` | `job_capture`（legacy）/ `smart_capture_full`（capture_full policy） | `smart_clone_capture_{reservation_id}` |
| release `reason_code` | `job_release` / `capture_excess_release` / `smart_preview_minute_release` | `smart_clone_release_{reservation_id}` |
| settle 入口 | `settle_job_credit_ledger`（[credits_service.py:1002](../../gateway/credits_service.py)） | `settle_smart_clone_reservation`（[smart_clone_reservation_service.py:505](../../gateway/smart_clone_reservation_service.py)） |
| settle 触发 | `mirror_job_terminal_state` 行 203 | `mirror_job_terminal_state` 行 151（**在分钟前**）+ TTL sweeper 兜底 |
| 状态机 | reserve→capture/release（ledger） | `reserved→captured`/`released`（`SmartCloneReservation` 表 + ledger 双写） |

**核心结论：两笔互不感知、各自结算、金额累加。** `shadow_capture` 按 `reserve_reason_code` 过滤 reserve 条目（[credits_service.py:775-776](../../gateway/credits_service.py)）：结算分钟时只看 `job_reserve` 条目，结算克隆时只看 `smart_clone_reserve_{id}` 条目。`_settlement_reason_codes`（[credits_service.py:405](../../gateway/credits_service.py)）的幂等族也不互含（克隆的 `smart_clone_capture_{id}` 不在 `legacy_job_reserve_codes` 族里）→ **两套结算彼此独立、互不幂等串扰**。

### 2.2 费率真源

- `DEBIT_RATES[("smart","standard")] = 100`（[credits_service.py:56](../../gateway/credits_service.py)，frozen fallback）
- `pricing_schema.py:219` runtime `"smart.standard": 100`（runtime 真源，覆盖 frozen）
- `estimate_credits`（[credits_service.py:154-173](../../gateway/credits_service.py)）= `0 if minutes None/≤0`，否则 `max(1, round(minutes×rate))` —— **纯线性，无平滑/保底/抵扣**。
- 克隆 `voice_clone_cost_credits = 600`（[pricing_schema.py:41/229](../../gateway/pricing_schema.py)）。

### 2.3 create → reserve（正式付费 smart + 自动克隆）

1. **克隆 600 reserve**（[job_intercept.py:1952-1957](../../gateway/job_intercept.py)）：触发 = `service_mode=="smart"` + `user` + `smart_consent.auto_voice_clone is True` + admin `smart_preview_clone_enabled is True`（行 1960）。
   - **⚠️ 触发条件不要求 `preview_mode`** → 见 §2.5（finding 1：正式 full smart 也被纳入 600 reservation）。
   - `reserve_smart_clone_credit`（[smart_clone_reservation_service.py:195](../../gateway/smart_clone_reservation_service.py)）：锁 users row → inline expire → 幂等 → 全局 cap → 库容门 → `reserve_credits_or_raise(600, service_mode="smart", reason_code="smart_clone_reserve_{id}")` → INSERT reservation，**同一事务原子**。
2. **分钟 reserve（create 端）**（[job_intercept.py:2243-2276](../../gateway/job_intercept.py)）：`shadow_credits = estimate_credits(est_min, "smart", "standard")`；`_is_smart_preview = bool(reservation_id) and preview_mode is True`（行 2264）。**正式 full smart 缺 `preview_mode` → 不跳 → 照常 reserve `est_min×100`（`reason_code="job_reserve"`）**。但 `est_min` 常在 create 时未知（`estimated_duration_seconds` 缺失）→ `shadow_credits=0` → 不 reserve。
3. **分钟 reserve（late 端）**（[job_intercept.py:5735-5762](../../gateway/job_intercept.py)）：`update_source_metadata` 报告真时长后，若无 `job_reserve` 则补扣 `late_credits = duration/60×100`（`reason_code="job_reserve"`）；预览（`extract_smart_preview_flag(smart_state)`）跳过，**正式 full smart 不跳 → 照常补扣**。

→ **正式 full smart + 自动克隆的 gross reservation = 600（克隆）+ 分钟×100（分钟），两笔分开占点。** 这是「叠加」的根源，也引出 §5 决策 D-B（over-reservation gating）。

### 2.4 terminal → settle（顺序是设计关键）

`mirror_job_terminal_state`（[job_terminal_mirror.py:145-229](../../gateway/job_terminal_mirror.py)），终态时按**严格顺序**：

1. **行 151** `_settle_smart_clone_reservations_post_terminal(db_job)` → 结算克隆 600。**在 anon/normal 分支之前**、**独立 `async_session()`**、marker-gated（`smart_clone_reservation_id`/`smart_clone_credit_reserved`）。`settle_smart_clone_reservation`：行锁 reservation → 有 chargeable `CloneBillingEvent` → `shadow_capture(600, reason_code="smart_clone_capture_{id}")` + **strict 验证 ledger 真写入** → `status=captured`；无 → `shadow_release` → `status=released`。**自带 commit**。
2. **行 152-180** 匿名预览早返回（`is_anonymous_preview` → 零结算）。**正式 smart 不走此分支**（smart 预览是 `smart_preview_mode`，登录用户，≠ 匿名）。
3. **行 183** `settle_job_quota`（legacy 配额）。
4. **行 203** `settle_job_credit_ledger` → 结算分钟。

> **结论（设计命门）**：克隆 600 结算（行 151，独立 session 已 commit）**先于**分钟结算（行 203）。所以分钟结算时，克隆已是 `captured`/`released` 终态、且已落 ledger commit。**分钟结算可可靠读到「本 task 克隆是否已扣 600」并据此抵扣**。这是本方案 settle-time 抵扣可行的基础。

### 2.5 分钟 settle 的三条分支（抵扣要落在哪）

`settle_job_credit_ledger`（[credits_service.py:1002](../../gateway/credits_service.py)）：

- **行 1046**：`smart_state.smart_preview_mode is True` → `shadow_release("smart_preview_minute_release")` → **预览只扣 600 不扣分钟**（短路，正式任务不进此分支）。
- **行 1074-1084**：`credits_policy` 真值 → 派发 `_settle_smart_job_credit_ledger`。正式 full smart 终态由 handoff stamp `credits_policy="capture_full"`（[process.py:6859/6930/7577](../../src/pipeline/process.py)）→ 走 **`capture_full` 分支**（[credits_service.py:1164-1179](../../gateway/credits_service.py)）：`estimate_actual_job_credits` → `shadow_capture(reason_code="smart_capture_full", reserve_reason_code="job_reserve")`。
- **行 1086-1113**：legacy succeeded 分支（无 `credits_policy` 的 smart 或其它）→ `shadow_capture(reason_code="job_capture", reserve_reason_code="job_reserve")`。

→ **抵扣必须同时落在 `capture_full` 分支与 legacy succeeded 分支**（两者都 `estimate_actual_job_credits` → `shadow_capture`）。建议抽共享 helper（§4.3）。

`estimate_actual_job_credits`（[credits_service.py:373](../../gateway/credits_service.py)）：`minutes = actual_minutes ?? source_duration/60 ?? estimated_minutes`；`credits = estimate_credits(minutes, service_mode, quality_tier)`。

### 2.6 不在范围的相关路径

- `_settle_smart_job_credit_ledger` 的 `refund_full`（release 全部）/ `capture_actual_cost_capped_at_studio_price`（**STUB + hard-gate**，[credits_service.py:1181-1217](../../gateway/credits_service.py)，正常用户路径到不了）。
- 编辑/commit（γ publish-only）：`should_settle_job_credits`（[credits_service.py:341](../../gateway/credits_service.py)）对 `copy_of_job_id` / `edit_generation>0` 返回 False → 编辑态不结算分钟 → 抵扣天然不触及。
- Express / Free 自动克隆：走**不同**的 `express_clone_reservations`（CosyVoice 克隆，注册零扣费，**不扣 600**）→ 不在本方案。

### 2.7 现状一句话

> 正式付费智能版 + 自动克隆 = **分钟×100（`job_capture`/`smart_capture_full`）+ 600（`smart_clone_capture_{id}`）= 叠加**。智能版 3min 预览 = 只 600（分钟被 `smart_preview_minute_release` 跳）。本方案把「正式任务的叠加」改成 `max(600, 分钟×100)` 抵扣。

---

## 3. 目标计费模型确认

### 3.1 公式等价性（无需额外决策）

「`max(600, 分钟×100)`」与「`600 含前 6 分钟 + 之后 100/分钟`」**代数完全相同**：

- `分钟 < 6`：两者都 = `600`。
- `分钟 ≥ 6`：`max(600, 分钟×100) = 分钟×100`；`600 + (分钟−6)×100 = 600 + 分钟×100 − 600 = 分钟×100`。✅ 一致。

实现等价于「克隆照扣 600 + 分钟扣 `max(0, 分钟×100 − 600)`」：

```
总扣 = 600 + max(0, 分钟×100 − 600) = max(600, 分钟×100)   ✓
```

| 分钟 | 分钟×100 | 克隆 600 | 抵扣后分钟 capture | 总扣 | = max(600,分钟×100) |
|------|----------|----------|--------------------|------|----------------------|
| 3 | 300 | 600 | max(0,300−600)=0 | 600 | 600 ✓ |
| 6 | 600 | 600 | max(0,600−600)=0 | 600 | 600 ✓ |
| 10 | 1000 | 600 | max(0,1000−600)=400 | 1000 | 1000 ✓ |
| 30 | 3000 | 600 | max(0,3000−600)=2400 | 3000 | 3000 ✓ |

### 3.2 「平滑过渡」= 已满足（连续、无悬崖）

`max(600, 分钟×100)` 在 6 分钟处**连续**（两段都 = 600），**没有跳变/悬崖**。唯一的「不平滑」是 6 分钟处斜率从 0 变 100（kink，C0 连续但非 C1 光滑）—— 这是抵扣模型的固有形态，**不是计费悬崖**。

**建议：直接用 `max()`（= 抵扣），不引入额外的曲线平滑（C1）。** 理由：
- 项目主目标公式本身就是 `max(600, 分钟×100)`，已无悬崖。
- 计费要**可向用户解释**：「克隆 600，超过 6 分钟后按 100/分钟」一句话能讲清；任意平滑曲线无法解释、且引入非整数/难复现的扣点。KISS。
- 详见 §5 决策 D-D（若项目主坚持要更光滑，给一个可选 ramp 变体，但不推荐）。

### 3.3 新模型 vs 6/01 §12.4 旧公式对比（CodeX 补：这是产品决策）

| 视频时长 | 6/01 §12.4 旧公式（`分钟×100 + 600 − 第10min起50/min封顶600`） | **本方案 `max(600, 分钟×100)`** | 用户少付 |
|---:|---:|---:|---:|
| 3 min | 300+600−0 = **900** | **600** | −300 |
| 6 min | 600+600−0 = **1200** | **600** | −600 |
| 9 min | 900+600−0 = **1500** | **900** | −600 |
| 10 min | 1000+600−0 = **1600** | **1000** | −600 |
| 12 min | 1200+600−100 = **1700** | **1200** | −500 |
| 22 min | 2200+600−600 = **2200** | **2200** | 0（收敛）|

> 旧公式把克隆 600 **加在分钟点之上**、只从第 10 分钟起缓慢抵扣（22 min 才抵满）；新模型从**第 6 分钟**起就把 600 全抵掉、且 <6 min 用 600 保底吸收分钟。**新模型对短/中任务明显更便宜（最多少收 600/任务）。** 这是营收影响，需项目主有意识拍板选哪个（见 D-D）。

---

## 4. 方案设计

### 4.1 核心决策：settle-time 抵扣（在分钟结算时按 actual 重算）

**在 `settle_job_credit_ledger` 的分钟 capture 处，把分钟 capture 金额减去「本 task 已 captured 的克隆点数 C」：**

```
分钟 capture = max(0, 分钟×100 − C)，其中 C = 本 task 已 captured 的克隆点（0 或 600）
```

**为什么 settle 而非 reserve**（任务建议倾向 settle，核实确认）：
- 真实 actual_minutes 与克隆 captured/released 终态**都在 terminal 才确定**（§2.4）；reserve 时 actual_minutes 常未知、克隆是否 chargeable 未知。
- settle 是真正动钱的地方（capture）。settle-time 抵扣 = **精确**（按 actual_minutes + 实际克隆结果），且**克隆 600 settle 先于分钟 settle**（§2.4），抵扣信号已就绪。
- reserve 不变（仍各自占点）→ 不动已审计的 P3 钱核心的 reserve 路径；over-reservation 由 settle 的 release 收口（`shadow_capture` 自动 release 超额 reserve）。see 决策 D-B 关于 over-gating 的可选优化。

### 4.2 抵扣信号 C：读 `CloneBillingEvent`（无 defer，CodeX #2 修订）

> **CodeX #2**：上一版用「克隆未结算 → `return []` 推迟分钟结算」会被 mirror 误判为「settle 成功」（无异常 → `settle_succeeded=True`，[job_terminal_mirror.py:201-206](../../gateway/job_terminal_mirror.py)）→ backfill 用持久 ledger 写出**错误的** `pending_credits_charged`（此刻只有克隆 600 落库、分钟未扣）+ stamp `settled_at`（[job_terminal_mirror.py:288-382](../../gateway/job_terminal_mirror.py)），管理面出现「已结算」假信号。**本版改为读 `CloneBillingEvent` 这一权威计费信号，彻底取消 defer 机制。**

**C = 本 task「将被/已被」收费的克隆点数**，直接从 `CloneBillingEvent`（[models.py:1029](../../gateway/models.py)，migration 037，**唯一权威计费信号**）取：

```sql
C = COALESCE(SUM(r.amount_credits), 0)
    FROM clone_billing_events e
    JOIN smart_clone_reservations r ON r.id = e.reservation_id
    WHERE e.task_id = :task_id AND e.chargeable = true
```

**为什么读 event 而非 reservation 状态 / capture ledger** —— **取消了顺序依赖，从根上消除 CodeX #2 的 defer 问题**：
- `CloneBillingEvent` 由 register-billed 在 **pipeline 阶段**（MiniMax 返回 voice_id 瞬间）就写入（[smart-clone-600 子方案 §3]），**早于任务进入终态**。分钟结算（terminal）时它**必然已存在且 committed**。
- 它是 `settle_smart_clone_reservation` 决定 capture/release 用的**同一个信号**（`chargeable = event is not None and bool(event.chargeable)`，[smart_clone_reservation_service.py:555](../../gateway/smart_clone_reservation_service.py)）。所以「有 chargeable event」⟺「克隆 600 终将被 capture」（克隆 settle 行 151 或 sweeper 兜底保证）。
- 因此**无论克隆 settle 是否已跑、是否与分钟同序**，分钟结算都能读到正确 C：
  - 克隆 chargeable → C=600 → 分钟 capture = `max(0, 分钟×100 − 600)`；克隆 settle（任何时刻）capture 600 → 合计 `max(600, 分钟×100)`。✓
  - 克隆失败/回预设/event 写入失败（白克隆孤儿，P3e-1b）→ **无 chargeable event** → C=0 → 分钟全额；克隆 settle release（无 event）。✓ 用户不为没收费的克隆买单。
- **不再需要 defer / 不碰 backfill 语义**：分钟结算永远在本轮完成（要么 C=0 全额、要么 C=600 抵扣），`settle_succeeded=True` 是真的、backfill 此时 ledger 已含克隆 capture（行 151 先跑）+ 分钟 capture → cost summary 正确。

**钱-安全方向（对抗性核对）**：唯一残余风险是「chargeable event 存在但克隆 settle 永久失败、600 始终没 capture」→ 分钟已抵 600 但克隆没扣 → **平台少收 600**（**fail-toward-user**，不是过收用户）。由 `smart_clone_reservation_sweeper` 保证最终 capture + strict-verify + loud log 兜底，与系统现有「白克隆」容忍同向、可接受。**绝不会过收用户**（这是比上一版 reservation-status 方案更安全的方向：那个方案在克隆 settle 失败时反而会全额扣分钟 → 过收）。

**保留的「先克隆后分钟」顺序仍成立但不再被依赖**：行 151 仍先于行 203，只是 C 不再需要它先完成。

> 事务可见性：`CloneBillingEvent` 在 pipeline 阶段已 commit，外层 `db`（分钟结算 session）任何隔离级别都能看到已 committed 的 pipeline 写入（它发生在本 mirror 事务开始之前）。无 READ COMMITTED 依赖（这是相对 reservation-status 方案的又一优势）。

**备选（若审核坚持「只抵已真正 capture 的钱」）**：改读 capture ledger（`sum smart_clone_capture_%`），则**必须**依赖克隆先结算 + 引入**显式 defer 信号**——不能用裸 `return []`。具体：helper 抛专用 `MinuteSettlementDeferred`，mirror 在通用 `except` **之前**显式捕获 → 置 `settle_succeeded=False`（复用其既有「settle 未完成则跳过 backfill、保留 `pending_*=null`」语义，[job_terminal_mirror.py:194-206/320](../../gateway/job_terminal_mirror.py)）+ INFO 日志（非 WARNING）。**本方案不推荐此备选**（event-based 更简单、更安全、无顺序/隔离依赖），仅记录以回应 CodeX #2。

### 4.3 实现落点（仅描述，不写代码）

- 新增 async helper `_smart_clone_minute_offset(db, *, job, service_mode, actual_credits) -> int`（返回抵扣后的分钟点；**无 defer 返回值**）：
  - `service_mode != "smart"` → 原样返回 `actual_credits`（express/studio/free inert）。
  - `C_own =` 本 task chargeable `CloneBillingEvent` join reservation 的 `amount_credits` 之和（§4.2 SQL；单任务 full smart 命中、convert 为 0）。
  - `C_carryover =` 预览结转抵扣（§4.6 single-use consume；单任务 full smart 为 0、convert 命中 600）。
  - 返回 `max(0, actual_credits − C_own − C_carryover)`。
- 在 `settle_job_credit_ledger` 的 **legacy succeeded 分支**（行 1086-1113）与 `_settle_smart_job_credit_ledger` 的 **capture_full 分支**（行 1164-1179）各调一次：算出 `actual_credits` 后、`shadow_capture` 前应用。无推迟分支、不碰 `settle_succeeded`/backfill。C_carryover 的 single-use consume 与本 task 的 `shadow_capture` 在**同一 settle 事务**内（原子）。
- `shadow_capture(actual_credits=adjusted)` 其余不变：`adjusted ≤ job_reserve` → capture adjusted + release 超额；`adjusted=0` → 全 release、capture 0（[credits_service.py:783-843](../../gateway/credits_service.py)）。幂等不变（`_has_existing_settlement` 在 `job_capture`/`smart_capture_full` 幂等族内，[credits_service.py:421-449](../../gateway/credits_service.py)）。

### 4.4 作用范围（只正式付费 smart）

- **只 `service_mode=="smart"`**：helper 首行 gate。Express/Free 的 CosyVoice 克隆走独立 reservation 且零扣费，不受影响；Studio 无克隆。
- 只对**有克隆 reservation 的 smart**生效：无 reservation 的 smart（未勾选克隆/克隆失败回预设）→ 无克隆 captured → `C=0` → 不抵扣、照常全额分钟。inert by default。
- **预览任务不受影响**：`smart_preview_mode` 在抵扣分支之前短路（行 1046）→ 预览只扣 600，不进 capture 分支。

### 4.5 D-B：reserve-time 减 offset（create + late 两处，避免 over-gating）

**问题**：现状正式 full smart gross reserve = 600（克隆，[job_intercept.py:1952](../../gateway/job_intercept.py)）+ 分钟×100（[2243](../../gateway/job_intercept.py) create / [5757](../../gateway/job_intercept.py) late）。用户若「付得起最终 `max()` 但不够 gross」会在准入处被误拒。

**做法（D-B = 做，两处都改）**：分钟 reserve 改为 `max(0, 分钟×100 − R)`，`R` = reserve 时可知的 offset 基数：
- `R_own = 600` 若本 task 已建克隆 reservation（create 端读局部变量 `_smart_clone_reservation_id`；late 端读 `job.smart_state.smart_clone_reservation_id`）。
- `R_carryover = 600` 若本 task 带 convert 结转 marker（`job.smart_state.preview_clone_credit_offset`，§4.6）。
- `R = R_own + R_carryover`（实际只会命中一个：full-smart-with-clone 命中 R_own；convert 命中 R_carryover）。

**两处都要改（CodeX #3）**：
- create reserve（[job_intercept.py:2243](../../gateway/job_intercept.py)）：克隆 600 reserve 块（1952）在它**之前**已跑 → `_smart_clone_reservation_id` 已可读。
- late reserve（[job_intercept.py:5757](../../gateway/job_intercept.py)）：duration 报告后补扣，读 `job.smart_state`。

**settle 仍是钱的权威**：reserve 只影响准入。即便 reserve 少留了 600，settle 按 actual 算 `max(600,分钟×100)`；若 reserve 偏少、actual 偏多，`shadow_capture` 的 additional-debit 路径从余额补足（[credits_service.py:872-917](../../gateway/credits_service.py)）→ 不漏扣。`InsufficientCreditsError` 行为不变（仍在两处 raise，只是阈值降到 net）。

### 4.6 D-C：预览→完整 600 结转（single-use carryover）

**目标**：「预览（扣 600）→ 转完整（reuse）」与「直接做完整」总扣一致 = `max(600, 分钟×100)`。当前 convert 的完整任务无自有克隆（reuse 强制 `consent.auto_voice_clone=False`）→ `C_own=0` → 全额分钟，合计 `600 + 分钟×100`（比直接做完整多 600）。D-C 把预览那 600 **结转**进完整任务分钟抵扣。

**机制**（含 CodeX 复审 #1/#2/#3 三处修订）：

**步骤 0 — `resolve_preview_reuse` 返回 reservation id + 金额（CodeX #2）**：当前 `PreviewReuseResolution` 只回 `preview_job_id/voice_id/source_type/source_ref`（[preview_reuse_service.py:43](../../gateway/preview_reuse_service.py)），函数内部已查到 captured reservation（[:104](../../gateway/preview_reuse_service.py)）但**未返回**。**扩展** dataclass 加 `preview_reservation_id`（= 已校验的 captured+chargeable+同用户那一条 reservation.id）+ `preview_credit_amount`（= `reservation.amount_credits`，**权威金额，不写死 600**）。这是结转 marker 的**唯一权威来源**。

**步骤 1 — convert-create 把 marker 写进 JobRecord forward **和** PG Job.smart_state（CodeX #1）**：reuse 覆盖块（[job_intercept.py:1289-1310](../../gateway/job_intercept.py)）server-set（绝不取客户端值）三个 marker：`preview_clone_credit_offset = preview_credit_amount`、`preview_clone_offset_reservation_id = preview_reservation_id`、`preview_source_job_id = <preview_job_id>`（供单飞查重 + 审计链接）。
- ⚠️**关键修订**：现有 PG `Job.smart_state` 写入**只在 `_smart_clone_reservation_id` 存在时**才写 dict、否则 None（[job_intercept.py:2187-2212](../../gateway/job_intercept.py)）。convert 强制 `auto_voice_clone=False` → **无 reservation** → 按现状 PG 列写 None → **offset helper（读 PG `Job.smart_state`）永远看不到 marker**。必须**泛化**该写入：`smart_state = ({reservation markers if reservation} | {convert markers if convert}) or None`。同时 `request_data["smart_state"]` forward 这些 convert marker（JobRecord 落盘 + mirror 增量 merge 进 PG，双保险；mirror dict.update 不会 clobber PG 已有的 convert key）。

**步骤 1.5 — convert single-flight，防双击/重发产生两个收费完整任务（CodeX #3）**：现 create 路径**无 create-level idempotency dedup**（[job_intercept.py:1994-2001](../../gateway/job_intercept.py) 注释自证：「双提交无 create-level idempotency dedup」是既有基线）；`convertPreviewToFull` 不带稳定 key（[smartPreviewClone.ts:91](../../frontend-next/src/lib/api/smartPreviewClone.ts)），gateway 缺 key 时随机生成（[job_intercept.py:1797](../../gateway/job_intercept.py)）→ 双击/重试**创建两个完整任务**，按 v1.2 第二个全额扣分钟 = **静默多收**。**修订 = 让 convert best-effort 单飞**（账本 single-use 兜底钱-正确），复用既有 Option-C 机制（非新表）：
- 派生**确定性** full job_id：`_convert_job_id = "job_" + sha256(f"{user.id}:convert:{preview_job_id}")[:32]`（与 smart-clone 的 `{user.id}:{idempotency_key}` 派生**不同命名空间**，不撞）。
- **Gateway pre-forward existing-check 为主**（CodeX 复审 #1 修订）：forward **前**在 gateway 查 `select(Job).where(job_id == _convert_job_id)`。**命中 → 映射返回现有 F，绝不 forward**——`submit_job()` **没有**通用 existing-check，若带已存在的 job_id forward 会 `save_job` **覆盖** + `runner.start` **重启**原 job（重跑付费 workflow / 污染产物，正是 smart-clone replay 留空 job_id 规避的同一教训，[job_intercept.py:1994-2001](../../gateway/job_intercept.py)）。
- 不存在 → `request_data["job_id"] = _convert_job_id`（Option C 预供，submit_job 接受，P3e-2a）+ stamp marker + forward。
- **并发硬化（v1.6 已实现）**：真正并发双击的极小窗口（两 POST 都在对方 commit 前 pre-check）由 `_acquire_convert_singleflight_lock`（pre-check 前取派生自 `_convert_job_id` 的 `pg_advisory_xact_lock`，镜像 P3e-4b `_acquire_global_cap_lock`）串行关掉 → 第二个在第一个 commit 后命中现有 F、幂等返回不 forward。PG `job_id` 主键唯一作非-PG/兜底防线（防真落两行）。
- **语义**：一个预览 → 正常收敛到 **一个**完整任务（best-effort 单飞；真并发双击由 §4.6 步骤 2 账本 single-use 兜底防双扣，残留=transient 重跑非多收）。用户想用克隆音色再做别的视频 → 走**普通 create 选音色库音色**（正常 full smart，照常全额分钟、无结转），不经 convert 路径。**把「第二个收费完整任务」收敛到罕见并发窗口**。

**步骤 2 — F settle 时 single-use consume（钱-正确性 backstop，原子防双扣）**：offset helper 见 `preview_clone_offset_reservation_id` 时，在 F 的 settle 事务内对**预览的** reservation 行做条件消费：
```sql
UPDATE smart_clone_reservations
  SET carryover_applied_to_task_id = :F_job_id
  WHERE id = :preview_resv_id
    AND status = 'captured' AND user_id = :F_user_id   -- 防越权/防错指
    AND carryover_applied_to_task_id IS NULL            -- single-use 闸
```
- rowcount=1（抢到）→ 再校验该 reservation 有 chargeable `CloneBillingEvent`（防不一致 ledger）→ `C_carryover = reservation.amount_credits`。
- rowcount=0 → 读现值：== F_job_id（F 自己 settle 重放）→ `C_carryover = amount`（幂等一致）；指向别的 task → `C_carryover=0`。
- 步骤 1.5 best-effort 单飞**正常**收敛「一预览一 F」，本 single-use 是**钱-权威防线**（真并发双击 / 绕过单飞的边缘——legacy job / 手工 / 未来路径——都由它保证不重复抵扣）。

**步骤 3 — F 的分钟 capture** = `max(0, 分钟×100 − C_carryover)`，与 single-use consume **同一事务** commit。

**为什么 settle-time consume 而非 create-time**：只有**成功**的完整任务才 settle → 才消费结转额度；失败/取消的 convert 永不 settle → 永不消费 → 额度仍在可重试（无需 release-on-failure）。

**cost_summary / 审计口径（CodeX #5）**：**per-job 净值语义**——
- **convert F** 的 ledger 只有分钟 capture `max(0,分钟×100−600)`（克隆 600 在**预览 job P** 名下，不在 F）→ F 的 `pending_credits_charged` = **净分钟**。为让这 600 的减免**可审计**（否则看着像漏扣），settle 时把 `clone_carryover_applied_credits=600` + `clone_carryover_source_job_id=P` stamp 进 F 的 `metering_snapshot`/cost summary。
- **单任务 full smart**（自有克隆）：克隆 capture（`smart_clone_capture_*`）与分钟 capture（`job_capture`）**related_job_id 同为 F**（克隆 reserve task_id=job_id）→ backfill 的 `select(...).where(related_job_id==job_id)`（[job_terminal_mirror.py:341](../../gateway/job_terminal_mirror.py)）**自然把两笔都算进** → F 的 `pending_credits_charged = (分钟×100−600)+600 = max(600,分钟×100)`，单 job 即显示完整总额。✓
- **funnel 合计**（P+F）= 600 + `max(0,分钟×100−600)` = `max(600,分钟×100)`；需 admin/analytics 按 `preview_source_job_id` 链接聚合 P、F 两 job 才能看到 funnel 总额（convert 是两 job、单任务是一 job，此不对称需在 admin 文档注明）。

**钱-不变量**：
- 合计 = 600（预览 clone capture，**不退**）+ `max(0, 分钟×100 − 600)`（完整任务分钟）= `max(600, 分钟×100)`。✓ 与直接做完整一致。
- 预览的 600 **不被退、不被二次 capture**——结转只**降低 F 分钟 capture**，不碰预览 clone ledger。
- **防双扣（两层）**：① best-effort 单飞（一预览正常一 F）；② 账本 single-use `carryover_applied_to_task_id` 闸（钱-权威，真并发兜底）。
- **防越权**：consume 的 `WHERE user_id + status='captured'` + chargeable event 复核 → 伪造/错指 marker 不生效。

**需 migration 039**：`smart_clone_reservations` 加列 `carryover_applied_to_task_id`（nullable String(64)）+ 配套索引。marker（含 `preview_source_job_id`）复用 `smart_state` dict 通道，免改 schema。

**与 D-B 配合**：convert F 的分钟 reserve 也按 `R_carryover=600` 减少（§4.5）；若结转额度在 settle 被并发 F 抢走（C_carryover=0，单飞下几乎不会发生），settle 的 additional-debit 从余额补足 → 仍正确收全额分钟。

---

## 5. 决策点（交项目主 + CodeX）

| # | 决策 | 结论（项目主 2026-06-15 已拍板） |
|---|------|------|
| **D-A** | 抵扣时机 | **settle-time 重算**（§4.1）：精确、不动已审计 reserve 路径主体。|
| **D-B** | 是否减分钟 reserve 避免 over-gating | ✅ **做，不推迟**。create reserve（[job_intercept.py:2243](../../gateway/job_intercept.py)）+ late reserve（[5757](../../gateway/job_intercept.py)）**两处都**按 `R=R_own+R_carryover` 减少分钟 reserve（§4.5）。settle 仍权威。|
| **D-C** | 预览→完整的预览 600 是否结转 | ✅ **现在就做**。convert 完整任务分钟减预览 600，合计 `max(600,分钟×100)`（与直接做完整一致）。**两层防双扣**：① convert best-effort 单飞（确定性 job_id + pre-check，正常一预览一 F，挡双击）；② 账本 single-use carryover（migration 039 `carryover_applied_to_task_id`，钱-权威、真并发兜底）。marker 落 PG Job.smart_state（泛化写入）+ `PreviewReuseResolution` 回 reservation id；cost_summary per-job 净值 + 可审计减免（§4.6）。|
| **D-D** | 用 `max()` 还是 6/01 平滑曲线 | ✅ **用 `max(600,分钟×100)`，替换 6/01 §12.4 旧公式**（§3.3）。连续无悬崖、可解释、短/中任务对用户更便宜。|
| **D-E** | finding 1 的灰度（见 §7） | 维持：翻 `smart_preview_clone_enabled` 是 customer-facing 计费变更，项目主有意识灰度 + E2E **必须**覆盖 full smart 路径（非仅 preview）。本抵扣大幅软化冲击（≥6min 任务 600 全吸收、净扣同旧「白克隆」）。|

---

## 6. 边界与边缘案例

| 场景 | 期望 | 本方案行为 |
|------|------|------------|
| 长任务（≥6min）+ 克隆成功 | `分钟×100` | C=600，分钟 capture=`分钟×100−600`，克隆 capture=600，合计 `分钟×100` ✓ |
| 短任务（<6min）+ 克隆成功 | `600` | C=600，分钟 capture=`max(0,分钟×100−600)=0`（分钟 reserve 全 release），克隆 600，合计 600 ✓ |
| 克隆失败回预设（未扣 600） | 全额分钟 | 无 chargeable event → 克隆 `released`，C=0 → 分钟全额 ✓（符合任务「克隆失败按全额」）|
| 智能版 3min 预览 | 只 600 | `smart_preview_mode` 短路（行 1046），不进 capture 分支，抵扣不触及 ✓ |
| 预览→完整 convert（reuse），首次 | `max(600,分钟×100)`（与直接做完整一致） | F 无自有克隆（C_own=0）；convert marker → single-use consume 抢到 → C_carryover=600 → 分钟 capture=`max(0,分钟×100−600)`；合计 = 预览 600 + 完整分钟 = `max(600,分钟×100)` ✓（§4.6）|
| 同一预览**重复 convert / 双击 / 重发** | 幂等返回同一个 F，**不建第二个收费 job** | 确定性 `_convert_job_id` + **Gateway pre-forward existing-check**（命中不 forward，§4.6 步骤 1.5）→ 收敛到首个 F；不产生「第二个全额分钟」的静默多收 ✓（CodeX #3）|
| 想用克隆音色再做**别的**视频 | 正常 full smart 全额分钟（无结转） | 走普通 create 选音色库音色（非 convert 路径）→ 无 marker → C_carryover=0 → 照常全额（这是正确价格，不该结转）✓ |
| convert 失败/取消（未 settle） | 结转额度不消费、可重试 | F 未 settle → `carryover_applied_to_task_id` 仍 NULL → 用户重试 convert 仍能抵（重试落同一确定性 F）✓ |
| 边缘：绕过单飞的两个 F 同结算一预览 | 只抵一次 | single-use `IS NULL` 闸只让一个 rowcount=1 → 仅一个抵 600（第二道防线）✓ |
| 多说话人 | 同单说话人 | limit-1 per-speaker cap + 单 reservation per task（idempotency on `task_id+purpose`）→ ≤1 克隆 captured/task；helper 按 `sum(captured)` 取 C，未来若允许多克隆也正确累加。|
| actual_minutes 未知 | 现状行为 | `estimate_actual_job_credits` fallback 链；全 None → `actual_credits=0` → 现有 `if ≤0: return []`（抵扣前返回，不改现状，越界出范围）|
| 克隆 settle 异常（行 151 抛）→ sweeper 兜底 | 抵扣不受影响 | C 读 chargeable `CloneBillingEvent`（pipeline 已写）而非克隆 settle 状态 → 克隆 settle 是否已跑都正确 C=600 抵扣；克隆 600 由 sweeper 终 capture ✓（§4.2）|
| 白克隆孤儿（MiniMax 已克隆但 event 写入失败，P3e-1b）| 不抵扣、用户不为白克隆买单 | 无 chargeable event → C=0 → 分钟全额；克隆 settle release ✓ |
| 编辑/commit 重跑（copy_as_new / edit_generation>0） | 不重复结算 | `should_settle_job_credits=False` → 不进分钟结算 → 抵扣不触及 ✓ |
| 幂等重放（list-jobs/detail/R2 sweeper 多次观察终态） | 不双扣 | 首次写 `job_capture`/`smart_capture_full`（或 0-capture 时的 `capture_excess_release`）→ `_has_existing_settlement` 命中 → 后续 skip；抵扣值由 actual_minutes（终态稳定）+ captured-clone（终态稳定）决定 → 重算一致，首写为准 ✓ |

---

## 7. 与 finding 1 的关系（必读）

CodeX 合并前审查 finding 1：`smart_preview_clone_enabled` 翻开后，**正式 full smart 自动克隆也被纳入 600 reservation**（reserve 条件 [job_intercept.py:1952](../../gateway/job_intercept.py) 不要求 `preview_mode`；pipeline gate [process.py:4162](../../src/pipeline/process.py) 两旗 OR）。即存量 full smart 自动克隆用户的计费从「**白克隆漏收**」变成「**扣 600（叠加到分钟）**」——customer-facing 计费变更。

**本抵扣方案正是该 600 的归宿，且大幅软化冲击：**
- 旧（flag 关，白克隆）：用户扣 `分钟×100`（克隆白嫖、平台漏收）。
- finding 1（flag 开，未抵扣）：用户扣 `分钟×100 + 600`（**突增 600**）。
- **本方案（flag 开 + 抵扣）**：用户扣 `max(600, 分钟×100)`。**≥6 分钟任务（真实视频常态）→ 600 全吸收 → 净扣 = `分钟×100` = 与旧「白克隆」相同**，用户**无感**；只有 <6 分钟短任务从 `分钟×100` 升到 600。

→ **抵扣使 finding 1 从「全员突增 600」变成「仅短任务到 600 保底」，是 finding 1 计费变更的正确缓冲。** 但仍需：项目主有意识灰度 `smart_preview_clone_enabled` + 真金小额 E2E **必须覆盖 full smart 路径**（不能只测 preview）。

---

## 8. 钱-正确性论证

1. **不双扣**：克隆 600 与分钟各自 `reason_code`、各自幂等族（§2.1）；抵扣只**降低**分钟 capture 金额，不新增第二笔克隆扣点。`shadow_capture` 幂等（`_has_existing_settlement`）防重放双写。
2. **不漏扣**：长任务分钟 capture = `分钟×100−600` + 克隆 600 = `分钟×100`，平台收齐分钟价；短任务收 600（≥ 成本）。克隆 chargeable 才扣（`CloneBillingEvent.chargeable`）。
3. **不漏退**：抵扣降低分钟 capture 后，`shadow_capture` 仍把分钟 reserve 的超额部分 release（[credits_service.py:832-843](../../gateway/credits_service.py)）→ 无悬挂 reserved。短任务分钟 capture=0 → reserve 全 release。
4. **无顺序依赖（CodeX #2 修订）**：C 读 pipeline 阶段已写的 chargeable `CloneBillingEvent`（与克隆 settle 同一权威信号），不依赖克隆 settle 先完成、不引入 defer、不碰 `settle_succeeded`/backfill。克隆 settle 失败时方向是平台少收（fail-toward-user），不会过收用户。
5. **单一结算入口不破**：抵扣全部落在 `settle_job_credit_ledger`（既有分钟结算单一入口），不新开旁路、不在 sweeper/cleanup 写结算（守 [[feedback_terminal_state_single_entry]]）。克隆结算仍是 `settle_smart_clone_reservation` 单一入口，**不改**。
6. **默认 inert**：无克隆 reservation、无 convert marker 的 smart（含 flag 关时的全部 smart）→ C_own=C_carryover=0 → 字节级不改现状；express/studio/free 不进 helper。
7. **D-C 防双扣/防越权/防双建（§4.6）**：**两层**——① convert best-effort 单飞（确定性 `_convert_job_id` + **gateway pre-forward existing-check**，命中不 forward）→ 双击/重发正常收敛到同一完整任务（真并发双击的极小窗口由 ② 兜底，残留=transient 重跑非多收）；② 结转额度 **single-use**（`carryover_applied_to_task_id` 条件 UPDATE，与分钟 capture 同事务）→ 一预览只抵一 F；consume 复核 `user_id + status=captured + chargeable event` → 伪造/错指 marker 不生效。预览 600 不退、不二次 capture，结转只降 F 分钟 capture。失败 convert 不消费额度（settle-only consume，可重试落同一确定性 F）。marker 同时落 PG `Job.smart_state`（泛化写入，CodeX #1）使 settle 读得到；减免经 `clone_carryover_applied_credits` 入 F cost_summary 可审计（CodeX #5）。
8. **付费 API 硬约束**：本方案**不新增任何克隆/付费 API 调用**，只重算已发生扣点的分配。克隆触发仍是用户显式 consent（CLAUDE.md 白名单豁免）。

---

## 9. 回归测试清单（实现期写，本任务只列）

**单元（credits_service / 抵扣 helper）：**
- 长任务（10min）+ 克隆 captured → 分钟 capture=400、克隆 600、合计 1000=`max(600,1000)`。
- 短任务（3min）+ 克隆 captured → 分钟 capture=0、克隆 600、合计 600=`max(600,300)`；分钟 reserve 全 release、无悬挂。
- 边界 6min → 合计 600（两段相等、连续）。
- 克隆 released（失败回预设，C=0）→ 分钟全额、无抵扣。
- 无克隆 reservation 的 smart → C=0、字节级等同现状（capture_full 与 legacy 两分支各一例）。
- express/studio/free → helper inert（不抵扣）。
- chargeable `CloneBillingEvent` 存在但克隆 reservation **尚未 settle**（克隆 settle 在分钟 settle 之后才跑）→ 分钟仍正确 C=600 抵扣（验证**无顺序依赖**，CodeX #2 核心）。
- chargeable event 存在但克隆 settle 永久失败 → 平台少收 600（fail-toward-user），不过收用户（对抗性）。
- **幂等**：同一终态 settle 跑 2 次 → 第二次 skip，账本只一笔；抵扣值两次一致。
- capture_full 分支与 legacy succeeded 分支**都**应用抵扣（两条用例分别覆盖）。

**单元（D-B reserve-time 减 offset）：**
- create reserve：full smart + 克隆 reservation 已建 → 分钟 reserve = `max(0, est×100 − 600)`。
- late reserve：同上，读 `job.smart_state.smart_clone_reservation_id`。
- convert F：reserve 按 `R_carryover=600` 减少。
- 无克隆/无 marker 的 smart → reserve 不减（inert）。
- reserve 减后 settle actual > reserved → additional-debit 从余额补足、不漏扣。

**单元（D-C single-use carryover + CodeX 复审红线）：**
- convert 首次 → C_carryover=600，合计 `max(600,分钟×100)`。
- **CodeX #1**：convert marker 同时落 Job API `JobRecord.smart_state` **和** Gateway PG `Job.smart_state`（无 reservation 也写）→ settle helper（读 PG）读得到。
- **CodeX #2**：`PreviewReuseResolution.preview_reservation_id` 必须是「同用户 + 该 preview job + captured + chargeable」那一条；`preview_credit_amount` = reservation.amount_credits（非写死 600）。
- **CodeX #3**：同一 preview 双击 / 重复 POST → 只建一个收费 full job（确定性 job_id + **gateway pre-forward existing-check，命中不 forward 不覆盖原 job**）；并发两 POST 收敛同一 F。
- **CodeX #4**：同一 F settle 首次失败后重试 → `carryover_applied_to_task_id == F_job_id` → 仍幂等抵 600，分钟 capture 只一笔。
- consume 越权复核：marker 指向**他人** / 非 captured / 无 chargeable event → C_carryover=0（不生效）。
- 绕过单飞的两个 F 同结算一预览（边缘）→ single-use `IS NULL` 闸只一个抵 600。
- 预览的 600 clone capture **不被退、不被二次 capture**（结转只动 F 分钟 capture）。
- **CodeX #5（cost_summary 口径）**：convert F 的 `pending_credits_charged` = 净分钟 `max(0,分钟×100−600)` + `clone_carryover_applied_credits=600` 可见；单任务 full smart 的 `pending_credits_charged` = `max(600,分钟×100)`（克隆+分钟同 job_id 自然合算）。

**集成（job_terminal_mirror）：**
- 行 151 克隆 capture → 行 203 分钟抵扣，端到端总扣 = `max(600,分钟×100)`。
- **CodeX #2 回归**：抵扣后 `_backfill_smart_cost_summary_post_settle` 写出的 `pending_credits_charged` = `max(600,分钟×100)`（非 0、非仅 600）、`settled_at` 仅在分钟+克隆都结算后 stamp；不出现「克隆已结算/分钟未结算」的中间态假信号。
- 行 151 克隆 settle 抛异常（被吞）→ 行 203 分钟仍按 chargeable event 正确 C=600 抵扣；克隆 600 由 sweeper 终 capture，总扣正确。
- 匿名预览 job 不受影响（早返回，§2.4 行 152）。

**E2E（真金小额，灰度前）：**
- full smart + 自动克隆长任务：扣 `分钟×100`（600 被吸收）。
- full smart + 自动克隆短任务：扣 600。
- 智能版 3min 预览：扣 600（不扣分钟）。
- 预览→convert 首个完整任务：合计 = `max(600,分钟×100)`（= 直接做完整）。
- 同一预览转第二个完整任务：第二个全额分钟（不重复抵 600）。

**守卫（AST/契约）：**
- 抵扣只在 `settle_job_credit_ledger` 路径，不在 sweeper/cleanup 出现（守单一入口）。
- 不新增任何付费 API / 克隆调用 import。

---

## 10. 范围与非目标

**范围（本期全做）**：
- 单任务「正式付费智能版 + 自动 MiniMax 克隆」的 settle-time 600 抵扣 → `max(600, 分钟×100)`（D-A）。
- create + late 两处 reserve-time 减 offset，避免 over-gating（D-B）。
- 预览→完整 convert 的 600 single-use 结转抵扣（D-C，migration 039）。

**非目标（本方案不做）**：
- 任意平滑曲线（D-D 已定用 `max()`）。
- Express/Free CosyVoice 克隆计费（零扣费、独立 reservation）。
- `fail_and_refund` / `partial_capture` STUB 的实装（hard-gate 中，独立任务）。
- 一次预览结转给**多个**完整任务（single-use 明确禁止）。

---

## 11. 落地步骤（批准后；本任务不执行）

1. ✅ CodeX 三审 v1.3 = 82/100「补 2 小修订后可批准进入 TDD」→ v1.4 已补（单飞表述更正 + 版本笔误）。
2. migration 039：`smart_clone_reservations` 加 `carryover_applied_to_task_id`（nullable）+ 索引。
3. TDD：先写 §9 单元（含 D-B/D-C）+ 集成红灯。
4. 实现：
   - `_smart_clone_minute_offset` helper（C_own + C_carryover single-use consume）+ capture_full / legacy 两分支接入。
   - D-B：create reserve（[job_intercept.py:2243](../../gateway/job_intercept.py)）+ late reserve（[5757](../../gateway/job_intercept.py)）减 `R`。
   - **D-C #2**：扩展 `PreviewReuseResolution` 返回 `preview_reservation_id` + `preview_credit_amount`（[preview_reuse_service.py:43](../../gateway/preview_reuse_service.py)）。
   - **D-C #1**：reuse 覆盖块（[job_intercept.py:1289](../../gateway/job_intercept.py)）stamp 三 marker（`preview_clone_credit_offset`/`preview_clone_offset_reservation_id`/`preview_source_job_id`，server-set）；**泛化** PG `Job.smart_state` 写入（[job_intercept.py:2187](../../gateway/job_intercept.py)）使其在无 reservation 的 convert 也落 marker；同时 forward 进 `request_data["smart_state"]`。
   - **D-C #3**：convert 确定性 `_convert_job_id` + forward 前 existing-check（Option-C 预供）→ best-effort 单飞（账本 single-use 兜底钱-正确；并发 advisory lock `_acquire_convert_singleflight_lock` v1.6 已实现，关掉 transient 重跑窗口）。
   - **D-C #5**：settle 把 `clone_carryover_applied_credits` + `clone_carryover_source_job_id` stamp 进 F 的 `metering_snapshot`（cost_summary 可审计）。
5. CodeX 钱-loop 外审（双扣/漏扣/漏退/single-use/越权/幂等）。
6. 默认 inert 验证（无克隆/无 marker 的 smart、非 smart 字节级不变）。
7. 真金小额 E2E：full smart 长/短 + 预览 + convert 首个/第二个。
8. 随 P3e 激活一起灰度（merge 分支 + alembic 037/038/**039** + 翻 `smart_preview_clone_enabled`），项目主有意识沟通 finding 1 计费变更。

---

## 附：关键 file:line 速查

| 项 | 位置 |
|----|------|
| 分钟费率 100 | [credits_service.py:56](../../gateway/credits_service.py) / [pricing_schema.py:219](../../gateway/pricing_schema.py) |
| `estimate_credits`（纯线性） | [credits_service.py:154](../../gateway/credits_service.py) |
| 克隆 600 真源 | [pricing_schema.py:41/229](../../gateway/pricing_schema.py) |
| 克隆 reserve | [smart_clone_reservation_service.py:195](../../gateway/smart_clone_reservation_service.py) |
| 克隆 settle（capture/release 600） | [smart_clone_reservation_service.py:505](../../gateway/smart_clone_reservation_service.py) |
| 克隆 capture reason 前缀 | `smart_clone_capture_`（[smart_clone_reservation_service.py:485](../../gateway/smart_clone_reservation_service.py)）|
| create 克隆 reserve 触发（finding 1：不要求 preview_mode） | [job_intercept.py:1952](../../gateway/job_intercept.py) |
| create 分钟 reserve（preview 跳；D-B 落点①） | [job_intercept.py:2243](../../gateway/job_intercept.py) |
| late 分钟 reserve（preview 跳；D-B 落点②） | [job_intercept.py:5757](../../gateway/job_intercept.py) |
| 克隆计费事件 `CloneBillingEvent`（C_own 信号源；task_id 索引 + chargeable） | [models.py:1029](../../gateway/models.py) |
| preview→full reuse 解析（D-C marker 来源；**扩展返回 reservation_id+amount**，CodeX #2） | [preview_reuse_service.py:43/104](../../gateway/preview_reuse_service.py) |
| reuse 覆盖块（D-C marker stamp 落点） | [job_intercept.py:1289-1310](../../gateway/job_intercept.py) |
| PG `Job.smart_state` 写入（**泛化含 convert marker**，CodeX #1） | [job_intercept.py:2187](../../gateway/job_intercept.py) |
| idempotency_key 随机兜底（无 dedup 基线，CodeX #3 根因） | [job_intercept.py:1797/1994](../../gateway/job_intercept.py) |
| Option-C 确定性 job_id 预供 + smart-clone replay 留空教训（convert 单飞参照） | [job_intercept.py:1982-2002](../../gateway/job_intercept.py) |
| convert 单飞 = **Gateway pre-forward existing-check**（命中不 forward） + PG job_id PK 后备 | §4.6 步骤 1.5 |
| 前端 convertPreviewToFull（无稳定 key，CodeX #3） | [smartPreviewClone.ts:91](../../frontend-next/src/lib/api/smartPreviewClone.ts) |
| terminal settle 顺序（克隆 151 先于分钟 203） | [job_terminal_mirror.py:145-229](../../gateway/job_terminal_mirror.py) |
| 分钟 settle（抵扣落点） | [credits_service.py:1002](../../gateway/credits_service.py)（succeeded 1086 / capture_full 1164）|
| `shadow_capture`（capture+release+overdraft 收口） | [credits_service.py:723](../../gateway/credits_service.py) |
| 预览跳分钟短路 | [credits_service.py:1046](../../gateway/credits_service.py) |
| pipeline reservation gate（两旗 OR） | [process.py:4162](../../src/pipeline/process.py) |
| **D-C single-use 闸（新）** | migration 039：`smart_clone_reservations.carryover_applied_to_task_id` |
