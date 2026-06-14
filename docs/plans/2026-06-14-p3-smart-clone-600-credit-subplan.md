# P3 子方案：智能版自动克隆主音色 + 600 点预扣 / 退还

**状态：** PROPOSED v2 / **待项目主 + CodeX 审新架构后再写动钱代码**
**日期：** 2026-06-14
**父方案：** [`2026-06-14-anonymous-express-cosyvoice-clone-enable-plan.md`](2026-06-14-anonymous-express-cosyvoice-clone-enable-plan.md) §5
**架构决策（项目主已拍板）：** 600 点信用**在 gateway 建任务时预扣**（不在 pipeline）。

---

## v2 修订（2026-06-14，项目主澄清 + CodeX 架构审核）

**项目主澄清（4 问的答复）：**
1. **形态=smart 3 分钟预览**：预览**只扣克隆点 600、不扣分钟点**（预览不交付、带水印）。用户预览满意 → 转**完整正式流程**；正式流程**复用**：上传的原始视频 + 已克隆音色；**其它预览中间产物不复用**，正式流程从头跑。→ ⚠️ 这把范围从"完整 smart 任务克隆计费"**改为 smart 预览 lane（原始方案 §12.3）**，是更大的特性。
2. capture 时机：按建议（终态结算）——但见下方 CodeX 修正（改 durable billing event）。
3. 库容门：本期一起做（解释见正文 §6）。
4. **`smart_preview_clone_enabled` 旋钮正式接入**（作为 smart 预览克隆的 gate，不再是占位）。

**CodeX 架构审核（钱-正确性 P0/P1，必须纳入）：**
- **P0-1 manifest 不可当账本**：MiniMax 成功后 pipeline 可能崩溃/丢产物 → "已花钱却 release"或"reservation 挂死"。**修正**：pipeline 在 MiniMax 返回 voice_id 瞬间写**持久化 `clone_billing_event`**（`task_id+reservation_id+provider=minimax+voice_id+chargeable=true`）；结算只看 DB reservation + 该事件，manifest 仅辅助校验。
- **P0-2 reservation 状态机 + 幂等键**：唯一约束 `task_id + purpose=smart_clone_minimax_600`；状态单向 `reserved→captured` 或 `reserved→released`；capture/release 同事务行锁 + ledger idempotency key 防重复扣/退。
- **P1-3 terminal finalizer/watchdog**：覆盖**所有**"克隆没触发"终态（未入队/worker 没起/前置失败/决策跳过/库满拒/provider 前失败/取消/超时/崩溃），任务进终态时无 chargeable clone event → release；有 → capture。
- **P1-4 capture 条件 = `chargeable_clone_created_for_this_task=true`**，不是"用了某 voice_id"（复用历史/缓存/手工/fallback voice 都不收费）。

**结论**：CodeX 暂不建议直接接真钱——gateway-create-time 预扣**可成立**，但前提是它是"可对账、可幂等流转的 reservation + durable billing event"，不能靠 manifest 当主信号。本 v2 架构（§3）已纳入。

---

> ⚠️ **本子方案动真钱（MiniMax 克隆 600 点预扣）。** 钱算错（双扣 / 扣了不退 / 退了不扣）是严重 bug。这是**用户显式 consent 的知情付费路径**（CLAUDE.md「✅ 用户显式触发」例外，不违反硬约束），但必须**计费正确**。批准前不写动钱代码。

---

## 1. 目标（用户原话）

登录用户（注册赠 800 点 = free 500 + trial 300）能**自动克隆主说话人音色（限 1 个）**，按 **600 点预扣 + 提示用户 + 失败退还**，克隆成功入个人音色库。

---

## 2. 现状与缺口（实施期调查，2026-06-14）

