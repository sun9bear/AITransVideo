# MiMo 免费版 Phase 2a（内部可跑通版）Implementation Plan

> **For agentic workers (Claude Code env):** 推荐用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐 task 驱动；若执行环境无该 skill（如其它 agent/CLI），直接按 checkbox (`- [ ]`) 顺序执行即可——**非硬依赖**。

**日期**：2026-05-29
**状态**：`DRAFT` —— Phase 1 已通过验收（见 [design §1.5](2026-05-29-mimo-free-tier-design.md)）。本计划把"免费版流程跑通"落成可执行任务。
**范围**：本计划 = **Phase 2a（内部可跑通版，behind flag，不对公众开放）**；"公开免费版"= Phase 2b 变现 + Launch（consent/法务 sign-off 后），**不在本计划开工范围**。（2026-05-29 CodeX review 后修正标题/范围，避免执行者误判上线范围。）
**前置**：[免费版 design spec](2026-05-29-mimo-free-tier-design.md)（§1.5 的 6 个落地 gate 是本计划的真源）、[Phase 1 plan](2026-05-29-mimo-free-tier-phase1-plan.md)（provider + 参考提取 + harness 已交付）。

**Goal:** 新增 `service_mode="free"`，复用 Express 非交互管线，把"原始说话人干净参考 + 中文译文 → MiMo voiceclone 保留原声中文配音"接成一条**完整可跑的免费版流程**，全程 **behind feature flag、不对公众开放**。Phase 1 已交付 `synthesize_voiceclone` + `extract_speaker_references` 两个原语，本计划把它们**接进 pipeline + 补齐 6 个落地 gate**。

**Architecture:** 复用 Express 编排（ASR → S2 三轮审校 → 翻译 → TTS → DSP 对齐 → 字幕 retiming → 发布/剪映草稿），唯一管线差异在 **TTS 音色环节**（voiceclone 分支）+ **发布阶段水印**。所有用户可见入口由双端 feature flag gate；6 个 gate 守住"未知模式静默降级 / 计费 / 配额 / provider / 下载 / fallback 可见性"。

**Tech Stack:** Python 3.11、FastAPI(gateway)、SQLAlchemy + Alembic(PG migration)、pytest（mock，不打真实付费 API）、Next.js/TS(前端类型 + flag gate)、ffmpeg(水印 drawtext)。

---

## ⚠️ LAUNCH GATE（非工程，先行声明）

**Phase 2 工程管线可以现在就建好（behind flag、不对公众开放），但"对真实用户开闸"必须先过 consent/法务**（design §5.3）：免费版克隆的是**视频里第三方说话人**的声音，《民法典》1023 条把声音权参照肖像权保护——勾 ToS ≠ 被克隆人本人授权。**这是产品/法务拍板的上线 gate，工程不能单方决定。** 本计划所有任务默认 `AVT_ENABLE_FREE_TIER=false`，CI/生产都不放公众入口；法务 sign-off + consent UI（Phase 2b）齐了再开 flag。

---

## 切片（gate #7）= 本期不做

Phase 1 复测确认 MiMo voiceclone 长输入 run-to-run 不稳定（同输入时长方差 ~19%），切短是**已验证的后备杠杆**但**本期不落地**（design §1.5 gate #7 注）：复用的 Express 对齐层（`utils/audio_fit.fit_audio_to_slot`：atempo + 补静音 + >20% 才 rewrite）本就吸收 TTS 时长方差，免费版可接受质量波动。**本计划不写任何切分/重拼代码。**

---

## 双端 Feature Flag（贯穿所有任务）

沿用 `AVT_ENABLE_POST_EDIT` 双 gate 模式（docker-compose.yml:127/297 + .env.example:109-110 + gateway 层 short-circuit + 回归守卫 `tests/test_phase1_guards.py`）：

| 端 | env | 默认 | 作用 |
|---|---|---|---|
| 后端 | `AVT_ENABLE_FREE_TIER` | `false` | gateway 拦 `service_mode="free"` 的创建；off → 404/拒绝 |
| 前端 | `NEXT_PUBLIC_ENABLE_FREE_TIER` | `0` | 才渲染免费版入口 |

---

