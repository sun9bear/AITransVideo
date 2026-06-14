# P3e-2/3 设计：smart 预览 lane + create-time reserve（understand workflow 综合）

**状态：** DESIGN（understand workflow wf_8b5dd2da 综合，5 reader 映射）/ 待实现
**前置：** P3 钱-核心 + P3e-1a gate + P3e-1b limit-1/路由/parity 已完成 + CodeX 清，**对既有 inert**
**父方案：** [`2026-06-14-p3-smart-clone-600-credit-subplan.md`](2026-06-14-p3-smart-clone-600-credit-subplan.md) §11 P3e-2/3/4

> 本文档是动 create 路径（计费敏感）前的设计落盘。pipeline 侧 reservation/billing 机器（reserve→bill→settle→trigger→sweeper + gate + limit-1 + register-billed 路由）已就绪且默认 inert；**P3e-2 是激活它的最后一环**（create 端 reserve + stamp marker）。

---

## 1. 现状（understand workflow 确认）

- **create 入口** `gateway/job_intercept.py::intercept_create_job`（L1158-1965）：parse → gate service_mode → 校验 consent（smart hard-fail / express soft-skip）→ `compute_job_policy`（smart 锁 MiniMax，L471-497）→ **forward Job API（L1773）** → 从 response 取 `job_id`（L1789）→ 建 PG Job（L1793-1836，含 execution snapshot 字段）→ `reserve_quota`（L1843）→ `reserve_credits_or_raise`（分钟点，L1878）→ `db.commit`（L1900）。
- **smart_preview_clone reservation 尚未接入 create**：`reserve_smart_clone_credit` 服务就绪（`smart_clone_reservation_service.py:122-232`），但 create 零调用。
- **marker 通道**：pipeline emit `[SMART_STATE]` → `process_runner._record_line` 合并进 JobRecord.smart_state（JSON store）→ mirror 同步到 PG Job.smart_state → finalizer marker-gate（`job_terminal_mirror._smart_clone_settle_needed` 读 `smart_clone_reservation_id`/`smart_clone_credit_reserved`）。pipeline 读 `_snap("smart_clone_reservation_id")`（JSON store JobRecord）。
- **APF 预览参照**：`is_anonymous_preview`（bool，**登出匿名专用**）驱动 3min teaser + 水印（`effective_policy_mode(service_mode, is_anonymous_preview)`，process.py:6470-6481）+ 零结算（job_terminal_mirror:152 短路跳 settle）。Lane（free/express）是运行时解析非 Job 持久字段。intake/cap 是通用 `LaneAwareCounterStore`。

---

## 2. 架构命门（understand 暴露 + 解法）

**问题**：`job_id` 由 Job API 在 **forward 之后** mint（L1789）；`reserve_smart_clone_credit(task_id=job_id)` 必须在 job_id 已知后调；但 **pipeline 读的 JSON store JobRecord.smart_state 是 forward 时写的**（reserve 之前）→ reservation_id 不在其中 → pipeline `_snap` 读不到 → gate/limit-1/register-billed 全不触发。

**解法（推荐 = 方案 B：create 后回写 JSON store JobRecord）**：
- reserve 在 create 流程内（job_id 已知后、`db.commit` 前，约 L1888 后）调 `reserve_smart_clone_credit`。
- 成功后 **stamp 两处**：
  1. **PG Job.smart_state**（gateway，直接写 db_job.smart_state，finalizer marker-gate 读这个）。
  2. **JSON store JobRecord.smart_state**（经 Job API 回写通道——`update_source_metadata`(L5238) / PATCH /jobs/{id}(L5049) 同源机制——pipeline `_snap` 读这个）。
- 备选（方案 A）：gateway 预生成 job_id 在 forward 前 reserve + 把 marker 塞 `request_data['smart_state']` 一并 forward。**但** 现 create 流程 job_id 由 Job API mint，改预生成牵动面大 → **不推荐**。
- ⚠️ **原子性**：reserve（写 reservation 行 + 信用预扣）与回写 marker 之间若 gateway 崩溃 → 孤儿 reservation（TTL sweeper 兜底 release，已就绪）。回写失败 → pipeline 读不到 marker → gate 不开 → 退预设（fail-safe，不漏收）。两个方向都安全。

---

## 3. 数据模型变更（需 migration）

- **JobRecord（JSON store，`src/services/jobs/models.py`）** + **Job ORM（`gateway/models.py`）** 加：
  - `smart_clone_reservation_id: str | None`（reservation.id）
  - `smart_clone_credit_reserved: bool`（marker gate 第二键）
  - `smart_preview_mode: bool`（**新** smart 预览标记，**区别于 is_anonymous_preview**——smart 预览是登录免费用户，非匿名）。驱动 3min teaser + 水印 + **部分**结算跳过（跳分钟结算、**保留**克隆 600 结算，区别于 APF 全跳）。
