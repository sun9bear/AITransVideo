# 免费预览接入真实克隆流程实施方案（拦 MiniMax / 放行 CosyVoice）

**状态：** PROPOSED / **待项目主 review 批准后才能动防线代码**
**日期：** 2026-06-14
**作者：** Claude（P0 阶段：读方案 + 核实防线 + 出实施计划）
**权威基线：** [`docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md`](2026-06-01-anonymous-preview-funnel-ux-plan.md)（唯一权威，一切以它为准）

> **v2 修订（2026-06-14，CodeX review 后）：** 采纳 CodeX 5 条 finding + Q1 拍板。变更摘要：
> 1. **匿名 consent**：`anonymous_consent` 只含 `voice_rights_confirmed`、**不含** `auto_voice_clone`，且只进 audit 不进 payload → 改为**新增显式匿名 Express 克隆 opt-in**（前端另发 `express_consent`，gateway 新增 strict-bool 校验器，服务端盖时间戳）。原"注入已校验 consent_payload"方案作废。
> 2. **admission 在运行时路径**（经 `anonymous_preview_policy.admit_for_free_preview` → create L1319，adapter 硬传 `mode="free"`）——P4 不只改恒抛 helper，须改 adapter + 补运行时回归。
> 3. **克隆点数 500→600** 不受 default-OFF 保护，直接影响现有计费；改为"更新默认 schema + admin/runtime snapshot 发布"，单独灰度。
> 4. **reservation owner**：`express_clone_reservations.user_id` 是非空 UUID FK + 锁 `users` 行 → 用 **sentinel user 作全局 owner**（cap 语义=全局）。
> 5. **mirror consent**：pipeline **只读 Job API snapshot 的 `express_consent`**，不读 Gateway `jobs` mirror（避免 mirror migration），加守卫测试。
> 6. **Q1 拍板=方案 A**（CodeX 确认代码已支持）；V1 从产品决策点降为 **P2 第一条回归测试**。

> ⚠️ **这是全项目最敏感的安全红线改动。** CLAUDE.md「付费 API 不能自动调用」源于 2026-04-05 MiniMax 余额被自动克隆兜底跑干的真实事故。本方案把现有「匿名绝不克隆」的三道防线 **改写**（不是删除）为「放行 CosyVoice 国内免费克隆、拦截 MiniMax 付费克隆」，并用守卫测试断言新边界。**任何防线代码改动必须先经本方案 review 批准。**

---

## 0. 一句话目标

让免费/快捷版预览走与付费用户**一致**的真实克隆流程：

- **匿名 / 登录快捷版** → 只允许 **CosyVoice 国内 v3.5 免费克隆**（注册零费用、合成是管线既有步骤），失败回 **CosyVoice 预设音色**。
- **登录智能版** → **MiniMax 克隆，预扣 600 点**（用户知情可见、含退还机制），失败回预设音色。
- 🚫 任何 except / fallback / batch / retry 路径**绝不**自动调 MiniMax（扣账户余额）克隆。
- 🚫 provider 之间不自动 fallback；克隆失败只回预设，不换付费 provider。

落点依据：权威方案 §12.2（快捷版可走 CosyVoice 临时克隆）、§12.3（智能版 600 点预扣）、§14.0（admin 旋钮）、§18 Phase 3b/4，以及 [[feedback_free_touchpoint_quality]]（免费触点必须展示真实管线效果）。

---

## 1. 现状核实（已逐文件确认，2026-06-14）

### 1.1 运行时门控链（匿名 express 真实路径）

```
前端 → gateway/anonymous_preview_api.py::anonymous_preview_create (L1204)
  ├─ validate_anonymous_consent(body.anonymous_consent) → consent_payload + server_confirmed_at  ← consent 已校验但未注入 payload
  ├─ record.mode 分流：express → _resolve_express_payload_tts_provider() / service_mode="express"
  ├─ admit_for_free_preview(duration, apf_limits)  ← 时长闸（非克隆决策）
  ├─ validate_create_payload(payload)  ← 防线③ gateway 侧：白名单 + FORBIDDEN_PAYLOAD_FIELDS
  └─ POST Job API → JobRecord(service_mode="express", anonymous_preview=true, voice_strategy=preset_mapping, express_consent=?)
        ↓ pipeline worker
src/pipeline/process.py::run()
  ├─ L2719 job_service_mode = snap('service_mode','express')
  ├─ L2722 job_voice_strategy = snap('voice_strategy','preset_mapping')
  ├─ L2727 job_anonymous_preview = snap('anonymous_preview',False) is True
  ├─ L2728-2738 防线②：if anonymous_preview and voice_strategy!='preset_mapping' → 强制 preset_mapping
  └─ L3589 if job_service_mode=="express" and approved_voice_selection is None:
        maybe_run_express_auto_clone(...)  ← 已接线的 CosyVoice auto-clone 单一入口
```