## 锚点（核实于 2026-05-29，实现时再确认当前行号）

| 区域 | 文件:符号 |
|---|---|
| 模式分发 / provider 白名单 | `gateway/job_intercept.py`：`_VALID_EXPRESS_PROVIDERS`:348、`_VALID_STUDIO_PROVIDERS`:349、policy 分支 ~447/475、`intercept_create_job` |
| 计费 fallback | `gateway/credits_service.py`：`DEFAULT_DEBIT_RATE=10`:55 |
| 价格目录 | `gateway/cost_management.py`：`DEFAULT_PRICE_CATALOG` |
| 配额（套餐总额，非按日） | `gateway/models.py`：`free_jobs_quota_total`:45；`gateway/entitlements.py`:117/154 |
| MiMo 原语（Phase 1 已交付） | `src/services/tts/mimo_tts_provider.py`：`synthesize_voiceclone`:152；`src/services/tts/voiceclone_reference.py`：`extract_speaker_references`:45 |
| TTS 分发 | `src/services/tts/tts_generator.py`：`_generate_one` 分发（~1192） |
| 下载 gate | `src/services/r2_publisher_lib/downloadable_keys.py`：`download_keys_for` |

---

# Phase 2a — 核心流程跑通（behind flag）

## Task 0：双端 feature flag 脚手架

**Files:** `docker-compose.yml`、`.env.example`、`gateway/config.py`(Settings 字段)、`tests/test_phase2_free_tier_guards.py`(Create)

> ✅ **已完成**（commit `536da7f`，2026-05-29）。Task 0 = **纯 flag plumbing**；`service_mode="free"` 的 **fail-closed 拒绝 gate 移到 Task 1 Step 1**（消费方在 `job_intercept`，与 free 分支一起落）——本任务不含该 gate。

- [x] **Step 1:** `AVT_ENABLE_FREE_TIER: "${AVT_ENABLE_FREE_TIER:-false}"`（**gateway env**，与 `AVT_ENABLE_POST_EDIT` 同块——flag 仅 gateway 读，非 app）+ `NEXT_PUBLIC_ENABLE_FREE_TIER: "${NEXT_PUBLIC_ENABLE_FREE_TIER:-0}"`（frontend build-arg）；`gateway/config.py` 加 `enable_free_tier` 字段（`env_prefix="AVT_"` 自动绑定）；.env.example 两行默认 off + LAUNCH GATE 注释。
- [x] **Step 2:** plumbing 测试（`tests/test_phase2_free_tier_guards.py`）：`enable_free_tier` 默认 False + 读 `AVT_ENABLE_FREE_TIER` env。（**fail-closed 拒绝 gate 不在此**，见 Task 1 Step 1。）
- [x] **Step 3:** Commit（`536da7f`）。

## Task 1（gate #1）：`service_mode="free"` 模式分发 + 白名单

**Files:** `gateway/job_intercept.py`、`frontend-next/src/components/workspace/TranslationForm.tsx`、`frontend-next/src/types/jobs.ts`、tests

> ✅ **Task 1 完成**：backend `df04b4a`（Step 1–3，gate + free policy）+ frontend `4014c42`（Step 4–5；`tsc --noEmit` + eslint 0 errors）。
> **CodeX 复审补丁**：(P1) `entitlements.get_effective_allowed_service_modes` 在 `AVT_ENABLE_FREE_TIER` 开启时把 `free` 纳入 allowed-modes——修 handler `:1286` 会把普通用户 free 挡成 `service_mode_not_allowed` 的漏洞（helper + handler flag-on 测试已补）；(P2) 前端 free 价格说明改为「不扣点 + 水印 + add-on 另计」，不再误落 express/studio 费率分支。
> **未知模式行为（2026-05-29 CodeX review 决议）**：只有 `free` 被识别 + flag-gated；**其它未知模式保留 legacy express fallback**（与 PR#3C-b3g smart 白名单先例一致——刻意保留，避免破坏 missing/typo `service_mode` 现有客户端的优雅降级）。**"reject 所有未知模式" 不做**——仅 `free` 需 fail-closed，因它是独立计费/产物路径。