- **alembic migration 038**：jobs 表加上述列（038，down_revision=037）。
- to_dict/from_dict（JobRecord）+ snapshot 序列化同步。

> marker 放 `smart_state` 字典（finalizer 已读 smart_state）还是 Job 顶层列？understand R4 倾向**顶层列**（快查 + pipeline `_snap` 直读），但 finalizer marker-gate 现读 `smart_state` 字典。**决策**：stamp 进 `smart_state` 字典（复用现有 marker-gate 通道，免改 finalizer），顶层列可选作索引加速（本期可不加）。

---

## 4. 分步实现（P3e-2/3/4）

- **P3e-2（create reserve + marker，激活核心，money-critical）**：
  1. create 读 `smart_preview_clone_enabled`（admin，fail-safe：读不到→不 reserve 走预设，不阻断）。
  2. 判定「smart 预览克隆请求」=`service_mode==smart` + `smart_consent.auto_voice_clone==True` + flag 开 + `smart_preview_mode` 请求标记。
  3. job_id 已知后调 `reserve_smart_clone_credit(user_id, task_id=job_id, amount_credits=600, ttl_minutes, library_cap)`。
  4. outcome：`reserved` → stamp marker（PG + JSON store 回写）；`denied`(insufficient_credits/voice_library_full)/`user_not_found` → **不阻断**，走预设 + create 响应带 `clone_skipped_reason`（**需新增响应字段**，现 create 响应是 pass-through，R1 风险点）。
  5. 翻 `smart_clone_requires_reservation`=True（激活 P3e-1a gate）——**与本步同批**。
- **P3e-3（预览 lane 交付）**：`smart_preview_mode` 驱动 3min teaser + 水印（扩 `effective_policy_mode` 或新分支）+ job_terminal_mirror **部分**结算跳过（跳分钟、保留克隆 600 结算）+ preview→正式 server 复用契约（前端只传 `preview_job_id`，server 校验同用户 + 有 captured billing event + voice 入库 → 取回 voice_id + 原视频引用）。
- **P3e-4（前端 + 反滥用）**：预扣弹窗（600 + 余额 `getMyCredits` + `clone_skipped_reason` 降级提示）+ consent 驱动（`jobs.ts` 现硬编 auto_voice_clone:true → 用户确认驱动）+ `smart_preview_clone` 旋钮去占位 + 反滥用 cap（`smart_preview_clone_daily_global_cap`/`inflight_cap` 真生效）。

---

## 5. 风险 / 注意（understand 汇总）

- **激活顺序硬约束**：P3e-2（reserve + marker）+ flag 翻 True 必须同批；否则既有 smart 用户 auto-clone 因无 reservation 全退预设（P3e-1a gate 闸关）。
- **clone_skipped_reason 响应字段不存在**：现 create 响应 pass-through；降级原因目前只在 audit JSONL / smart_state。前端「降级提示」需后端**新增**响应字段或前端读 job 终态/audit。
- **smart_preview_mode ≠ is_anonymous_preview**：不能复用匿名标记（那会全跳结算 + 当匿名处理）。smart 预览要**部分**跳（跳分钟、留克隆结算）。
- **fail-safe 方向贯穿**：flag 读不到 / reserve denied / 回写失败 → 一律退预设（不漏收、不误扣）。
- **lane 字符串硬编多处**（mode_scope_key / express_subgate_key / _create_mode_gate switch）——若 smart 预览复用 APF intake，须同步扩 lane 枚举（R2 风险）。

---

## 6. 测试矩阵（钱-正确性优先，TDD）

| 类别 | 断言 |
|---|---|
| 🔥 reserve | consent + flag + ≥600 + 库未满 → reserve 600 + stamp marker（PG + JSON store） |
| 🔥 insufficient | <600 → denied、不 reserve、marker 不写、走预设、create 响应带 clone_skipped_reason=insufficient_credits |
| 🔥 库满 | 库满 → denied=voice_library_full、preset |
| 🔥 flag off | smart_preview_clone_enabled=False → 不 reserve、零既有行为变化 |
| 🔥 marker→pipeline | stamp 后 pipeline `_snap("smart_clone_reservation_id")` 读到 → gate 开 + limit-1 + register-billed 激活（端到端） |
| 🔥 marker→finalizer | stamp 后 job 终态 → finalizer marker-gate 激活 → capture/release |
| 回写失败 | Job API 回写失败 → pipeline 读不到 → 退预设（fail-safe），reservation 由 sweeper release |
| 激活顺序 | flag=True 但 create 未 reserve（误配）→ 既有 smart 退预设（不崩、不漏收） |