**关键架构事实：克隆只发生在 pipeline（process.py → `maybe_run_express_auto_clone`），不在 gateway intake。** Gateway 容器无 pydub、从不 import 克隆模块。这决定了三道防线的改写边界（见 §3）。

### 1.2 三道防线精确位置（已核实 file:line）

| 防线 | 位置 | 当前行为 | 是否在运行时路径 |
|---|---|---|---|
| **①** admission 契约 | [`src/services/anonymous_preview_admission.py`](../../src/services/anonymous_preview_admission.py) | 正常 emit `PRESET_ONLY`；EXPRESS+flag emit `EXPRESS_TEMPORARY_CLONE_GATE`；`raise_clone_provider_boundary()` **恒抛 NotImplementedError** | **否**——`evaluate_anonymous_preview_admission` 仅被 `test_anonymous_preview_t6_policy.py` 调用（codegraph callers 确认）。纯 contract shell |
| **②** pipeline 强制预设 | [`src/pipeline/process.py:2728-2738`](../../src/pipeline/process.py) | 匿名任务 `voice_strategy != preset_mapping` → 强制 `preset_mapping`（注释自称"防 clone 第三道防线"） | **是**——运行时兜底 |
| **③** gateway 侧守卫 | payload 白名单 + import 黑名单 + AST 守卫 | 见 §1.3 | **是**——create 时拦截 |

### 1.3 防线③ 的具体守卫清单（已核实）

- [`gateway/anonymous_preview_payload_spec.py`](../../gateway/anonymous_preview_payload_spec.py)：
  - `ANONYMOUS_PREVIEW_PAYLOAD_SPEC` 白名单（**不含** `express_consent`）。
  - `FORBIDDEN_PAYLOAD_FIELDS = {voice_a, voice_b, voice_clone, voiceclone_reference_path, free_consent}`。
- `tests/test_anonymous_express_t4_payload_consistency.py:470` — gateway 模块 import 黑名单 `banned = ("voice_clone","user_voice","minimax","clone")`；`FORBIDDEN_PAYLOAD_FIELDS` 断言含 `voice_clone` / `voiceclone_reference_path`。
- `tests/test_anonymous_preview_rate_limit.py:403` — rate-limit 模块 `forbidden_src_prefixes` 含 `src.services.voice_clone`。
- `tests/test_anonymous_preview_t3_upload.py:704` — gateway 模块不 import `services.jobs`（pydub 守卫，与克隆无关，**不动**）。
- `tests/test_apf3a_anonymous_preview_contract.py` / `test_anonymous_preview_t6_policy.py` — 防线① 契约测试。

### 1.4 已有可复用基建（已核实，不重造）

- **`src/services/express/pipeline_clients.py::maybe_run_express_auto_clone`** — Express CosyVoice auto-clone 单一入口，已含 4 层闸（L1 admin 主开关默认 False / L2 worker env / L3 allowlist fail-closed / L4 consent），CosyVoice-only，失败回预设、**绝不 MiniMax**。process.py:3589 已接线。
- **武汉 CosyVoice worker**（[[mainland_worker_wuhan_access]]）；temp voice 写 `user_voices.is_temporary=true` + `temporary_expires_at`；reservation/cleanup sweeper 已有。
- **已有 admin 旋钮**（`gateway/admin_settings.py`）：
  - `express_cosyvoice_auto_clone_enabled=False`（登录快捷版主开关）+ allowlist/min_ratio/min_lines/target_model/sample_max_seconds/per_user_daily_cap=5/per_user_active_temp_cap=3/reservation_ttl_minutes=30。
  - `anonymous_express_enabled=False` / `anonymous_express_daily_global_cap=50` / `express_tts_provider="cosyvoice"`。
  - `VALID_ANON_EXPRESS_TTS_PROVIDERS = {cosyvoice, volcengine}`（`anonymous_lane.py`，刻意不含 mimo）。
- **赠点 800 已就位**：`gateway/plan_catalog.py:378` `_GA={"free":500,"trial":300,...}` → 新用户 free bucket 500 + trial bucket 300 = **800**。**无需改动。**
- **克隆点数 500（需→600）**：真源 `gateway/pricing_schema.py:38 voice_clone_cost_credits=500`（+ `:226`）；`admin_settings.py:142` 是 deprecated compat 副本。
- 4 个 consent 模块全部存在：`gateway/{anonymous,express,smart,free}_consent.py`。

### 1.5 当前阻断克隆的真实原因（不是表面的"三道防线"）

逐行核实后，**真正阻断匿名 express 走 CosyVoice 克隆的是两点**，而非笼统的"三道防线"：

