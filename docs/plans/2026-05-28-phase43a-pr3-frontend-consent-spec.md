# Phase 4.3a PR3 — 前端 Express consent UI + availability wiring spec

**作者：** Claude (Opus 4.7 / 1M)
**版本：** v0.2（Codex 一轮 review 修订；待二轮，未实施）
**日期：** 2026-05-28
**变更摘要 v0.1 → v0.2（Codex 一轮 6 点）：**
- **§5 文案去掉"7 天自动失效"**（Codex 重点）：PR2 的 `temporary_expires_at=now+7d` 目前**只是元数据**，真正删 DashScope 临时音色的 sweeper 是 Phase 4.3b，尚未上线。文案不得承诺"7 天后自动删除"——改为"临时用途，不进入永久音色库；系统后续按清理策略处理"。
- **§2.6 新增决策 6：checkbox state 生命周期**——切换 service mode（express→studio/smart→express）或重新提交时**不得残留 `true`**；离开 Express 即 reset false。+ 守卫 `test_consent_state_resets_on_mode_switch`
- §2.1/§3 强化：`express_consent` 在 `service_mode==='express'` 时**无条件**带（未勾也发 `false`），措辞收紧防漂移
- §3/§7 强化：前端**绝不**发 `server_confirmed_at`（只发 `client_confirmed_at`）+ 守卫
- §2.2/§4.2 强化：availability fetch 失败 fail-closed 不渲染
- §2.5/§8 强化：PR3 纯前端，不改 gateway / src/pipeline / migration / admin_settings
**前置：** PR1（gateway 基础层）+ PR2（atomic reservation + pipeline）均已 merge（origin/main `f3d00086`）。
**主 spec：** [`2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md`](2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md)
**PR2 spec NG1：** "前端 consent checkbox / availability wiring → PR3（TranslationForm UI + 未勾选也发 `{auto_voice_clone:false}` 守卫）"

---

## §0 背景：PR1/PR2 留给 PR3 的契约

PR1/PR2 已经把 Express auto-clone 的**后端全链路**做完了，但**没有任何用户入口**——前端从不发送 `express_consent`，所以 pipeline 的 L4 consent gate 永远 fail → 永远 skip clone（即便 admin 开了主开关）。PR3 = 把用户入口打开。

PR3 直接复用、**不改动**的后端资产：

| 资产 | 位置 | PR3 用途 |
|---|---|---|
| `validate_express_consent(raw)` | `gateway/express_consent.py`（PR1-C） | 校验前端发来的 consent（2 字段，soft-skip 语义） |
| `express_consent` 写入 JobRecord + `server_confirmed_at` 后端生成 | `gateway/job_intercept.py`（PR1-C） | 接收 POST /jobs 的 consent |
| `GET /api/me/express-auto-clone-availability` → `{available, reason}` | `gateway/entitlements.py`（PR1-D） | 前端判断是否渲染 checkbox |
| L4 consent gate + 全失败降级 | `src/services/express/*`（PR2） | consent=true 才进 reserve→clone |

**关键判断：PR3 是纯前端 PR。** 后端 consent 校验 / availability endpoint / pipeline gating 全部就位，PR3 只加：consent checkbox + availability fetch + 提交 payload 接线 + TS 类型 + 静态守卫。**不碰任何 Python / 不碰 alembic / 不动 admin_settings 默认值。**

---

## §1 范围 / 非范围

### 1.1 范围（PR3 in-scope，纯前端）

- **G1 consent checkbox**：Express 模式下渲染"自动克隆主说话人音色（实验性）"勾选框，**默认未勾选**，文案让 consent 有意义（说明会触发一次音色克隆、临时 7 天、占用克隆配额）。
- **G2 availability gating**：checkbox 仅在 `service_mode==express` **且** `GET /api/me/express-auto-clone-availability` 返回 `available=true` 时渲染。fail-closed：fetch 失败 / `available=false` → 不渲染。
- **G3 提交 payload 接线**：Express 提交时**始终**带 `express_consent: {auto_voice_clone, client_confirmed_at}`（**未勾选也发 `auto_voice_clone:false`**，不省略字段）。
- **G4 TS 类型 + 静态守卫**：`CreateTranslationJobInput` 加字段；新增静态扫 guard 锁死 G3 的"未勾选也发 false"契约 + 默认未勾选 + availability gating。

