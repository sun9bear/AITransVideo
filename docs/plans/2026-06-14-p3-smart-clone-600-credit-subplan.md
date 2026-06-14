# P3 子方案：智能版自动克隆主音色 + 600 点预扣 / 退还

**状态：** PROPOSED / **待项目主 + CodeX 审架构后再写动钱代码**
**日期：** 2026-06-14
**父方案：** [`2026-06-14-anonymous-express-cosyvoice-clone-enable-plan.md`](2026-06-14-anonymous-express-cosyvoice-clone-enable-plan.md) §5
**架构决策（项目主已拍板）：** 600 点信用**在 gateway 建任务时预扣**（不在 pipeline）。

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

## 3. 架构：gateway 建任务时预扣（项目主拍板）

```
前端 smart submit（auto_voice_clone）
  → [新] 预扣弹窗"克隆主音色 -600 点，余额 X"→ 用户 confirm
  → gateway intercept_create_job（smart 分支）
       1. 既有：reserve 分钟点数（estimated minutes）
       2. [新] 若 smart_consent.auto_voice_clone=true + admin smart_auto_clone_enabled
              + 分钟预扣后剩余 ≥ 600 + 库未满
          → reserve 额外 600（独立 reason_code=smart_voice_clone_reserve_<jobid>，
            service_mode="smart"，shadow）
          → JobRecord 落 `smart_clone_credit_reserved=true` + `smart_clone_reserve_reason_code`
       3. 否则（无 consent / <600 / 库满）→ 不 reserve 克隆点；JobRecord
            `smart_clone_credit_reserved=false`（pipeline 据此**不克隆 → 预设**，plan §12.3
            "点数不足 600 继续用预设"）
  → pipeline 读 JobRecord snapshot：
       - smart_clone_credit_reserved=false → **不调 MiniMax 克隆**（强制 stub→preset）
       - =true → 克隆**仅主说话人 1 个**（real MiniMax）；结果(cloned voice_id / 失败原因)
         写进 job 产物 manifest（settlement 据此对账）
  → 结算 capture_full（gateway，既有终态结算）：
       - 读 pipeline 产物：克隆是否真成功产出 voice_id
       - 成功 → **capture 600**（reserve 转实扣）
       - 失败/未触发/preset 回落 → **release 600**（全额退还）
```

**为何安全**：reserve 在动 MiniMax **之前**（建任务时）；pipeline 只在已 reserve 时才克隆；结算按"是否真产出 clone voice"决定 capture 还是 release。**任何"reserved 但没成功克隆"都 release**（不漏退）。**任何"没 reserve"都不克隆**（不漏扣后克隆=白克隆）。

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

## 7. JobRecord 新字段（Job API JSON store，无需 DB migration）

- `smart_clone_credit_reserved: bool`（建任务时定，pipeline + settlement 读）。
- `smart_clone_reserve_reason_code: str`（release/capture 对账用）。
- pipeline 产物 manifest 加 `smart_clone_outcome: {cloned: bool, voice_id, reason_code}`（settlement 读）。

> Job API snapshot 已支持任意字段（`from_payload`）；mirror 侧不读（settlement 在 gateway，读 Job API + 产物）。

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