1. **匿名无显式克隆 consent**：`anonymous_consent` 只含 `voice_rights_confirmed`（内容/声音权利确认），**不含** `auto_voice_clone`，且 docstring 明写"只进 preview record audit、不进 Job API payload"（[`anonymous_consent.py`](../../gateway/anonymous_consent.py)）→ `maybe_run_express_auto_clone` 的 L4 consent 闸（要求 `auto_voice_clone is True` + `server_confirmed_at`）必然不过 → skip。**修复需新增显式匿名 Express 克隆 opt-in，不能复用 `anonymous_consent`**（CodeX P1-a）。
2. **`maybe_run_express_auto_clone` 需要真实 `user_id` + allowlist 成员**：匿名 session 无真实 user_id（sentinel 系统用户），L3 allowlist fail-closed → skip。匿名缺一条不依赖 user_id/allowlist 的等价闸。

> 防线②（强制 preset_mapping）其实**不**阻断 L3589 的 auto-clone（它只看 `service_mode=="express"`，不看 voice_strategy）。**Q1 已拍板=方案 A**：CodeX 确认代码已支持 cloned voice 经 `_speaker_voices`+`requires_worker` 旁路注入（auto_clone 成功写 routing → process 强制 `tts_provider="cosyvoice"` → TTSGenerator 见 `requires_worker=True` 强制走武汉 worker），**preset_mapping 不阻断克隆音色**。原"待验证点 V1"降为 **P2 第一条回归测试**（断言旁路链路成立），不再是产品决策点。

---

## 2. 目标状态（不可动摇的安全护栏）

| 档位 | 允许的克隆 | 触发条件 | 失败回落 |
|---|---|---|---|
| 匿名 / 登录快捷版 | **仅 CosyVoice 国内 v3.5 免费克隆** | server-confirmed consent + admin 主开关 + 全局 cap + worker 可用 + 主说话人阈值 + 原子 reservation | **→ CosyVoice 预设音色** |
| 登录智能版 | **MiniMax 克隆，预扣 600 点**（知情可见、含退还） | 显式 consent + 点数 ≥600 + 个人音色库未满 + provider call 前预扣 | **→ 预设音色 / 复用已有音色**，并退回预扣点数 |

**红线（实施后必须有测试守住）：**
- 🚫 任何 except/fallback/batch/retry 路径绝不自动调 MiniMax 克隆。
- 🚫 provider 间不自动 fallback；克隆失败只回预设，不换付费 provider。
- CLAUDE.md「付费 API 不能自动调用」**保留**，只在该段补一句澄清：CosyVoice 国内免费克隆不在其射程（注册免费 + 克隆费已向用户收取，商业化网站；对照 [[feedback_paid_api_constraint_scope]]：硬约束 DNA 是"烧用户账单/额度/账户永久库存"，CosyVoice 注册零扣费、不占 MiniMax slot）。

---

## 3. 三道防线的精确改法（**review 重点**）

> 总原则：**防线不删，改写成"拦 MiniMax 放行 CosyVoice"，守卫测试同步改为断言新边界。** Gateway intake 从不 import 克隆模块这条**不变**（克隆是 pipeline 职责），所以防线③的"gateway 无克隆 import"守卫**保留**。

### 3.1 防线③（gateway 侧）—— 改 payload 白名单 + 细化守卫断言

**新增显式匿名 Express 克隆 opt-in（CodeX P1-a，不复用 `anonymous_consent`）：**
- 前端匿名 express create 在 `anonymous_consent`（内容/声音权利，保持独立）之外，**额外发送** `express_consent={auto_voice_clone: <bool>}`（用户在 express 卡片显式勾选"允许克隆我的音色"）。
- gateway **新增校验器** `validate_anonymous_express_clone_consent(raw)`（照 `anonymous_consent.py` strict-bool 模式：拒 `1`/`"true"` coercion；`auto_voice_clone` 缺省/非 True → 视为不克隆，**不报错、回预设**）。校验通过后服务端盖 `server_confirmed_at`。
- 仅当该 opt-in 校验为 True 时，create 才把 `express_consent={auto_voice_clone:true, server_confirmed_at:...}` 注入 payload；未勾选 → **不注入**，express 走 CosyVoice 预设（不是 fail-closed 拒绝，是正常预设路径）。

**改 `gateway/anonymous_preview_payload_spec.py`：**
- `ANONYMOUS_PREVIEW_PAYLOAD_SPEC` **新增** `express_consent`（仅 express lane 且用户勾选克隆时注入；free lane 永不带）。
- `FORBIDDEN_PAYLOAD_FIELDS` **保持** `voice_a/voice_b/voice_clone/voiceclone_reference_path/free_consent` 全部禁——这些是 MiniMax/MiMo-voiceclone 路径字段，CosyVoice express auto-clone **不**经它们传参（克隆在 pipeline 内由 consent+admin 闸触发，不靠 payload 字段）。

**consent truth 单一真源（CodeX P2-b）：** pipeline **只读 Job API JobRecord snapshot 的 `express_consent`**（Job API 已持久化，`src/services/jobs/api.py`），**不读** Gateway `jobs` mirror（mirror 当前无该字段，避免加 mirror migration）。加守卫测试断言 pipeline 的 `_snap("express_consent")` 来源是 Job API snapshot。