| 维度 | 现状 | 缺口 |
|---|---|---|
| MiniMax clone provider | ✅ 已接线 `build_smart_clone_provider()`（[`process.py:4472`](../../src/pipeline/process.py)，条件=smart_consent.auto_voice_clone + smart_auto_clone_enabled + main speaker + quota） | — |
| **600 点预扣/退还** | ❌ smart 克隆**不单独计费**，只在任务结算按分钟扣（**漏收克隆费**）。Studio 手动克隆才有 reserve/release 600（[`voice_selection_api.py:668-713`](../../gateway/voice_selection_api.py)） | **预扣 + 退还 + 结算** |
| 限主说话人 1 个 | 部分（main speaker 逻辑在），但可能对多个 main speaker 都克隆 | **硬限 1** |
| 个人音色库容门 | smart 用 quota snapshot（水线暂停），无"满则拒"硬门 | **库满硬拒（对齐 Studio）** |
| 前端预扣提示 | ❌ smart_consent 硬编码 `auto_voice_clone:true`（[`jobs.ts:109`](../../frontend-next/src/lib/api/jobs.ts)），无勾选、无"预扣600点"弹窗、无余额展示 | **弹窗 + 余额 + 退款提示** |
| smart 预览 lane | ❌ 不存在（只有完整任务） | 本子方案**不建预览 lane**（见 §9） |

**重要隐患**：当前 smart auto-clone **已在调真 MiniMax 但不扣 600**（smart_auto_clone_enabled 默认 True + 前端硬编码 auto_voice_clone:true）。本子方案补上 600 计费，顺带堵住漏收。

---

## 3. 架构 v2：smart 预览 lane + gateway 预扣 + durable billing event

**预览阶段（只扣克隆 600，不扣分钟点；预览带水印、不交付）：**
```
前端 smart 预览提交（含克隆 opt-in）
  → [新] 弹窗"克隆主音色 -600 点（余额 X）；预览不扣分钟点；失败自动退还"→ confirm
  → gateway 建 smart 预览任务（preview_mode）：
       - **降级到预设的所有情况都不阻断预览，但必须把"降级原因 + 本次用预设音色"回给前端提示用户**
         （知情，不静默降级）。create 响应带 `clone_skipped_reason`（枚举），前端据此弹提示：
           · 余额 <600 → `insufficient_credits`：提示"点数不足 600，本次用预设音色（可充值/升级后再克隆）"
           · 库满 → `voice_library_full`：提示"音色库已满，本次用预设音色（可删旧音色/升级）"
           · smart_preview_clone_enabled=False（admin 关）→ `clone_disabled`：提示"音色克隆暂未开放，本次用预设"
           · 无 consent（用户没勾选克隆）→ 正常预设，无需特别提示
       - 上述任一 → **不创建 reservation、不克隆**；正常出 3 分钟预设音色预览。
       - 否则（consent + 旋钮开 + 余额≥600 + 库未满）→ 创建 reservation 行：
         purpose=`smart_clone_minimax_600`，**唯一约束 (task_id,purpose)**，状态=`reserved`，幂等键；
         **不 reserve 分钟点**（预览不交付）
  → pipeline（预览：3 分钟 teaser + 水印）：
       - 无 reservation → 不调 MiniMax → 预设音色
       - 有 reservation → 克隆**仅主说话人 1 个**（real MiniMax）；**MiniMax 返回 voice_id 瞬间
         写 durable `clone_billing_event`(task_id+reservation_id+provider=minimax+voice_id+
         chargeable=true)**；克隆音色存个人音色库(source=`smart_preview`)
  → terminal finalizer（任务进任意终态时跑，幂等）：
       - 有 chargeable clone_billing_event → **capture 600**（同事务行锁 reservation + ledger 幂等键）
       - 无 → **release 600**（覆盖所有"克隆没触发"终态：未入队/worker 没起/前置失败/决策跳过/
         库满拒/provider 前失败/取消/超时/崩溃）
       - manifest 仅辅助产物校验，**不当账本**

完整正式流程（用户预览满意后选择继续）：
  → **复用**：上传的原始视频 + 已克隆 voice_id（个人音色库）；**其它预览中间产物不复用**，正式管线从头跑
  → 正式任务按分钟计费；**不重复克隆、不重复扣 600**（voice 已在库、preview 阶段已 capture）
```

