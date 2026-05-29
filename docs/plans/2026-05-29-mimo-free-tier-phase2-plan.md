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

- [ ] **Step 1（首个验收测试，CodeX 建议）：** 写失败测试 — `AVT_ENABLE_FREE_TIER=false` 时 `service_mode="free"` 创建被**拒绝**（404 / 明确错误）**且不静默降级 express**（修 `job_intercept.py:1042` 未知模式→express 的行为）。这是 Task 0 plumbing 接上的 fail-closed 消费方 gate。
- [ ] **Step 2:** 写失败测试：flag 开启时 `compute_job_policy(service_mode="free")` 返回 `wait_for_review=False`、`tts_provider="mimo"`、`tts_model="mimo-v2.5-tts-voiceclone"`、`voice_strategy="free_voiceclone"`、`voice_clone_enabled=False`、`credits=0`；加显式 mode 白名单，未知模式 → 拒绝（不再静默 express）。
- [ ] **Step 3:** 在 `compute_job_policy` 加 `free` 分支（与 smart/express 并列），gate 在 `AVT_ENABLE_FREE_TIER`（flag off → 拒绝，**不落 express**）；`intercept_create_job` 同步认 `free`。
- [ ] **Step 4:** 前端：`jobs.ts` 加 `"free"` service_mode 类型；`TranslationForm.tsx` 免费版入口（`NEXT_PUBLIC_ENABLE_FREE_TIER` gate）；任务列表能渲染 free 任务。
- [ ] **Step 5:** 测试通过 + Commit。

## Task 2（gate #4）：voiceclone 接进 TTS 管线（核心）

**Files:** `src/services/tts/tts_generator.py`、`DubbingSegment` 模型（加字段）、管线参考提取 + stamp 调用点（`src/pipeline/process.py` TTS 前）、tests

> Phase 1 已交付 `synthesize_voiceclone`(内联 ref + 10MB 校验，mimo_tts_provider.py:152) + `extract_speaker_references`(voiceclone_reference.py:45)。本任务**只做 wiring**，不重写原语。
>
> ⚠️ **CodeX review 核实的两条约束**：
> 1. **reference 经 per-segment 字段传递，不用全局 map / generator 临时状态**（并行 TTS 安全）。`generate_all`(tts_generator.py:243) 签名不变（segments/output_dir/job_record）；`_generate_one`(:1192) 已按 `segment.tts_provider`(:1214) 读 per-segment 字段——沿用此约定。
> 2. **分发不能只看 `provider == "mimo"`**：基础 MiMo TTS 已存在（`_generate_one_mimo`:487，dispatch 在 `_generate_one`:1279 的 `if provider == "mimo"`）。voiceclone 新分支必须**额外要求** `service_mode == "free"` / `voice_strategy == "free_voiceclone"`（或 `segment.voiceclone_reference_path` 非空），否则会劫持普通 MiMo 基础合成。

- [ ] **Step 1:** `DubbingSegment` 加字段 `voiceclone_reference_path: str | None = None`。
- [ ] **Step 2:** 管线 TTS **前**：调 `extract_speaker_references(segments, audio/speech_for_asr.wav, audio/voiceclone_ref/)` → `{speaker: path}`，持久化 job 产物，并把每段对应说话人的参考路径 **stamp 到 `segment.voiceclone_reference_path`**（design §2.2）。
- [ ] **Step 3:** 写失败测试（mock `synthesize_voiceclone`，不打真实 API）：`_generate_one` 的 `provider == "mimo"` 块内，当（`service_mode=="free"` 或 `voice_strategy=="free_voiceclone"`）**且** `segment.voiceclone_reference_path` 非空 → 走 `_generate_one_mimo_voiceclone(segment, ...)`（读 `segment.voiceclone_reference_path`）；否则走基础 `_generate_one_mimo`。缺参考 → 回落基础 `mimo-v2.5-tts` 预设（free），不报错、不调付费克隆。
- [ ] **Step 4:** 实现分支；产物照常进 DSP 对齐 + retiming（不破坏对齐不变量）。
- [ ] **Step 5:** mock 测试 + 回归 `test_mimo_voiceclone_provider.py` / `test_voiceclone_reference.py` 仍绿（确认未劫持非 free 的 mimo 基础合成）+ Commit。

## Task 3（gate #2）：用户侧 debit 真源 `(free, standard)=0`

**Files:** `pricing_runtime`(+ `pricing_schema` 如有)、`gateway/credits_service.py`、pricing/credits 测试