**改守卫断言（`test_anonymous_express_t4_payload_consistency.py`）：**
- import 黑名单 `banned=("voice_clone","user_voice","minimax","clone")` 对 **gateway 模块**保留（gateway 仍不 import 任何克隆模块）。但需确认黑名单扫的是 **import 模块名 / payload 字段名**，不是误伤 `express_consent` 内嵌的 `auto_voice_clone` value——核实后 `express_consent` 作字段名不含 `clone`，`auto_voice_clone` 是嵌套 dict 的 key/value 不被字段名扫中。**新增断言**：`express_consent` 在白名单内；`voice_clone`/`voiceclone_reference_path` 仍在禁列。
- `test_anonymous_preview_rate_limit.py` 的 `src.services.voice_clone` 黑名单**保留不动**（rate-limit 模块永不该 import 克隆）。

> 净效果：gateway 侧守卫的"无克隆 import"语义**完全保留**，只放开"consent 字段可穿透"这一条最小必要口子。

### 3.2 防线②（pipeline 强制预设）—— 改写为"拦 MiniMax 放行 CosyVoice"

`src/pipeline/process.py:2728-2738` 当前逻辑：匿名 + `voice_strategy!=preset_mapping` → 强制 `preset_mapping`。

**Q1 已拍板 = 方案 A（保留 `preset_mapping`，克隆经 speaker routing 旁路注入）。** CodeX 确认代码已支持，无需改 dispatch：

- **保留**强制 `preset_mapping` 这行**不动**。CosyVoice 克隆走 L3589 `maybe_run_express_auto_clone`：成功时把 cloned voice_id 注入 `_speaker_voices` + `_speaker_voice_routing.requires_worker=True`（[`auto_clone.py`](../../src/services/express/auto_clone.py)）→ process 把 routing 写到 segment 并强制 `tts_provider="cosyvoice"`（[`process.py`](../../src/pipeline/process.py)）→ `TTSGenerator` 见 `requires_worker=True` 强制走武汉 worker（[`tts_generator.py:1360`](../../src/services/tts/tts_generator.py)）。`voice_strategy=preset_mapping` **不阻断**克隆音色。
- 防线② 的语义升级为："匿名任务 `voice_strategy` 字段恒 `preset_mapping`，但 CosyVoice 克隆经 speaker routing 旁路注入"。同时**保留**对任何会路由到 MiniMax 克隆 / `voiceclone_reference_path` 的 strategy 的强制拦截。

**V1 → P2 第一条回归测试（不是产品决策点）**：断言 cloned CosyVoice voice 经 `_speaker_voices`+`requires_worker` 注入后，`preset_mapping` 下 `tts_generator` dispatch 到 mainland worker。守卫测试另断言："匿名任意 mode 注入 minimax/voiceclone 类 strategy → 仍强制回 preset"。

> （方案 B：新增 `express_cosyvoice_clone` strategy + 改 dispatch——**已否决**，方案 A 无需改 dispatch，侵入更小。）

### 3.3 防线①（admission 契约）—— 改写契约 + 测试断言新边界

**纠正（CodeX P2-a）：** 只有 `raise_clone_provider_boundary()` 是测试专用；`evaluate_anonymous_preview_admission` **确在运行时路径**——经 [`anonymous_preview_policy.admit_for_free_preview`](../../gateway/anonymous_preview_policy.py) 在 create L1319 调用，但该 adapter **硬传 `mode="free"` + `anonymous_express_cosyvoice_clone_enabled=False`**，故现状恒 emit `PRESET_ONLY`。改写须分两层：

- **运行时 adapter（`anonymous_preview_policy.py::admit_for_free_preview`）**：express lane 改传 `mode="express"` + 真实 `anonymous_express_cosyvoice_clone_enabled`（来自 admin settings），使 express+clone-on 时 emit `EXPRESS_TEMPORARY_CLONE_GATE`。**补运行时回归测试**（不只改契约测试）。
- **契约 helper（`anonymous_preview_admission.py`）—— ⚠️ 实现修正（P4 落地 commit 9418f160，CodeX 复核确认；本条覆盖上方 v2 的初版设想）**：经深读契约确认，`raise_clone_provider_boundary()` 守护的是"**本契约模块的调用图**永不变成 clone 执行器"，而**真克隆已在 pipeline `maybe_run_express_auto_clone` 接线**（完全不经此契约模块）。因此 **boundary helper 保留恒抛 `NotImplementedError`——不拆分、不改行为**（~10 个 PR#23 r7 安全硬化测试保持绿），只更新 docstring 说明"CosyVoice 临时克隆已在 pipeline 接线、本模块仍是纯决策 shell、MiniMax 绝不经匿名/快捷路径"。**不**引入 `raise_paid_clone_provider_boundary`，**不**让 `EXPRESS_TEMPORARY_CLONE_GATE` 停止恒抛（它本就不抛——它是 `voice_strategy` 枚举值，恒抛的是 boundary helper）。`EXPRESS_TEMPORARY_CLONE_GATE` 仍是契约信号，**不被 create 消费**（payload voice_strategy 恒 `preset_mapping`）；adapter mode-aware 让该信号诚实反映 admin 旋钮，但**不改运行时克隆**（克隆由 pipeline 独立 gating）。
- 对应测试（`test_apf3a_anonymous_preview_contract.py` / `test_anonymous_preview_t6_policy.py` / 新增 `test_anon_clone_enable_t4_admission.py`）：boundary helper **仍断言恒抛**（不改）；新增 adapter `mode="express"`+flag → `EXPRESS_TEMPORARY_CLONE_GATE` 运行时断言 + "consumed 字段（decision/duration/artifact_policy）free vs express 一致"零回归断言。