### 1.2 非范围（PR3 out-of-scope）

- **NG1 不改后端**：consent validator / availability endpoint / job_intercept / pipeline 全部 PR1/PR2 已就位，PR3 一行 Python 不动。
- **NG2 不碰 alembic / migration**：reservation 表 migration 032 是 PR2 资产，PR3 不 apply、不新建。
- **NG3 不开启生产开关**：admin 主开关 `express_cosyvoice_auto_clone_enabled` 默认仍 False；PR3 merge 后**不**翻开关、**不**部署。
- **NG4 不做 daily_cap / active_temp_cap 的前端预检**：availability endpoint 明确不查 cap（spec §2.5）；cap 是 pipeline reserve 的事，用户看到 checkbox ≠ 一定能 clone（可能 reserve 时 cap_exceeded → 静默回预设）。前端不假装预检。
- **NG5 不做 consent 历史 / 审计 UI**：audit JSONL 是 admin-only 运维数据（主 spec §9.2），不向用户暴露。

### 1.3 验收 DoD

1. ✅ Express 模式 + availability=true → 渲染 consent checkbox，默认未勾选
2. ✅ availability=false（admin off / 非 allowlist / 未登录 / admin_settings 不可读）→ **不**渲染 checkbox
3. ✅ 非 Express 模式（studio / smart）→ **不**渲染 Express checkbox（互不污染；smart 有自己的 consent）
4. ✅ Express 提交**始终**发 `express_consent`，勾选 → `auto_voice_clone:true` + `client_confirmed_at`，未勾选 → `auto_voice_clone:false` + `client_confirmed_at:null`
5. ✅ `npx tsc --noEmit` 0 error；`npm run lint` 0 error
6. ✅ 静态守卫锁死 G3（未勾选也发 false）/ 默认未勾选 / availability gating
7. ✅ 后端零改动（守卫：PR3 diff 不含 `*.py` / `gateway/` / `src/` / alembic）
8. ✅ checkbox state 切换 service mode / 重新提交**不残留 true**（决策 6）
9. ✅ 文案**不承诺删除时限**（无"N 天后删除/失效"字样；4.3b sweeper 未上线）
10. ✅ 前端**绝不**构造 / 发送 `server_confirmed_at`

---

## §2 关键决策

### 2.1 决策 1：未勾选也发 `{auto_voice_clone:false}`（不省略字段）✅

后端 `validate_express_consent` 对**缺失** express_consent 返回 `express_consent_missing_or_invalid_type`（soft-skip）；虽然行为上等价于未勾选，但**语义上"缺字段"与"显式拒绝"不可区分**。Codex PR3 边界明确要求：未勾选也发 `auto_voice_clone:false`，让后端 / audit 能区分"用户明确没勾"（false）vs "前端 bug 漏发"（missing）。

实现：`submitTranslationJob` 在 `service_mode==='express'` 分支**无条件**设 `requestBody.express_consent = {auto_voice_clone: <checkboxState>, client_confirmed_at: <iso|null>}`。镜像现有 smart 分支 `if (service_mode==='smart') requestBody.smart_consent = {...}` 的模式。

### 2.2 决策 2：availability 双门控渲染（fail-closed）✅

checkbox 渲染条件 = `serviceMode==='express' && availability?.available === true`。

- availability fetch 失败 / 超时 → `available` 视为 false → 不渲染（fail-closed，与 endpoint 自身 fail-closed 一致）。
- `available=false` 的所有 reason（admin_flag_off / not_in_allowlist / unauthenticated / admin_settings_unavailable）→ 统一不渲染，**不**向用户展示具体 reason（隐私 + 避免暴露 allowlist/灰度状态）。

### 2.3 决策 3：checkbox 默认未勾选（opt-in）✅