**钱-正确性不变量（CodeX 必守）：**
1. 钱的事实**只依赖 DB reservation + durable billing event**，绝不依赖文件产物（manifest）。
2. reservation 状态**单向** `reserved → captured | released`；唯一约束 `(task_id, purpose)` + capture/release 同事务行锁 + ledger 幂等键 → 防 double-charge / capture-release 竞态。
3. 任意终态若无 chargeable clone event → **必 release**（不漏退）；无 reservation → **绝不克隆**（不白克隆）。
4. capture 条件 = `chargeable_clone_created_for_this_task=true`（本任务真新建付费克隆），**不是**"用了某 voice_id"（复用历史/缓存/手工/fallback voice 都不收费）。
5. ops-level kill switch（关预扣 / 关 capture / 禁无 reservation 触发付费 clone），非改账务语义的普通用户开关。

> ⚠️ **架构含义（CodeX 强调）**：这要求一个**真正的 reservation 状态机 + durable billing event 表 + terminal finalizer/watchdog**，比"简单 reserve/release"重得多——很可能需要 **gateway DB 加表/列（reservations + billing_events）→ alembic migration**，不再是"无需 migration"。这是本子方案最重的一块，必须先把数据模型 + 状态机审清再写代码。

---

## 4. 信用原语（复用既有，不新造）

复用 `gateway/credits_service` / `voice_selection_api` 已有：
- `reserve_credits_or_raise(db, user_id, job_id, estimated_credits=600, service_mode="smart", reason_code=...)` → `InsufficientCreditsError`。
- `shadow_release(db, user_id, job_id, reason_code, reserve_reason_code)` → 退还。
- capture：复用 `capture_full` 终态结算路径，扩展为"额外 capture 克隆 reserve"。
- `ensure_credit_buckets_for_user` / `_commit_shadow`。

> **不新建** internal endpoint / pipeline 信用客户端（gateway-create-time 架构的好处：信用调用全在 gateway，pipeline 不碰钱）。

---

## 5. 限主说话人 1 个

- gateway create 时：克隆 reserve 固定 **600 × 1**（只为主说话人）。
- pipeline：`evaluate_voice_review` 入口把待克隆 main speaker 列表**截断为 1**（按 plan §13 优先级：说话时长/台词数/音质，取首个）。其余说话人走预设/复用。
- 即便多 main speaker，也只克隆 1 个、只扣 600。

---

## 6. 个人音色库容门（对齐 Studio）

- gateway create reserve 前：查 `count_active_voices_for_user`（跨 provider 合计），≥ 套餐上限（Trial 10/Plus 30/Pro 100，admin 可配）→ **不 reserve 克隆点**，JobRecord `smart_clone_credit_reserved=false` + reason=`voice_library_full`，pipeline 走预设。前端弹窗提示"音色库已满"。
- 镜像 `gateway/cosyvoice_clone/api.py::cosyvoice_clone_max_voices_per_user` 模式（满则拒、在 reserve/付费之前）。

---

## 7. 数据模型（v2 校正：钱-账本需 gateway DB 表 + migration）

**钱-账本（gateway DB，需 alembic migration——v2 推翻了 v1"无需 migration"）：**
- `smart_clone_reservations` 表（或复用既有 reservation 表加 purpose）：`task_id`(唯一约束 + purpose) / `user_id` / `purpose='smart_clone_minimax_600'` / `amount=600` / `status∈{reserved,captured,released}` / `created_at` / `settled_at` / ledger 幂等键。
- `clone_billing_events` 表：pipeline 在 MiniMax 成功瞬间写 `task_id` / `reservation_id` / `provider='minimax'` / `voice_id` / `chargeable=true` / `created_at`。**这是唯一权威计费信号**。
- 二者是 §3 不变量 1/2 的落地；terminal finalizer 读它们做幂等 capture/release。

**JobRecord 字段（Job API JSON store，无需 migration）——仅状态传递，不当账本：**
- `smart_clone_credit_reserved: bool` / `smart_clone_reservation_id: str`（pipeline 读，决定是否克隆）。
- `preview_mode: bool`（预览 lane 标记，驱动 3 分钟+水印+不扣分钟点）。

> **manifest 仅辅助产物校验，不当账本**（CodeX P0-1）：钱只认上面两张 DB 表。settlement/finalizer 在 gateway，读 reservation + billing_event；pipeline 不碰钱（只写 billing_event 这条 durable 信号）。