### 3.4 匿名 gate 缺口 —— 让 `maybe_run_express_auto_clone` 支持匿名（无 user_id/allowlist）

§1.5 第 2 点的核心缺口。**扩展 `maybe_run_express_auto_clone`（不新建第二条克隆路径）：**

- 新增匿名分支：当任务是匿名 express（`anonymous_preview=true`）时，L1/L3 闸从"`express_cosyvoice_auto_clone_enabled` + user allowlist"切换为：
  - **L1'**：`anonymous_express_cosyvoice_clone_enabled`（**新增 admin 旋钮**，默认 False）。
  - **L3'**：不用 user allowlist（匿名无 user role），改用 **全局 fail-closed cap**：`anonymous_clone_daily_global_cap` + `anonymous_clone_active_cap`（**新增旋钮**，权威方案 §14.0/§18 已拍板 100/20）。
  - **L2/L4 不变**：worker env + server-confirmed consent（匿名 express 克隆 opt-in 经 §3.1 注入）。
  - **reservation owner（CodeX P1-b 已定）**：`express_clone_reservations.user_id` 是非空 UUID 外键 + service 锁 `users` 行（[`032 migration`](../../gateway/alembic/versions/032_express_clone_reservations.py) / [`express_reservation_service.py`](../../gateway/express_reservation_service.py)），session id hash **不能**直接作 owner。**采用 sentinel 系统用户作全局 owner** → `anonymous_clone_active_cap` 语义 = **全局活跃上限**（非 per-anonymous），叠加 `anonymous_clone_daily_global_cap` 双闸；不迁移 reservation 表（避免 owner_scope/owner_key migration）。
  - temp voice 写 `is_temporary=true`+TTL，不进永久个人音色库（权威方案 §12.2）。
- 失败/任一闸不过 → return None → 回 CosyVoice 预设（与现有语义一致）。

---

## 4. Admin 旋钮 + Consent（P1，全部默认 OFF 休眠上线）

### 4.1 新增 admin 旋钮（`gateway/admin_settings.py` + migration）

| 旋钮 | 默认 | 用途 | 权威依据 |
|---|---|---|---|
| `anonymous_express_cosyvoice_clone_enabled` | `False` | 匿名 express CosyVoice 克隆主开关 | §14.0 |
| `anonymous_clone_daily_global_cap` | `100` | 匿名克隆每日全局上限（fail-closed） | §14.0/§18 |
| `anonymous_clone_active_cap` | `20` | 匿名活跃临时克隆上限 | §14.0/§18 |
| `smart_preview_clone_enabled` | `False` | 登录智能版预览克隆主开关 | §12.3 |
| `smart_preview_clone_daily_global_cap` | `200` | Smart 预览克隆每日全局上限 | §18 |
| `smart_preview_clone_inflight_cap` | `5` | Smart 预览克隆并发上限 | §18 |

> **登录快捷版克隆已有旋钮** `express_cosyvoice_auto_clone_enabled`，复用，不新增。
> 加列必跑 migration（哪怕 flag 关，见 [[feedback_apf_deploy_incident]]）。`StrictBool` + `field_validator` cap 上下界（照 `anonymous_express_daily_global_cap` 现有 validator 模式）。

### 4.2 克隆点数 500 → 600（⚠️ 非 default-OFF 保护，CodeX P3）

> **生产真源是 `/opt/aivideotrans/config/pricing_runtime.json`**（[`pricing_runtime.py`](../../gateway/pricing_runtime.py)：缺失才回退 `build_default_pricing_payload()`）。仓库内 `pricing_schema.py` 只是 default。**500→600 会立即影响现有 MiniMax 克隆计费，不受任何 flag 保护**——须**单独灰度**、与 admin 沟通发布时机。

- 改默认 schema `gateway/pricing_schema.py:38` + `:226` `voice_clone_cost_credits=600`。
- 经 **admin / runtime snapshot 发布**生产价（写 `pricing_runtime.json`），不是改代码即生效。
- `admin_settings.py:142` deprecated compat 副本同步或留注释。
- 同步前端展示 + 估算 + 测试。
- **建议**：本项与克隆功能解耦，可在智能版 P3 上线前由项目主单独拍板发布。