> ⚠️ **CodeX review 核实**：用户侧 debit 真源是 **`pricing_runtime` + `credits_service.DEBIT_RATES`**(credits_service.py:47)，**不是** `cost_management.py`（那是内部成本目录/毛利分析，属优化方案域，不应成为 debit 真源）。当前 `DEBIT_RATES` **无 `("free","standard")`** → 未知 `(mode,tier)` 落 `DEFAULT_DEBIT_RATE=10`(credits_service.py:55)，免费 job 会被错扣 10 点/分。该文件注释记载 `smart` 曾因此 silent 10× 少扣——同一坑。

- [ ] **Step 1:** 写失败测试：`free` 结算 = 0 点，来自 **pricing_runtime 真源 `(free, standard)=0`**；且**冻结 fallback `DEBIT_RATES` 也补 `("free","standard"): 0`**（pricing_runtime 缺失/损坏时不回落到 10）。不靠 policy 临时塞 `credits=0`，更非落 `DEFAULT_DEBIT_RATE`。
- [ ] **Step 2:** pricing_runtime（admin 真源）+ `DEBIT_RATES`（冻结 fallback）**双层**都加 `(free, standard)=0`。**不碰 `cost_management.py`**。
- [ ] **Step 3:** 终态走单一入口 `mirror_job_terminal_state`（结算 0 点，仍进 metering 让 admin 成本页可见免费版真实 ASR+LLM+TTS 成本，不漏记）。
- [ ] **Step 4:** 测试 + Commit。

## Task 4（gate #3）：免费版日配额（独立 ledger）

**Files:** `gateway/models.py` + `gateway/alembic/versions/*`(新 migration)、`gateway/job_intercept.py`(创建时 gate)、tests

- [ ] **Step 1:** 写失败测试：免费 job **创建时**校验"每用户每天 1 次"；今日已用 → 拒绝 + 升级提示；**不复用** `free_jobs_quota_total`（那是免费套餐总额）。
- [ ] **Step 2:** 新 PG 表/ledger `free_service_daily_usage`（user_id + 自然日 key，固定时区 Asia/Shanghai）+ Alembic migration。
- [ ] **Step 3:** 创建免费 job 成功时 +1；reset 按自然日。
- [ ] **Step 4:** 测试 + Commit。

## Task 5（gate #5）：下载 / stream / eager-push **三联** gate 显式 `free` 分支

**Files:** `src/services/r2_publisher_lib/downloadable_keys.py`、tests

> ⚠️ **CodeX review 核实**：`downloadable_keys.py` 有**三个**按 mode 分流的函数，未知/None mode **全部默认 Studio（全放行）**——只改 `download_keys_for` 会被 R2 预推 / stream 302 绕过：
> - `download_keys_for`:87（`/download/{key}` 权限）
> - `stream_kinds_for`:106（`/stream/{kind}` 权限，Studio 含 `audio`）
> - `eager_push_keys_for`:137（sweeper 主动推 R2 的 key 集，Studio 含 `editor.*`）
>
> free 必须在**三处都显式限制**，否则免费用户能 `/stream/audio` 或经 R2 预推拿到门控产物。

- [ ] **Step 1:** 写失败测试（三函数各一条 + bypass 断言）：`free` 在 `download_keys_for` / `stream_kinds_for` / `eager_push_keys_for` 都**只放水印成品**（`publish.dubbed_video` + poster；**不含** `audio` / 字幕 / 草稿 / 后编辑产物）；bypass 断言：free 的 eager-push 集 ∌ `editor.*`、stream 集 ∌ `audio`。
- [ ] **Step 2:** 新增 `FREE_ALLOWED_DOWNLOAD_KEYS` / `FREE_ALLOWED_STREAM_KINDS`（{video,poster}）/ `EAGER_PUSH_TO_R2_KEYS_FREE`（{dubbed_video, poster}），三函数各加显式 `if service_mode == "free"` 分支（语义≈express 受限版，但**显式不复用** express，便于 Phase 2b 付费解锁单独放开）。
- [ ] **Step 3:** 测试 + Commit。

## Task 6（gate #6）：fallback / kill-switch 可见性

**Files:** `gateway/job_intercept.py`(policy 读 admin flag)、`gateway/admin_settings.py`、TTS fallback 路径、tests

- [ ] **Step 1:** 写失败测试：(a) admin flag `free_tier_voiceclone_enabled`(默认 True) 关 → free `voice_strategy` 降级**最便宜预设引擎 CosyVoice**，免费版继续；(b) MiMo voiceclone 单段失败 → provider 重试 → 回落基础 `mimo-v2.5-tts` 预设（free），**对用户/admin 可见提示，不静默**；(c) **绝不**在失败路径自动调付费克隆(MiniMax)。
- [ ] **Step 2:** 实现 kill-switch 读取 + fallback 可见性（event/通知打点）。
- [ ] **Step 3:** **付费 API 守卫测试**（AST 扫，仿 `test_phase1_guards`）：free 路径无自动 MiniMax 克隆。
- [ ] **Step 4:** 测试 + Commit。

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