---

## 8. 前端

- `TranslationForm.tsx` smart submit 前：**预扣弹窗**——"将克隆主说话人音色，预扣 600 点（余额 X）。失败自动退还。" + confirm/cancel。余额来自既有 `getMyCredits()`（[`TranslationForm.tsx:77`](../../frontend-next/src/components/workspace/TranslationForm.tsx)）。
- 余额 <600 或库满：弹窗降级为"点数不足/音色库已满，本次用预设音色"（不阻断提交）。
- `jobs.ts`：smart_consent 现硬编码 auto_voice_clone:true → 改为**用户确认结果**驱动（弹窗 confirm → true；cancel-but-proceed → false 走预设）。
- 任务结果页：克隆失败时显示"克隆失败，已退还 600 点"。

---

## 9. 明确不做（范围边界）

- **不建 smart 3 分钟预览 lane**（独立 funnel 特性；本子方案让"完整 smart 任务"就能克隆+计费，匹配用户"登录用户能自动克隆主音色"诉求）。
- **不改 smart_auto_clone_enabled 默认**（保持 True，既有行为）。
- 多说话人克隆 / 平滑抵扣公式（plan §12.4）= 后续。

---

## 10. 测试矩阵（钱-正确性优先，TDD 先写）

| 类别 | 断言 |
|---|---|
| 🔥 reserve | consent + ≥600 + 库未满 → reserve 600；JobRecord reserved=true |
| 🔥 insufficient | <600 → 不 reserve、reserved=false、pipeline 走预设、**不调 MiniMax** |
| 🔥 库满 | 库满 → 不 reserve、reason=voice_library_full、preset |
| 🔥 release-on-fail | reserved=true 但克隆失败/未触发/preset → settlement **release 600**（余额复原） |
| 🔥 capture-on-success | reserved=true 且产出 voice_id → settlement **capture 600**（实扣） |
| 🔥 no-double | 成功路径绝不 release+capture 双发；失败路径绝不 capture |
| 限1 | 多 main speaker → 只克隆 1、只扣 600 |
| 入库 | 成功 → user_voices 写入（source=smart）|
| 前端 | 弹窗展示 600+余额；<600/库满降级提示；cancel→preset |
| consent | auto_voice_clone 由用户确认驱动，非硬编码 |
| 回归 | 既有 smart 行为（reserved=false 时）字节级不变 |

---

## 11. 分步实现（批准后）

- **P3a** 后端 gateway create reserve + JobRecord 字段 + 库容门 + insufficient→preset（TDD）→ CodeX。
- **P3b** pipeline 限1 + 读 reserved 决定克隆/preset + 产物 manifest（TDD）→ CodeX。
- **P3c** settlement capture/release 对账（TDD，钱-正确性重点）→ CodeX。
- **P3d** 前端预扣弹窗 + 余额 + consent 驱动 + 退款提示（tsc + 守卫）→ CodeX。
- **P3e** 全量 set-diff + 最终 CodeX。

---

## 12. 待项目主/CodeX 审的开放问题

1. **预扣顺序**：分钟点数 + 600 克隆点，用户恰好 800 点时若"分钟+600 > 800"——先 reserve 分钟还是先 reserve 克隆？建议**先分钟后克隆**（分钟是任务必需，克隆是增值；不够克隆就走预设）。
2. **capture 时机**：克隆成功的 capture 跟 capture_full 一起（终态）还是克隆成功即时 capture？建议**跟 capture_full**（单一结算入口，对账简单，避免中途 capture 后任务又失败的复杂度）。
3. **库容门 plan-aware**：Trial 10/Plus 30/Pro 100 是否本子方案落地，还是先用单一 admin int（现有 cosyvoice 那个）？建议先单一 int，plan-aware 单列。
4. **smart_preview_clone_enabled 旋钮**：本子方案 gate 用既有 `smart_auto_clone_enabled` 即可；那个占位旋钮是否改名/接入，还是继续留作"未来预览 lane"占位？建议**继续留占位**，本子方案不用它（避免混淆）。