付费 clone 必须用户**显式 opt-in**（CLAUDE.md 付费 API 硬约束）。checkbox state 初始 `false`。即使 availability=true，不勾就不 clone。

### 2.4 决策 4：client_confirmed_at 是辅助审计，不可信 ✅

勾选时设 `client_confirmed_at = new Date().toISOString()`；未勾选为 `null`。后端 `validate_express_consent` 明确这是 untrusted audit-assist，真正可信的 `server_confirmed_at` 由 gateway 生成（PR1-C）。前端**不**依赖它做任何 gating。

### 2.5 决策 5：PR3 纯前端，后端零改动 ✅

守卫测试断言 PR3 的 diff 不触碰 `gateway/` / `src/` / `*.py` / alembic。任何后端改动都说明设计漂移（PR1/PR2 应已覆盖）。

### 2.6 决策 6：checkbox state 生命周期——切换 mode / 重提交不残留 true（Codex 一轮）✅

consent 是**付费 opt-in**，绝不能在用户不知情时残留 `true`。硬约束：

- **初始** `false`（决策 3）。
- **切换 service mode**（express → studio/smart，或再切回 express）→ consent state **reset 为 `false`**。不能出现"用户先在 express 勾了，切到 studio 又切回，checkbox 仍是勾的"。
- **availability 变为 false**（重新 fetch 后不可用）→ state 强制 `false`（checkbox 都不渲染了，state 不能残留 true 被提交）。
- **重新提交 / 表单复用**（同一 form 实例连续建多个任务）→ 每次提交读的是当前 checkbox state；不依赖上次提交的残留值。提交后若 form 不销毁，建议 reset false（避免下一个任务静默继承上一个的勾选）。

实现要点：consent state 与 service mode 绑定——`serviceMode !== 'express'` 时 effective consent 恒为 false（即使内部 state 残留，submit 时也按 `serviceMode==='express' ? checkboxState : false` 取值）。守卫 `test_consent_state_resets_on_mode_switch` 锁此契约。

---

## §3 consent payload 契约

Express 提交 body 里的 `express_consent` 字段（与 `validate_express_consent` 2 字段严格对齐）：

```jsonc
// 勾选
"express_consent": {
  "auto_voice_clone": true,
  "client_confirmed_at": "2026-05-28T03:44:50.000Z"   // 勾选时刻 ISO 8601 UTC
}
// 未勾选（仍必发）
"express_consent": {
  "auto_voice_clone": false,
  "client_confirmed_at": null
}
```

- **只在 `service_mode==='express'` 时发**（studio/smart 不发；smart 走 `smart_consent`）。
- `auto_voice_clone` 必须是 JS `boolean`（不是 `"true"` / `1`）——后端 strict-bool 校验，coercion 会被拒 `auto_voice_clone_not_bool`。
- `server_confirmed_at` **前端绝不发**——后端单一来源生成。前端发了也会被忽略（PR1-C `validate_express_consent` 不读它）。

---

## §4 availability fetch + gating

### 4.1 新增 API client（`lib/api/entitlements.ts`）

```ts
export interface ExpressAutoCloneAvailability {
  available: boolean
  reason: string   // ok / unauthenticated / admin_settings_unavailable / admin_flag_off / not_in_allowlist
}

export async function getExpressAutoCloneAvailability(): Promise<ExpressAutoCloneAvailability> {
  const resp = await fetch('/api/me/express-auto-clone-availability', { credentials: 'include' })
  if (!resp.ok) {
    // fail-closed：任何非 2xx → 不可用
    return { available: false, reason: 'fetch_failed' }
  }
  return resp.json() as Promise<ExpressAutoCloneAvailability>
}
```

镜像现有 `getEntitlements()`（直 fetch gateway `/api/me/*`，非 Job API base）。

### 4.2 TranslationForm 接线

- 在 Express 模式被选中时 fetch availability（或 form mount 时 fetch 一次，缓存于 state）。
- `available===true` → 渲染 checkbox；否则不渲染（且 consent state 保持 false）。
- fetch 失败 → `{available:false}` → 不渲染（fail-closed，不阻塞表单提交，只是没有 clone 入口）。