### 4.3 Consent（CodeX P1-a 修正）

- **匿名 express 克隆 opt-in**：`anonymous_consent.py` **只**含 `voice_rights_confirmed`（内容权利），**不含** `auto_voice_clone`——**新增**独立校验器 `validate_anonymous_express_clone_consent`（strict-bool，照 `anonymous_consent.py` 模式，服务端盖 `server_confirmed_at`），见 §3.1。两个 consent 各司其职、不混用。
- **登录快捷版**：`express_consent.py` 已有 `auto_voice_clone` strict-bool + `server_confirmed_at`（`maybe_run_express_auto_clone` 的 `_has_consent` 已消费），复用。
- **智能版**：`gateway/smart_consent.py` 扩展 600 点预扣 consent（克隆/声音授权显式确认）。
- 前端 UI（express 卡片克隆勾选 + 智能版 600 点预扣展示）按权威方案 §16.3/§17。

---

## 5. 智能版 600 点预扣 + 退还（P3）

> **⚠️ P3 实施状态（2026-06-14，CodeX 最终复核校正）：智能版 600 点 MiniMax 克隆"核心能力已存在于既有路径"；本任务新增的是 600 点定价 + 占位旋钮，smart **预览 lane** 刻意延后。**
>
> **校正前文（CodeX 最终复核）**：早前曾误述"smart MiniMax auto-clone 被全 stub 成 fail-closed"。**准确事实**：`src/pipeline/process.py` 的真 `build_smart_clone_provider()`（MiniMax-capable）在 **smart 全量任务**路径**已接线**（process.py ~4468-4472）——当 `smart_auto_clone_enabled`（默认 True）+ 用户 `smart_consent.auto_voice_clone` opt-in + 有 main speaker + quota 可用 时调用真 MiniMax 克隆；条件不满足才回 `_build_b2_not_wired_clone_provider` stub→PRESET。即**既有 smart 全量 auto-clone 能调真 MiniMax**，这是 pre-existing、用户经 smart_consent 显式 opt-in、CLAUDE.md「✅ 用户显式触发」合规的路径，**本任务未改动**。
>
> **已就位（"智能版扣 600 点克隆"实质已交付）**：
> - 克隆点数 **600**（P1，pricing_schema + 全 fallback）。
> - **用户显式触发的 600 点 MiniMax 克隆有两条既有路径**：① Studio / voice-selection「克隆音色」按钮 → `voice_selection_api` reserve/capture **600 点 + shadow credits 预扣/退还**；② smart 全量任务 auto-clone（smart_consent opt-in + smart_auto_clone_enabled）。两者都是 CLAUDE.md 合规的用户知情付费路径。
> - `smart_preview_clone_enabled` + daily/inflight cap admin 旋钮（P1，默认 OFF，**占位**）。
>
> **刻意延后（需专项 session）**：
> - smart **3 分钟预览 lane** 本身（登录 smart 的短预览 + 预览阶段主说话人 600 点预扣克隆 + 激活 + 预览转完整复用 voice_id）当前不存在——建它是独立 funnel 特性，不在本 clone-enablement 任务范围。
> - `smart_preview_clone_enabled` 旋钮当前**不 gate 任何运行时路径**，**不 gate 既有 smart_auto_clone**（要停既有 smart 克隆用 `smart_auto_clone_enabled`，不是本旋钮）。两者作用于不同流、不互相 AND。
> - 下列 §5 子项是该 smart-预览-lane 专项的设计目标（**本任务不落地**）。
>
> **⚠️ 运营提示（CodeX 强调，避免误判）**：本任务**未**关闭既有 smart 全量 auto-clone 的 MiniMax 克隆能力（`smart_auto_clone_enabled` 仍默认 True）。若项目主希望"在 smart 预览 lane 接好前，既有 smart 全量也不自动调 MiniMax"，需**单独**把 `smart_auto_clone_enabled` 置 False（本任务未改其默认，以免回归既有 smart 行为）。

> **⚠️ 已知功能缺口（CodeX 最终复核 P2，安全上是关闭态、不触达 MiniMax）：匿名/快捷前端尚未发送 `express_consent`。** `frontend-next/src/lib/api/anonymousPreview.ts::createPreview` body 只带 `anonymous_consent`，两个调用点（`anonymous-trial-panel.tsx`）也没传 express 克隆 opt-in。**后果**：即使项目主开 `anonymous_express_cosyvoice_clone_enabled`，匿名 express 因 consent 缺失仍 **fail-closed 回 CosyVoice 预设**（安全，但克隆链路功能上未闭合）。**后端 + 安全已完整**（create 已准备接收并注入 `express_consent`，pipeline L4 consent gate 已就位）；**剩最后一公里前端 opt-in UI**（在 express 卡片加"允许克隆我的音色"勾选 + availability gating + `createPreview` body 带 `express_consent={auto_voice_clone}`，镜像 PR3 登录态 express consent 的 jobs.ts/TranslationForm.tsx 模式）。因默认 OFF + fail-closed-to-preset，可安全在专项前端 session 接线（项目主开灰度前补即可）。