- [x] **Step 1（gate 测试，含 handler 层）：** (a) 纯 helper `_gate_service_mode`：flag off + `free` → 403 `free_disabled`（不降级 express）；unknown → express；known 透传。(b) **handler 层** `intercept_create_job`（`service_mode=free` + flag off）→ 403 `free_disabled` **且 `proxy_request` 未被调用**（真实安全边界，CodeX P3）。
- [x] **Step 2（policy 测试）：** flag 开启时 `compute_job_policy(user, "free")` 返回 `service_mode="free"`、`tts_provider="mimo"`、`tts_model="mimo-v2.5-tts-voiceclone"`、`voice_strategy="free_voiceclone"`、`voice_clone_enabled=False`、`requires_review=False`、`quality_tier="standard"`。**credits=0 不在 policy dict**——debit 真源是 pricing_runtime/DEBIT_RATES（Task 3），与 express/studio/smart 一致（CodeX P2）。
- [x] **Step 3:** `_gate_service_mode` helper + `intercept_create_job` 用它（替换 inline 白名单）+ `compute_job_policy` free 分支。回归：free guards + gateway_job_policy + smart_kill_switch + phase1_guards + create_job 全绿。
- [x] **Step 4:** 前端：`service_mode` 联合类型加 `'free'` **6 处一致**（`types/jobs.ts` ×2、`types/api.ts`、`TranslationForm` state、`result-download-list`、`ResultMediaCard`；后两者把 `free` 并入 express 的受限输出分支，避免 free 完成任务渲染 Studio 全量下载）；`TranslationForm.tsx` 免费版 plan card（`NEXT_PUBLIC_ENABLE_FREE_TIER` gate，默认不渲染）。
- [x] **Step 5:** `tsc --noEmit` 0 errors（联合类型一致性已验证）+ eslint 0 errors（34 warnings 均为既有、与本改动无关）+ Commit。

## Task 2（gate #4）：voiceclone 接进 TTS 管线（核心）

**Files:** `src/services/tts/tts_generator.py`、`DubbingSegment` 模型（加字段）、管线参考提取 + stamp 调用点（`src/pipeline/process.py` TTS 前）、tests

> Phase 1 已交付 `synthesize_voiceclone`(内联 ref + 10MB 校验，mimo_tts_provider.py:152) + `extract_speaker_references`(voiceclone_reference.py:45)。本任务**只做 wiring**，不重写原语。
>
> ⚠️ **CodeX review 核实的两条约束**：
> 1. **reference 经 per-segment 字段传递，不用全局 map / generator 临时状态**（并行 TTS 安全）。`generate_all`(tts_generator.py:243) 签名不变（segments/output_dir/job_record）；`_generate_one`(:1192) 已按 `segment.tts_provider`(:1214) 读 per-segment 字段——沿用此约定。
> 2. **分发不能只看 `provider == "mimo"`**：基础 MiMo TTS 已存在（`_generate_one_mimo`:487，dispatch 在 `_generate_one`:1279 的 `if provider == "mimo"`）。voiceclone 新分支必须**额外要求** `service_mode == "free"` / `voice_strategy == "free_voiceclone"`（或 `segment.voiceclone_reference_path` 非空），否则会劫持普通 MiMo 基础合成。

> ✅ **Task 2 完成**（Chunk A `1dfb11e` + 补严 `f21fd5e`；Chunk B 本批提交）。最终 dispatch 条件：`provider=="mimo"` **且** `_voice_strategy=="free_voiceclone"`（用 `set_voice_strategy` 注入器，不读 job_record 内部）**且** `segment.voiceclone_reference_path` 非空 → voiceclone；否则 base MiMo 预设（含缺参考回落）。