---

## §5 checkbox 文案（中文，consent 有意义）

```
☐ 自动克隆主说话人音色（实验性）

   勾选后，系统会用视频中占比最高的说话人的一小段语音（约 10–20 秒）
   克隆一个临时音色用于本次配音，让主说话人的声音更贴近原片。
   · 该音色为本次任务临时使用，不进入你的永久音色库；系统后续会按清理策略处理
   · 会占用一次音色克隆配额
   · 失败时自动改用预设音色，不影响配音完成
```

要点：
- "实验性"标注（canary）
- 明确"会克隆一次"（付费动作知情）
- 明确"临时 / 不进永久库"（与 PR2 `is_temporary` 一致）
- 明确"占用克隆配额"（cap 知情）
- 明确"失败回预设"（不焦虑）
- **默认未勾选**

> **不得承诺"N 天后自动删除"（Codex 一轮重点）**：PR2 的 `temporary_expires_at=now+7d`
> 目前**只是元数据**，真正删 DashScope 临时音色的 sweeper 是 Phase 4.3b（未上线）。
> 文案只能说"临时用途 / 不进永久库 / 后续按清理策略处理"，**不写死天数 / 不承诺自动删除**——
> 否则用户预期与系统实际行为不符（音色实际可能在 4.3b 上线前一直存在）。

> 文案最终措辞可在实施时微调，但**默认未勾选 + 上述知情点 + 不承诺删除时限**是硬约束。

---

## §6 前端文件改动清单

| 文件 | 改动 |
|---|---|
| `lib/api/entitlements.ts` | + `ExpressAutoCloneAvailability` 接口 + `getExpressAutoCloneAvailability()` |
| `types/jobs.ts` | `CreateTranslationJobInput` + `expressAutoVoiceClone?: boolean`（checkbox state；form → submit 透传） |
| `lib/api/jobs.ts` | `submitTranslationJob`：`service_mode==='express'` 分支无条件加 `express_consent`（镜像 smart 分支） |
| `components/workspace/TranslationForm.tsx` | + availability fetch + consent checkbox（Express + available 才渲染，默认 false）+ 把 state 传入 submit |

**预计 diff：~120–180 行前端，0 行后端。**

---

## §7 测试守卫（前端静态扫 + tsc + lint）

> 本项目前端无 Vitest/RTL/jsdom，用 **Python 静态扫 + tsc --noEmit + npm run lint**（与
> `tests/test_phase2_download_backend.py::test_frontend_has_no_r2_leakage` 同模式）。

**新文件**：`tests/test_phase43a_pr3_frontend_consent.py`

- `test_express_submission_always_sends_consent` — 扫 `jobs.ts`：`service_mode==='express'` 分支设 `express_consent` 且含 `auto_voice_clone`（锁 G3 "未勾选也发 false"）
- `test_consent_payload_only_for_express` — 扫 `jobs.ts`：`express_consent` 只在 express 分支设置（不污染 studio/smart）
- `test_checkbox_default_unchecked` — 扫 `TranslationForm.tsx`：consent state 初始为 `false`
- `test_checkbox_gated_by_availability` — 扫 `TranslationForm.tsx`：checkbox 渲染受 `available` 控制（出现 `available` 判断 + express 判断）
- `test_consent_state_resets_on_mode_switch` — 扫 `TranslationForm.tsx` / `jobs.ts`：submit 取值为 `serviceMode==='express' ? checkboxState : false`（非 express 恒 false，不残留 true）（决策 6）
- `test_availability_client_fail_closed` — 扫 `entitlements.ts`：`getExpressAutoCloneAvailability` 非 2xx 返回 `available:false`
- `test_pr3_does_not_touch_backend` — 守卫：PR3 不应改 `gateway/**` / `src/**` / `*.py` / alembic（实施时人工/CI diff 确认；可做轻量 grep guard）
- `test_frontend_no_server_confirmed_at` — 扫前端：**不**出现前端构造 `server_confirmed_at`（后端单一来源）
- `test_consent_copy_no_deletion_deadline` — 扫文案：consent checkbox 文案**不含**"天后删除/失效"类承诺（DoD #9；4.3b sweeper 未上线）