复用现有 credit ledger / shadow credits（gateway voice-clone 端点已有 shadow credits 机制）—— **以下为延后 smart-预览-lane 专项的设计目标**：

复用现有 credit ledger / shadow credits（gateway voice-clone 端点已有 shadow credits 机制）—— **以下为延后专项的设计目标**：

- provider call **前**预扣 600 点（`clone_credit_reserved` 状态，权威方案 §15.2）。
- 克隆成功 + 预览 TTS 激活成功 → 确认扣除，voice 存入个人音色库（`source=smart_preview`）。
- 克隆失败 / 激活失败 / 质量不达标 → 释放预扣 + 清理不可用 voice_id（`clone_failed_refunded`）。
- 点数 <600 → 不克隆，回预设/复用（`clone_skipped_insufficient_credits`）。
- 个人音色库满（Trial 10/Plus 30/Pro 100，跨 provider 合计）→ 拒绝新克隆（`voice_library_quota_full`）。
- 预览转完整任务复用同一 voice_id，不重复克隆/扣点（权威方案 §12.3/§13）。

> 智能版**确实**调 MiniMax 克隆——这是用户显式 consent + 点数预扣的知情付费路径，**符合** CLAUDE.md「✅ 用户显式触发」例外，**不违反**硬约束。

---

## 6. 测试矩阵（P6，TDD 先写测试）

> 准则：每道防线改写后，守卫测试**改为断言新边界**（不是删守卫）。新增"拦 MiniMax"正向 + "放行 CosyVoice"正向 + "失败回预设"三类断言。

### 6.1 防线守卫（断言新边界）

| 测试 | 断言 |
|---|---|
| 防线① 契约（P4 实现修正） | `raise_clone_provider_boundary` **仍恒抛**（守护契约模块不变 clone 执行器，不拆分）；adapter `mode="express"`+admin flag → `EXPRESS_TEMPORARY_CLONE_GATE`（契约信号，不被 create 消费）；consumed 字段 free vs express 一致（零回归）；真克隆只在 pipeline，MiniMax 绝不经匿名/快捷 |
| 防线② pipeline | 匿名 express+clone 开 → 允许 CosyVoice 克隆路由；匿名任意 mode 注入 minimax/voiceclone strategy → 仍强制回 preset |
| 防线③ payload | `express_consent` 在白名单；`voice_clone`/`voiceclone_reference_path`/`voice_a`/`voice_b`/`free_consent` 仍在禁列；gateway 模块仍不 import 任何克隆模块；rate-limit 模块仍不 import `src.services.voice_clone` |

### 6.2 行为测试

- 匿名 express + clone 开 + consent + worker 可用 → CosyVoice 克隆成功，temp voice `is_temporary=true`+TTL。
- 匿名 express + clone 开 + **worker 不可用 / reservation denied / 样本不足 / consent 缺失** → 回 CosyVoice 预设，**不抛、不换 provider**。
- 匿名克隆全局 cap 用尽 → 拒绝克隆回预设（fail-closed）。
- 🔥 **匿名/快捷任意失败路径绝不调 MiniMax**（mock MiniMax client，断言零调用）。
- 登录快捷版（已有 `express_cosyvoice_auto_clone_enabled`）路径回归不破。
- 智能版：点数≥600 预扣→克隆→激活→入库；点数<600 跳过；失败退点 + 清理 voice_id；库满拒绝；预览转完整复用同 voice_id 不重复扣。
- 克隆点数 600 在 pricing/前端/估算一致。

### 6.3 回归

- 本机预存失败基线用 set-diff 对照（[[feedback_test_database_stub_convention]]：本机约 335 例预存失败 + `test_process_pipeline.py` ~44 例 Fake mock 漂移非回归，[[project_cosyvoice_voice_match_fix]]）。只断言"本方案未新增失败"。

---

## 7. 待项目主裁定的开放问题