- [x] **Step 1:** `DubbingSegment.voiceclone_reference_path: str | None = None`（`1dfb11e`）。
- [x] **Step 2:** `process.py` S4 stage（run() 内，`generate_all` 前）调 `voiceclone_reference.stamp_segment_references(segments, audio/speech_for_asr.wav, audio/voiceclone_ref/)` + `tts_generator.set_voice_strategy(job_voice_strategy)`；best-effort（失败→未 stamp→base 预设，不阻塞管线）（本批）。
- [x] **Step 3:** `_generate_one` 三分支 mock 测试：free+ref→voiceclone / 非free+ref→base / free+无ref→base（`f21fd5e`）。
- [x] **Step 4:** `_generate_one_mimo_voiceclone` + reference-gated dispatch + `set_voice_strategy` 注入器（`1dfb11e` + `f21fd5e`）。
- [x] **Step 5:** 回归全绿（wiring + mimo provider + voiceclone_reference + free guards）；`process.py` AST 校验通过；stamp 逻辑单测在 `test_voiceclone_reference.py`（本批）。

## Task 3（gate #2）：用户侧 debit 真源 `(free, standard)=0`

**Files:** `pricing_runtime`(+ `pricing_schema` 如有)、`gateway/credits_service.py`、pricing/credits 测试

> ⚠️ **CodeX review 核实**：用户侧 debit 真源是 **`pricing_runtime` + `credits_service.DEBIT_RATES`**(credits_service.py:47)，**不是** `cost_management.py`（那是内部成本目录/毛利分析，属优化方案域，不应成为 debit 真源）。当前 `DEBIT_RATES` **无 `("free","standard")`** → 未知 `(mode,tier)` 落 `DEFAULT_DEBIT_RATE=10`(credits_service.py:55)，免费 job 会被错扣 10 点/分。该文件注释记载 `smart` 曾因此 silent 10× 少扣——同一坑。

> ✅ **Task 3 完成**（本批提交）。关键修复：`estimate_credits` 的 `max(1, round(...))` 最低-1-点地板会把 rate=0 变成 1 点——加 `if rate <= 0: return 0`，让真正免费（rate 0）的模式真的 0 点（付费模式仍保留地板）。`estimate_credits` 是**唯一** debit 计算点（reserve / terminal settle / shadow 都调它），一处修复覆盖预扣 + 结算 + shadow。**【CodeX P1 复审补】** `_get_runtime_debit_rates` 改为 **frozen 基底 + runtime overlay**：旧 `pricing_runtime.json` 缺 `free.standard` 时落 frozen 0（而非 `DEFAULT_DEBIT_RATE=10` → 错扣 100/10min），同时也加固了 `smart.standard` 同类风险；补 stale-runtime 回归测试。

- [x] **Step 1:** 三个测试通过：`DEBIT_RATES[("free","standard")]==0`（冻结）、`get_runtime_pricing().credits.debit_rates["free.standard"]==0`（runtime）、`estimate_credits(10,"free","standard")==0`（解析后，绕过地板）。
- [x] **Step 2:** 双层都加 `(free, standard)=0`：`pricing_schema` 默认 `debit_rates["free.standard"]=0`（runtime 真源）+ `credits_service.DEBIT_RATES[("free","standard")]=0`（冻结 fallback）。**未碰 `cost_management.py`**。
- [x] **Step 3:** 终态结算用同一 `estimate_credits`（rate 0 → 0 点）；`mirror_job_terminal_state` 未改（经 estimate_credits 算得 0）。仍进 metering（成本页可见免费版真实成本）。
- [x] **Step 4:** 测试 + Commit；**并补上之前 defer 的 handler flag-on forward/override snapshot 断言**——free=0 不再 402，handler 走到 `proxy_request`，转发 body 带 `service_mode=free` / `tts_provider=mimo` / `voice_strategy=free_voiceclone`（102 passed 回归）。

## Task 4（gate #3）：免费版日配额（独立 ledger）

**Files:** `gateway/models.py`（新表）+ `gateway/alembic/versions/034_*`（migration，链 `033`）、`gateway/free_service_quota.py`（day-key + reserve/consume/release service）、`gateway/job_intercept.py`（free 分支 gate + 跳过 legacy `reserve_quota`）、tests

