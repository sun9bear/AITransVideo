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
  - ⚠️ **关键耦合（实施期发现，必读）**：「跳分钟点」**不能**脱离「3min teaser」单独做——若 create 端 skip 分钟 reserve 但 pipeline 仍跑**完整**视频（不限 3min），就是**免费完整任务**（漏收全部分钟点，远比克隆 600 严重）。所以 P3e-3 必须**作为一个整体切片**：`smart_preview_mode` marker + create 跳分钟 reserve + **pipeline 限 3min teaser** + 水印 四者同批，缺一不可。「跳分钟结算」自然达成=create 没 reserve 分钟 → settle_job_quota/credit_ledger 天然 no-op（无需改 settlement）。
  - **P3e-2b 现状**：smart-consent job 现 reserve 600 **+ 分钟点都扣**（=完整 smart 任务 + 克隆计费），**还不是**「3min 预览只扣 600」。后者 = 本 P3e-3 整体切片。
- **P3e-2b CodeX 终审补丁（93110c27）**：reservation 收紧闸两旗 OR 耦合——`smart_preview_clone_enabled`(create 侧 reserve 旗) 为真即自动强制 pipeline gate，消除「只开 create 旗、忘开 pipeline gate 旗 → 无 reservation smart job 走 legacy 漏收」的配置顺序风险。
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

---

## 7. P3e-3c-2 设计：preview→正式 server 复用契约（understand workflow wf_f03167ab 综合）

**目标**：用户预览满意后转完整正式流程。前端**只传 `reuse_preview_job_id`**（不传 voice_id / 不传 source）。server 端校验 + 取回 voice_id + 原视频引用复用，生成一个**完整付费 smart 任务**（扣分钟、交付、**不重克隆、不重扣 600**）。

**钱-不变量（本切片必守）**：
1. ❌ **不重扣 600**：复用路径**不创建新 reservation**（强制 `auto_voice_clone=False` → create 600-reserve 块条件 `auto_voice_clone is True` 不满足 → 跳过）。
2. ❌ **不重克隆**：pipeline `_smart_needs_new_clone` 要求 `_smart_consent_allows_clone`（=`auto_voice_clone is True`）→ 强制 False → 绝不调 MiniMax。
3. ✅ **照常扣分钟**：不设 `smart_preview_mode`/`preview_mode` → 完整任务 minute reserve 正常 + 交付完整成片（无 teaser/水印/stream-only）。
4. ❌ **防越权**：voice_a **server 端从 captured reservation.captured_voice_id 取**，源 from preview Job.source_*；客户端夹带的 voice_a/voice_b/source **一律覆盖**（不信任）。
5. ❌ **拒绝不合格预览**：未捕获/已 release/voice 已过期 → 显式 4xx 拒绝，**绝不**静默回落到完整重克隆或错扣。

**权威信号（reader B）**：`SmartCloneReservation(task_id=preview_job_id, user_id).status=='captured' AND settled_at IS NOT NULL` → `captured_voice_id`；佐以 `CloneBillingEvent.chargeable=true`。voice 活性 = `UserVoice(user_id, voice_id, expired_at IS NULL)`。

**架构**：
- **新服务模块** `gateway/preview_reuse_service.py::resolve_preview_reuse(db, *, user_id, preview_job_id) -> (PreviewReuseResolution | None, reason)`，纯 DB 校验/取回（独立可测，镜像 smart_clone_reservation_service 风格）。reason ∈ `{preview_not_found, preview_forbidden, preview_clone_not_captured, preview_voice_unavailable, preview_source_unavailable}`。
- **create 路径薄接线**（`intercept_create_job`，trust-marker strip 之后、smart_consent 校验之前）：`reuse_preview_job_id` present + admin `smart_preview_clone_enabled` 开 → 调 resolve → 成功则覆盖 request_data（`service_mode=smart`、`source={type,value}`、`voice_a=voice_id`、`voice_b=None`、`smart_consent.auto_voice_clone=False` 的合法 6 字段 consent、pop `preview_mode`/`reuse_preview_job_id`）→ 走既有完整 create flow（DRY，分钟计费等同）。resolve 失败 → 4xx 拒绝。flag off + present → 403 `reuse_disabled`（不静默改建普通任务）。
- **默认 inert**：`reuse_preview_job_id` 缺省 → 字节级不变。