CI：现有 `frontend` job（`tsc --noEmit` + `lint`）覆盖类型/语法；Python 静态扫进 `backend` job 既有 pytest。

---

## §8 边界与约束（Codex PR3 红线）

| 项 | 约束 |
|---|---|
| alembic / migration | ❌ 不 apply、不新建（migration 032 是 PR2 资产，运行库尚未建表） |
| 部署 | ❌ PR3 merge 后**不**部署；部署阶段单独授权 |
| 生产开关 | ❌ `express_cosyvoice_auto_clone_enabled` 保持 False；PR3 不翻 |
| 后端代码 | ❌ 一行 Python 不改（守卫锁死） |
| 默认勾选 | ❌ 绝不默认勾选（付费 opt-in 硬约束） |
| reason 暴露 | ❌ 不向用户展示 availability 的具体 reason（隐私 + 灰度状态） |

**PR3 merge ≠ 功能上线**：admin 主开关仍 False，且运行库无 reservation 表。真正灰度需要单独授权的部署阶段：apply migration 032 → admin 开开关 + 把灰度 user_id 加进 allowlist → 观察。

---

## §9 风险矩阵

| # | 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|---|
| R1 | 前端漏发 express_consent → 后端当 missing | M | M | §2.1 无条件发 + `test_express_submission_always_sends_consent` 守卫 |
| R2 | 默认勾选 / 误勾选触发付费 clone | L | High | §2.3 默认 false + `test_checkbox_default_unchecked` + opt-in 文案 |
| R3 | availability fetch 失败导致表单卡死 | L | M | §4.2 fail-closed 不阻塞提交，只是不渲染 checkbox |
| R4 | 用户以为勾了就一定 clone（实际可能 cap_exceeded 回预设）| M | L | §5 文案"失败回预设"；NG4 不假装预检 |
| R5 | PR3 误改后端引入未审计行为 | L | High | §2.5 + `test_pr3_does_not_touch_backend` 守卫 |
| R6 | checkbox 残留 true（切 mode/重提交）→ 用户不知情触发付费 clone | M | High | §2.6 决策 6：非 express 恒 false + reset + `test_consent_state_resets_on_mode_switch` |
| R7 | 文案承诺"7 天删除"但 4.3b sweeper 未上线 → 用户预期与实际不符 | M | M | §5 + DoD #9 + `test_consent_copy_no_deletion_deadline`：不写删除时限 |

---

## §10 实施分阶段

| 阶段 | 内容 | 估时 |
|---|---|---|
| **PR3-A** | 守卫测试先行（静态扫 jobs.ts / TranslationForm / entitlements；后端零改动 guard） | 2-3h |
| **PR3-B** | `entitlements.ts` availability client + `types/jobs.ts` 字段 | 1-2h |
| **PR3-C** | `jobs.ts` submit 接线（无条件发 express_consent） | 1-2h |
| **PR3-D** | `TranslationForm.tsx` checkbox + availability gating + 文案 | 3-5h |
| **PR3-E** | tsc + lint + 静态守卫全绿 + 开 GitHub PR + @codex review | 2-3h |

**总估时**：~9-15 工时

---

## §11 实施前自检

- [ ] 用户/Codex 已审 §1 范围（PR3 = 纯前端）
- [ ] 已审 §2 五决策（未勾选发 false / availability 双门控 fail-closed / 默认未勾选 / client_at 不可信 / 后端零改动）
- [ ] 已审 §3 consent payload 契约（2 字段，不发 server_confirmed_at）
- [ ] 已审 §5 checkbox 文案 5 个知情点
- [ ] 已审 §7 静态守卫策略 + §8 边界红线
- [ ] @codex review 此 spec v0.1
- [ ] Codex 反馈纳入后再开 PR3 实施