> ⚠️ **CodeX plan review 修正（3 点，落地前改 — 否则"看似有 gate、实则双扣/并发洞"）**：
> 1. **不与 legacy quota 双扣**：现 create 流程 upstream 接受后无条件 `reserve_quota(db, user.id, job)`（job_intercept.py:1557 → `quota.py:70` 递增 `users.free_jobs_quota_used`）。`service_mode=="free"` 必须**绕开 legacy `reserve_quota`**——只走新 daily ledger，不碰 `free_jobs_quota_*`（那是免费*套餐*总额，非按日）。
> 2. **原子 admission（防并发穿透）**：现流程先 forward upstream（:1499）再本地记录（:1506）；纯"check 后 record"会让两个并发 free 请求都过 check 并都被 upstream 接受。改成**单事务原子预约**：锁 `users` row（`SELECT … FOR UPDATE`，与 `ExpressCloneReservation` reserve service 同模式）或 PG `INSERT … ON CONFLICT … DO UPDATE … WHERE active_count < cap RETURNING`；预约失败直接 403，且**在 upstream forward 之前**；upstream 失败要 release（补偿）。
> 3. **per-job ledger，非 aggregate counter**：带 `create_idempotency_key`/`job_id` + `status(reserved/consumed/released)`，daily cap = 该 `(user, SH-day)` 的 **active(reserved|consumed) 行数 < cap**；支持幂等、失败释放、审计（镜像 `ExpressCloneReservation`）。

> ✅ **Task 4 完成**（Steps 1–3 `0310fc0`；Steps 4–5 本批提交）。reserve 在 upstream forward **前**原子预约（锁 users row）；2xx → consume / 非 2xx → release；TTL + inline-expire 兜底 forward 异常/本地失败的漏 release。`service_mode=="free"` 跳过 legacy `reserve_quota`（`reserved = True if free else await reserve_quota(...)`）。`153 passed` 回归（含 create-flow 非 free 不变）。
>
> ✅ **CodeX post-impl fix（P1+P2，本批）**：idempotency 全部按 `(user_id, create_idempotency_key)` 隔离（`_find_active_by_key` lookup + migration 唯一索引），跨用户同 client key 不再串到别人 row、不再绕过自己 cap。active idempotency = `reserved|consumed`：consumed 后同 key 重试是**幂等命中**而非 `daily_cap_exceeded` 403（网络超时重试安全）。consume/release 带 `user_id`。+2 测试（cross-user 隔离 / consumed-retry），`96 passed`。

- [x] **Step 1（纯函数）:** `shanghai_day_key`（ZoneInfo + UTC+8 fallback）+ 测试（`0310fc0`）。
- [x] **Step 2（model + migration）:** `FreeServiceDailyUsage`（per-job ledger + status 机 + partial-unique idempotency 索引）+ migration `034` 链 `033` + migration-chain/model-columns 测试（`0310fc0`）。
- [x] **Step 3（reserve service）:** `free_service_quota.reserve/consume/release`（锁 users row、inline-expire stale、idempotency by `create_idempotency_key`、cap = active(reserved|consumed) 行 for `(user, SH-day)`）+ aiosqlite 状态机/幂等/cap/next-day/inline-expire 测试（`0310fc0`）。PG-only FOR UPDATE 真并发留真 PG 测试。
- [x] **Step 4（wiring）:** `intercept_create_job` free 分支 — forward 前 reserve（拒绝 → 403 `free_daily_quota_exceeded`、不 forward）；2xx → consume / 非 2xx → release；free 跳过 legacy `reserve_quota`。handler 测试：snapshot forward / over-cap 403 不 forward / free 不调 `reserve_quota`（本批）。
- [x] **Step 5:** 回归 `153 passed, 1 skipped` + Commit。

## Task 5（gate #5）：下载 / stream / eager-push **三联** gate 显式 `free` 分支

**Files:** `src/services/r2_publisher_lib/downloadable_keys.py`、tests

> ⚠️ **CodeX review 核实**：`downloadable_keys.py` 有**三个**按 mode 分流的函数，未知/None mode **全部默认 Studio（全放行）**——只改 `download_keys_for` 会被 R2 预推 / stream 302 绕过：
> - `download_keys_for`:87（`/download/{key}` 权限）
> - `stream_kinds_for`:106（`/stream/{kind}` 权限，Studio 含 `audio`）
> - `eager_push_keys_for`:137（sweeper 主动推 R2 的 key 集，Studio 含 `editor.*`）
>
> free 必须在**三处都显式限制**，否则免费用户能 `/stream/audio` 或经 R2 预推拿到门控产物。