| # | 问题 | 状态 / 建议 |
|---|---|---|
| **Q1** | 防线② 用方案 A 还是 B？ | ✅ **已拍板 = 方案 A**（CodeX 确认代码已支持）。V1 降为 P2 回归测试 |
| **Q2** | 匿名克隆 reservation owner / cap 语义？ | ✅ **已定 = sentinel user 作全局 owner**，`anonymous_clone_active_cap` = 全局活跃上限（不迁移 reservation 表）。仍需 owner 确认全局 cap 语义可接受 |
| **Q3** | 登录快捷版（非匿名）是否同步接入前端 consent UI（PR3 待办），还是仅做匿名+智能？ | 待 owner 定。本任务聚焦匿名/快捷 CosyVoice + 智能 MiniMax；登录快捷 UI 若 PR3 未做则一并补 |
| **Q4** | 智能版个人音色库配额（Trial10/Plus30/Pro100 跨 provider）是否本任务落地，还是单列？ | 待 owner 定。建议本任务 P3 一并做（否则 600 点克隆无库容保护，权威方案 §12.3） |
| **Q5** | 克隆点数 500→600 的发布时机（非 default-OFF，影响现有计费）？ | 待 owner 定发布窗口；建议与克隆功能解耦、单独灰度（§4.2） |
| **Q6** | CLAUDE.md「付费 API」段澄清文案，是否需法务/项目主定稿？ | 待 owner 拍板措辞（CosyVoice 注册免费 + 克隆费已收取，不在硬约束射程） |

---

## 8. 分阶段执行（默认全 OFF 休眠上线）

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **P0** | 读方案 + 核实防线 + 本实施计划 | **本文档（交审）** |
| **P1** | admin 旋钮（§4.1，6 新增 + migration）+ consent 注入（§4.3）+ 克隆点数 600（§4.2），**默认 OFF** | 旋钮 + migration + 测试 |
| **P2** | **第一步写 V1 回归测试**（旁路链路）→ 改写防线②③（§3.1/§3.2，方案 A）+ 匿名接入 `maybe_run_express_auto_clone` 匿名分支（§3.4，CosyVoice only，sentinel 全局 owner） | 防线改写 + 守卫断言新边界 |
| **P3** | 智能版 600 点预扣 + 退还 + 库容门（§5） | shadow credits 接线 + 测试 |
| **P4** | 改写防线①（§3.3 契约 + 测试断言新边界） | 契约改写 |
| **P5** | 更新 CLAUDE.md 三道防线/付费 API 段 + 把"2026-06-12 anon-express"口径落到本文档（原 plan 文件不存在，仅代码引用） | 文档同步 |
| **P6** | pytest（TDD）全绿 + 冒烟 + set-diff 回归对照 | 测试报告 |

依赖：P0→P1→P2→{P3∥P4}→P5→P6。

---

## 9. 部署纪律（[[deploy_experience]] / [[feedback_apf_deploy_incident]] / [[feedback_compose_env_file_recreate]]）

- 所有新旋钮**默认 OFF**（休眠上线）；真钱/真克隆灰度**由项目主用 admin 旋钮开**，agent 不擅自开。
- `git archive HEAD`（不 tar 工作树）；**app src/ 是 bind-mount → 仅 `docker restart aivideotrans-app`**。
- **gateway 改动**（admin_settings / payload_spec / consent / api）需**重建镜像** + 确认 `PYTHONPATH=/opt/aivideotrans/app`。
- **ORM 加列必跑 migration**（哪怕 flag 关，否则 sentinel INSERT 漏 NOT NULL 列）。
- compose 改动落 **root 入口**（不只改 app/ 那份）。
- ⚠️ **prod 仅跑到 anon-express 线 + A'/B'，不含 main 的无关 +16**（[[project_cosyvoice_voice_match_fix]]）——部署本任务时按 **per-file/分阶段**，别把无关 +16 带上 prod。
- 部署前 `psql` 检查 in-flight pipeline（INFLIGHT=0 才动）。

---

## 10. git 协作

- 在**自己的 worktree + feature 分支**（建议 `claude/anon-clone-enable`）干活；本 P0 计划文档为未跟踪新文件，写在共享树 docs/plans/ 下、**不提交**，交项目主 review。
- 批准后 P1+ 代码改动进 worktree；提交只用**显式 pathspec**（`git commit -- <files>`，绝不 `git add .`）；不 push、不合 main；完成交项目主 review。
- 绝不在共享工作树做改状态的 git 操作（checkout/stash/reset/cherry-pick）。

---

## 11. 一页纸总结（给 review）

1. **真正阻断匿名克隆的只有两点**（非笼统三道防线）：consent 没注入 payload + `maybe_run_express_auto_clone` 需 user_id/allowlist。
2. **克隆只在 pipeline 发生**，gateway 从不 import 克隆模块——所以防线③的"gateway 无克隆 import"守卫**全保留**，只加 `express_consent` 一个白名单字段。
3. 防线② **已定方案 A**（保留 preset_mapping，克隆经 worker routing 旁路，CodeX 确认代码已支持）；V1 是 P2 第一条回归测试，非决策点。
4. 防线① 是契约 shell 不在运行时路径，改动风险最低。
5. **CosyVoice 免费克隆不违反硬约束；MiniMax 克隆只走智能版用户显式 consent + 600 点预扣的知情路径。** 任何失败路径零 MiniMax 自动调用，守卫测试强制断言。
6. 全部默认 OFF，赠点 800 已就位，点数 500→600。