**source 复用** = reuse preview `Job.source_type`/`source_ref`（P3e-3b 只裁了派生 `preview_teaser.wav`，**source_ref 仍指原始全长源**：YouTube URL / 上传 final_path）。本地文件若已清理 → pipeline ingestion 失败 → 终态 release 分钟（非永久漏，graceful）；早期 fs 存在性检查 = 优化项，本切片不做（保持纯 DB 可测）。

**voice 实际复用（非钱-关键，best-effort）**：完整任务复用**同源视频** → `source_content_hash` 同 → smart auto-reuse-by-hash（默认开）自然命中 `created_from='smart_preview'` 的克隆音色；显式 `voice_a` 加固。钱-安全由 `auto_voice_clone=False`（无法克隆）兜底——即便 voice 复用退化为 PRESET 也只是音质降级，绝不重克隆/重扣。

**copy_as_new 缺口（CodeX 标，归 P3e-4）**：本契约是**新建完整任务**（不走 editing copy_as_new），故无 `preview_teaser.wav` 复制问题。但 P3e-4 的 enter-edit gate 须挡 smart 预览进 editing（否则 copy_as_new 复制清单缺 teaser → resume 满长出片）。

## §8 — P3e-4a：免费用户 smart 预览 entitlement 放行 + edit/export 泄漏闸（默认 inert）

**目标**：让免费 / 未获 smart entitlement 的登录用户能进入**受限**智能版预览 lane（3min
水印 teaser、只扣 600 克隆、跳分钟、stream-only），并把"smart 预览不可进入任何可编辑 /
可导出路径"这条 stream-only 契约在服务端封死。默认 inert（admin `smart_preview_clone_enabled`
默认 False → 字节级不变）。

**改动**：
- **新 `gateway/smart_preview_gate.py::smart_preview_lane_exempt(request_data, user)`**：放行判定纯函数。放行 = 登录 + `preview_mode is True` + `smart_consent.auto_voice_clone is True` + 通用 smart kill switch 双层（env `enable_smart_mode` AND admin `smart_mode_enabled`）+ 本 lane 旗 `smart_preview_clone_enabled`。
- **`intercept_create_job` 两道 entitlement gate**（Gate A smart_disabled / Gate B service_mode_not_allowed）：单次计算 `_smart_preview_exempt`（限 `service_mode=="smart"`）→ 未获 smart 但放行的免费预览过闸；`_smart_preview_via_exemption`（未获 smart + 经 exemption 进来）标记供 600 预留失败兜底。
- **600 预留失败兜底**：`_smart_preview_via_exemption and not reservation_id` → 402 `smart_preview_reserve_failed`（不落按分钟计费的完整任务）。entitled 用户 reserve 失败仍按既有降级语义。
- **enter-edit 闸**：`enter_editing` 在 `smart_preview_mode is True` 时无条件拒绝；gateway `_enforce_post_edit_access` 同档 403（置于 plan-limits 前）。
- **共享判定 preview-aware**：`services.smart.state.is_editable_smart_state` 对 `smart_preview_mode=True` 返回 False —— 单点封死 `enter_editing` / 剪映 draft gate（`api.py`）/ `JianyingDraftRunner` 三处消费者。

**钱/安全不变量（4-lens 对抗性 + CodeX 两轮过）**：默认 inert；fail-closed；免费用户**只能**拿受限 600-preview（auto_voice_clone=False 蒙混、preview_mode 非真值、600 预留失败、reuse 路径、enter-edit/剪映 导出一律封死）；entitled 用户行为不变；通用 smart 紧急停同时停预览（含 gate→reserve TOCTOU 重核）。

**⚠️ 已知遗留（CodeX 第二轮标，归下一步 P3e-4a-2，flag-flip 前必关）**：smart 预览任务的**其它只读检视面**仍未被 stream-only 闸覆盖——`GET /jobs/{id}/review-state`（暴露 transcript/translation items 的 `source_text`/`cn_text`=译文）、`GET /jobs/{id}/speaker-audio/{speaker}[/{seg}.wav]`（源文+源音字节）、report 流（`subtitle_width_report.json` cue 文本）。均为 **P3e-3d 既有遗留**（非本切片引入，CodeX 评 P2），但本切片放免费用户进来后变为该用户类可达。下一步用同一 `_policy_mode_for == "anonymous_preview"` 闸覆盖（需先核实 smart 自动审批是否真填 review-state）。

**非钱依赖（归后续）**：免费用户**转完整**（reuse→full）仍过既有 smart entitlement kill-switch；预览**前端入口** + 预扣弹窗 + consent 驱动 + 反滥用 cap 真生效（`smart_preview_clone_daily_global_cap`/`inflight_cap` 现仍 inert）= P3e-4c。