> ✅ **Task 5 完成**（本批提交）。三函数（`download_keys_for` / `stream_kinds_for` / `eager_push_keys_for`）各加显式 `if service_mode == "free"` 分支 → 新 `FREE_ALLOWED_DOWNLOAD_KEYS`={`publish.dubbed_video`} / `FREE_ALLOWED_STREAM_KINDS`={video,poster} / `EAGER_PUSH_TO_R2_KEYS_FREE`={dubbed_video,poster}，与 express 等值但**显式独立**（Phase 2b 可单独解锁）。free 不再 fall-through 到 Studio。`37 passed`（含 `test_cleanup_r2_parity` / `test_r2_publisher` 回归不变）。

- [x] **Step 1:** 写失败测试（三函数各一条 + bypass 断言）：`free` 在 `download_keys_for` / `stream_kinds_for` / `eager_push_keys_for` 都**只放水印成品**（`publish.dubbed_video` + poster；**不含** `audio` / 字幕 / 草稿 / 后编辑产物）；bypass 断言：free 的 eager-push 集 ∌ `editor.*`、stream 集 ∌ `audio`。RED 确认（free≡studio 全放行）。
- [x] **Step 2:** 新增 `FREE_ALLOWED_DOWNLOAD_KEYS` / `FREE_ALLOWED_STREAM_KINDS`（{video,poster}）/ `EAGER_PUSH_TO_R2_KEYS_FREE`（{dubbed_video, poster}），三函数各加显式 `if service_mode == "free"` 分支（语义≈express 受限版，但**显式不复用** express，便于 Phase 2b 付费解锁单独放开）。
- [x] **Step 3:** 测试 + Commit。

## Task 6（gate #6）：fallback / kill-switch 可见性

**Files:** `gateway/job_intercept.py`(policy 读 admin flag)、`gateway/admin_settings.py`、TTS fallback 路径、tests

> ✅ **Task 6 完成**（本批提交）。三部分全落地：
> - **(a) kill-switch**：新增 `AdminSettings.free_tier_voiceclone_enabled`(plain bool，默认 True)。`compute_job_policy` free 分支读它 → 关则降级 CosyVoice `preset_mapping`(`tts_provider=cosyvoice` / `tts_model=cosyvoice-v3-flash` / `voice_clone_enabled=False`)，仍 `service_mode=free`、credits 仍 0、免费版继续，绝不触付费 clone。
> - **(b) 可见回落**：`_generate_one` 加 `force_mimo_preset` 参数；`_generate_one_with_backoff` 在 free voiceclone 重试耗尽后回落**基础 MiMo 预设**(`force_mimo_preset=True`，同 free provider)，`result.fallback_used_provider="mimo_preset"`(→ segment manifest 可见) + `logger.warning` + console，绝不静默、绝不切付费 provider。
> - **(c) 守卫**：`mimo_tts_provider` 无付费 clone/TTS import(AST)、`get_fallback_provider("mimo")` 恒 None(行为)、fallback 分支 AST 锁 `provider="mimo"` + `force_mimo_preset=True`。
> - **CodeX P1 fix**：`force_mimo_preset=True` 在 `_generate_one` 内**硬置 `provider="mimo"`**，优先级高于 `segment.tts_provider` 与 `requires_worker` —— 防 segment 上 `tts_provider="minimax"` 漂移把 free 回落带进付费 MiniMax 分支（原 AST guard 只看调用参数、看不到内部覆盖）。补行为测试：drifted `tts_provider="minimax"` + `force_mimo_preset=True` → 仍只走 MiMo base。
>
> Task 6 测试全绿（含 CodeX P1 回归）。

- [x] **Step 1:** 失败测试三部分：(a) kill-switch 降级 CosyVoice preset；(b) voiceclone 失败可见回落基础 MiMo 预设；(c) 守卫 free 路径无付费 clone。
- [x] **Step 2:** kill-switch（policy 读 admin flag）+ fallback 可见性（`fallback_used_provider="mimo_preset"` + log/console）。
- [x] **Step 3:** 付费 API 守卫（`mimo_tts_provider` import 扫 + `get_fallback_provider("mimo")`=None + fallback AST 锁 `provider="mimo"`）。
- [x] **Step 4:** 测试 + Commit。

## Task 7（§4.1）：10 分钟时长卡口（fail-closed）

**Files:** job 入口探测（ffprobe）+ `gateway/job_intercept.py`、tests

- [ ] **Step 1:** 写失败测试：`mode=free` 且 ffprobe 时长 > 10min → 进昂贵阶段(ASR/LLM/TTS)**之前**拒绝 + 升级提示（上传/URL 两路都先探测再花钱）。
- [ ] **Step 2:** **fail-closed**（CodeX review）：`mode=free` 若**拿不到可信时长**（YouTube/local 探测失败 / 无 duration）→ **拒绝 / 要求重传**，不得继续进 ASR/LLM/TTS（否则成本闸门不闭合）。付费模式维持现状不变。
- [ ] **Step 3:** 实现卡口 + 测试 + Commit。

## Task 8（§4.2）：免费版水印（最小实现）

**Files:** 发布阶段 publish 代码、tests

- [ ] **Step 1:** 写失败测试：`mode=free` 的 `publish.dubbed_video` 经 ffmpeg `drawtext` 叠加水印；付费版不加。
- [ ] **Step 2:** 实现 drawtext 叠加（文字/位置先用配置默认值；admin 可配 UI 留 Phase 2b）。
- [ ] **Step 3:** 测试 + Commit。

## Phase 2a 验收（手动，behind flag）

- [ ] 部署到美国主机，`AVT_ENABLE_FREE_TIER=true`(仅内部)；选一个真实视频跑完整 free 流程。
- [ ] 验证：未知模式不再静默 express；free 任务 credits=0 且进 metering；日配额拦第 2 次；下载只给水印成品；MiMo 失败可见回落预设；10min 卡口生效；voiceclone 保留原声产物正常对齐。
- [ ] flag 关闭后公众入口完全不可见（前后端双 gate）。

---

# Phase 2b — 变现 + 打磨（outline，跑通后再做）

> 不在"跑通流程"范围内，单列后续 task：

- **付费解锁 add-on**（design §4.3）：后编辑(按时长 10 点/分、底价 20) + 剪映草稿(flat 50)；全部费率进 `pricing_runtime` admin 可配。后编辑 re-TTS 计费在 MiMo 免费期"进入即解锁"，转收费后"按段计"。
- **水印 admin 配置 UI**（§4.2）：文字/位置/字号/透明度存 `admin_settings`。
- **免费版 LLM 模型默认**（§1b/§3）：`_MODE_DEFAULTS["free"]` 倾向低成本模型；admin prompts 页加"免费版" tab。
- **consent UI + ToS 文案**（§5.3，LAUNCH GATE 配套）：上传前勾选"拥有内容使用权/知悉语音合成"。

---

## 明确不做（Phase 2 边界）

- **不做切片/重拼**（gate #7 deferred，见 design §1.5）——现有对齐吸收方差即可。
- 不碰 Smart MiniMax 主链路 / clone / quota / UserVoice mirror。
- **free mode 本身就是自动（非交互）管线**——约束的准确表述是：voiceclone **不进入 Express/Studio/Smart 的静默 fallback / 兜底路径**；**仅在用户显式选 `free` mode 且 `AVT_ENABLE_FREE_TIER` 开启时触发**（知情的 TTS 步骤；失败回落免费预设；credits=0；绝不自动调付费 MiniMax 克隆）。
- 不在 flag off / 法务未 sign-off 前对公众开放任何入口。
- 不把 MiMo 限免 TTS 当长期商业定价依据。

## 测试清单（汇总，对齐 design §5.6）

- `compute_job_policy` free 分支策略正确 + 未知模式不静默 express + flag off 拒绝
- 10min 卡口 / 日配额 gate / credits=0 结算 + metering 不漏
- voiceclone 内联参考调用（mock urlopen）+ 缺参考回落预设
- 水印：免费加、付费不加
- kill-switch 降级 CosyVoice + MiMo 失败可见回落
- **付费 API 守卫**（AST 扫）：free 路径无自动 MiniMax 克隆
- 下载 gate：free 只放水印成品
