# APF2c backend adapter 边界设计（design only，无 runtime wiring）

**状态：** DESIGN ONLY
**日期：** 2026-06-02
**任务：** APF2c-backend-adapter-design-contract-scaffold
**关联：**

- 漏斗 UX 方案：`docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md`
- APF2 匿名 intake 契约（APF2a 设计 + APF2b runtime contract）：`docs/plans/2026-06-02-apf2-anonymous-intake-contract.md`
- APF2b runtime contract 模块：`src/services/anonymous_preview_intake.py`
- APF2b contract 测试：`tests/test_anonymous_preview_intake_service.py`
- APF2a contract scaffold：`tests/test_apf2_anonymous_intake_contract.py`
- Human 三角决策记录：`docs/ai-workgroup/inbox/CodeX/2026-06-02T195902_from-Human_to-CodeX_type-instruction_task-AUTO-next-phase-triage-human-decision.md`
- 本任务派发：`docs/ai-workgroup/working/Claude-Code/2026-06-02T200120_from-CodeX_to-Claude-Code_type-instruction_task-APF2c-backend-adapter-design-contract-scaffold.md`

> **不是实施授权**。本文只描述 APF2c adapter 与 `src/services/anonymous_preview_intake.py` 之间未来的输入/输出边界、fail-closed 约束、绝对禁用清单与测试 scaffold 范围。任何 `src/`、`gateway/`、`frontend-next/`、`public/`、`migrations/`、`.env`、`docker-compose.yml`、`Dockerfile*` 改动仍在 Human Gate 之内，本任务**不解锁**。

---

## 1. APF2c adapter 的定位与范围

APF2c adapter 是**未来** backend / upload handler 与 `src/services/anonymous_preview_intake.py`（APF2b 落地的纯 contract 模块）之间的边界层：

- **不是 endpoint 实现**：adapter 不挂任何 FastAPI / Gateway 路由、不绑端口、不暴露 HTTP / Job API；
- **不是 upload handler**：adapter 不接收 multipart body、不直接落盘、不操作 R2 / object store；
- **不是 probe / compliance / clone provider**：adapter 不调用 ffprobe、ASR、LLM、TTS、CosyVoice / MiniMax 等 provider；
- **不是 counter store / DB**：adapter 不连接 PostgreSQL、Redis、Gateway DB；
- **不持有付费 API 凭据**：adapter 不读 `.env`、不持密钥。

APF2c adapter 的**唯一职责**是把"未来真实 backend 子系统已经持有的事实（facts）"翻译成 `anonymous_preview_intake` 模块要求的纯值，逐条调用其 helper，把 `IntakeRejected` 转译成 status-only `PreviewRecord`。一切真实 I/O、provider、外部服务、运行态状态都是 adapter 的**输入**，由真实 backend 在 adapter 之外提供。

APF2c adapter 仍属于 design-time 概念：本任务不允许产生任何 adapter 的 runtime 代码——只允许把这套边界写到本设计文档与 `tests/test_apf2c_backend_adapter_contract.py` 的 fake adapter 中。

---

## 2. Adapter 输入/输出 contract

`anonymous_preview_intake` 暴露的 helper 是**纯值进、纯值出**：

| pure helper | 入参（纯值） | 出参 |
|---|---|---|
| `require_config(config)` | `IntakeConfig` 或 `None` | `IntakeConfig` 或 `raise IntakeRejected` |
| `admit_source(config, *, source_type, is_free_user)` | `IntakeConfig` + `SourceType` + `bool` | `None`，YouTube/匿名/Free → `raise IntakeRejected` |
| `admit_upload(config, upload)` | `IntakeConfig` + `UploadIntake` | `None`，违反 allow-list/size/duration/chunked/temp-storage → `raise IntakeRejected` |
| `evaluate_probe_result(probe_result)` | `ProbeResult` | `ProbeResult`，`failure_reason` 非空 → `raise IntakeRejected` |
| `evaluate_compliance_result(result)` | `ComplianceResult` | `ComplianceResult`，BLOCK / NEEDS_MANUAL_REVIEW / blocked_media_retained → `raise IntakeRejected` |
| `fail_closed_from_exception(stage, exc)` | `str` + `BaseException` | `IntakeRejected(status=FAILED)` |
| `build_anonymous_session(config, *, session_id_hash, ip_hash, device_cookie_hash, now)` | hash 值 + `datetime` | `AnonymousSession` |
| `build_preview_record(config, *, session, upload, probe_result, compliance_result, source_type, now)` | 已经过 gate 的纯值 | `PreviewRecord(status=READY_FOR_MODE)` |

APF2c adapter 的边界翻译规则如下。

### 2.1 facts → `IntakeConfig`

未来 backend 子系统应有一个 config 加载层（`gateway/config.py` Settings、ENV、admin runtime 表等）。adapter 接收**已加载好**的配置事实，而不是去读 `.env`：

- `session_ttl_seconds` ← admin 配置或常量 `DEFAULT_SESSION_TTL_SECONDS`；
- `allowed_upload_types` ← 配置（默认 `("mp4", "mov", "m4v", "webm")`）；
- `max_upload_bytes` ← 配置（默认 500 MB）；
- `max_source_duration_seconds` ← 配置（默认 30 分钟）；
- `single_request_upload_only` ← 配置（默认 True）；
- `temp_upload_dir` ← Gateway / app 容器实际挂载的临时目录路径（由 backend 持有，adapter 接收为纯 `Path`）；
- `temp_storage_available` ← **由 adapter 外部的 storage health probe 提前判定**后传入。adapter 不自己 `Path.exists()`、不 `os.access()`。pure 模块要求 `temp_storage_available=True` 才会放行 upload；
- `temp_upload_ttl_seconds`、`preview_record_ttl_seconds`、`compliance_audit_retention_seconds` ← 配置；
- 四个 rate-limit cap ← 配置（默认 500 / 3 / 2 / 1）；
- `youtube_enabled_for_anonymous`、`youtube_enabled_for_free` ← 在 APF2 中**必须**为 `False`（C19）；
- `escalate_to_login_after_rate_limit` ← 默认 `True`（C23）。

如果配置层任何字段缺失、类型非法、env 没被注入，adapter 必须**在调 pure helper 之前** raise `IntakeRejected(PreviewStatus.FAILED, "<原因>")`，并把响应翻译成 status-only preview response。

### 2.2 future request facts → `AnonymousSession`

future request facts 包含：

- HttpOnly same-site cookie 中携带的 session id（adapter 在 backend 持有它的明文）；
- 请求来源 IP；
- cookie / device 派生标识；
- 服务器当前时间（`datetime.now(SHANGHAI)`）。

adapter 必须：

1. 用 `hashlib.sha256` 等 keyed-hash 把 session id / IP / cookie 转成 `session_id_hash` / `ip_hash` / `device_cookie_hash`（明文**不**进 pure 模块）；
2. 调 `build_anonymous_session(config, session_id_hash=..., ip_hash=..., device_cookie_hash=..., now=...)`；
3. 把 `AnonymousSession` 持久化语义留给 backend——本设计**不**指定 session 存储后端，也**不**指定 DB / Redis。

### 2.3 future upload facts → `UploadIntake`

真实 upload handler 完成 single-request 上传后，**已经掌握**：

- 文件名（`file_name`）；
- 字节数（`byte_length`）；
- 通过 `ffprobe` 等工具得到的 `duration_seconds`；
- 通过 streaming hash 得到的稳定 `source_hash`（同一原始文件在不同请求 / 不同设备应得到同一 hash）；
- 临时存储路径（`stored_path: Path`）；
- 是否分片（`is_chunked`，APF2 必须为 `False`）。

adapter 把这些事实组装成 `UploadIntake` dataclass，调 `admit_upload(config, upload)`。**不**让 pure 模块自己去 `Path.exists()` 或读 bytes；temp-storage 健康检查在 adapter 外部完成后通过 `IntakeConfig.temp_storage_available` 传入。

### 2.4 future probe facts → `ProbeResult`

真实 probe 子系统（ffprobe、ffmpeg、自研 audio quality 评估等）跑完后得到 `duration_seconds` / `source_hash` / `media_type` / `audio_present` / `audio_quality_score` / `teaser_candidate_range` / `failure_reason`。adapter 把这些原始 probe 事实包装成 `ProbeResult`，调 `evaluate_probe_result(probe_result)`。

- probe 异常（subprocess crash、timeout）由 adapter 在调 probe **外部** catch；
- catch 后必须调 `fail_closed_from_exception("probe", exc)` 并 `raise from exc`；
- **不允许** silent 把 probe 异常吞掉。

### 2.5 future compliance facts → `ComplianceResult`

合规链顺序固定（C15）：local prefilter → ASR teaser only → LLM compliance。adapter 接收三层综合后的 status + reason + audit_metadata：

- `status ∈ {PASS, BLOCK, NEEDS_MANUAL_REVIEW}`；
- `audit_metadata` 仅保留 30 天的纯文本元数据，**不**包含 media bytes（`bytes` / `bytearray` / `memoryview`）；
- `blocked_media_retained` 恒为 `False`——若 True，pure 模块按 contract 违例 `raise FAILED`。

异常 / timeout 由 adapter 在调 compliance 外部 catch，并 `raise fail_closed_from_exception("compliance", exc) from exc`。匿名路径下 `NEEDS_MANUAL_REVIEW` 被 pure 模块翻译为 `SOFT_REJECTED`。

### 2.6 future counter store facts → 限频判定

`anonymous_preview_intake` 模块**不**持有 counter store。adapter 与未来真实 counter store（fake local JSON / Redis-like / DB）之间的 contract 是：

- adapter 在调 pure intake helper **之前**完成 4 维计数查询：
  - `global:{YYYY-MM-DD@Asia/Shanghai}`；
  - `ip:{ip_hash}:{day}`；
  - `device:{device_cookie_hash}:{day}`；
  - `source:{source_hash}:{day}`；
- 任一维度命中 cap → adapter 自己 raise `IntakeRejected(PreviewStatus.RATE_LIMITED, ...)`，并按 `escalate_to_login_after_rate_limit` 写入 `AnonymousSession.escalated_to_login`；
- counter store 不可读 / 不可写 → adapter raise `IntakeRejected(PreviewStatus.FAILED, ...)`；**不**走旁路、**不**silent fallback。

### 2.7 future storage health facts → temp_storage_available

adapter 外部应有一个 storage health probe（Gateway / app 启动期、定时巡检、或每次 upload 前的浅检查）输出 `bool`。adapter 把该 bool 直接灌入 `IntakeConfig.temp_storage_available`：

- `True` → pure `admit_upload` 放行其余检查；
- `False` → pure `admit_upload` raise `IntakeRejected(PreviewStatus.FAILED, "temp_storage_available is False (fail closed)")`。

storage health probe 的实现细节**不**在本设计范围内。

### 2.8 输出 → status-only preview response

adapter 的对外输出**只**有 status-only preview response：

- 成功路径：调 `build_preview_record(...)` 得到 `PreviewRecord(status=READY_FOR_MODE, ...)`，序列化为 JSON 返回给 frontend；
- 失败路径：catch `IntakeRejected`，构造 status-only `PreviewRecord`（status ∈ `{REJECTED, RATE_LIMITED, SOFT_REJECTED, FAILED}`，`status_reason` 为人类可读原因），返回给 frontend；
- **绝不**返回 `preview_url` / `download_url` / `preview_artifact_key` / clone voice id / pricing / payment / credit 字段——这些是 `FORBIDDEN_PREVIEW_RECORD_FIELDS` 的内容；
- **绝不**在异常分支自动 retry preview media / clone / pricing 操作。

未来 backend / API 层把这份 status-only response 包成 HTTP response 的工作不在本设计范围内。

---

## 3. Fail-closed 总则

下列情况下，APF2c adapter **必须** raise `IntakeRejected` 并把响应翻译成 status-only failed/rejected/soft_rejected/rate_limited preview record，**绝不**进入后续昂贵路径（probe / ASR / LLM / preview media / clone / pricing）：

1. `IntakeConfig` 缺失、字段非法、env 未注入；
2. `temp_upload_dir` 未配置（`None`）；
3. storage health probe 报告 `temp_storage_available=False`；
4. counter store 不可读 / 不可写 / 子目录缺失 / 文件损坏；
5. probe 子系统抛异常 / timeout / 返回 `failure_reason`；
6. compliance 子系统抛异常 / timeout / 返回 `BLOCK`；
7. compliance 返回 `blocked_media_retained=True`（contract 违例）；
8. compliance 返回 `NEEDS_MANUAL_REVIEW`（匿名 soft reject）；
9. `source_type == youtube_url`（匿名 / Free 永远拒绝）；
10. upload 落在 allow-list / size / duration / chunked / extension 之外；
11. rate-limit 任一维度命中 cap；
12. adapter 自己抛异常（必须 `fail_closed_from_exception("...adapter...", exc)`）。

**禁止的 fallback 模式**：

- 不得在 `except` 分支自动调用付费 API；
- 不得自动切换 clone provider；
- 不得 silent fallback 到 pass；
- 不得用 fallback 覆盖真实失败原因；
- 不得在失败分支生成 preview media；
- 不得 silent 丢弃异常并继续。

---

## 4. APF2c 仍在 Human Gate 的项目

下列项目**本任务不解锁**，必须由 Human 在后续 Gate 中显式批准并由 CodeX 拆成新任务：

| 项目 | 当前状态 | 解锁前置 |
|---|---|---|
| 真实 upload endpoint | Human Gate | endpoint 设计 + ownership + 安全审计 |
| 真实 backend adapter runtime 代码 | Human Gate | 本设计完成 + Human 批准 |
| Gateway / Job API 对外暴露 | Human Gate | endpoint 决策 + Caddy 路由 + 鉴权 |
| DB / migration / schema change | Human Gate | DBA review + downtime 评估 |
| Production counter store 选型 | Human Gate | Redis-like / DB / 持久 JSON 决策 |
| Production storage health probe | Human Gate | 健康检查策略决策 |
| Preview media 生成 | Human Gate（APF3） | preview 管线设计 |
| Voice clone provider 调用 | Human Gate（Phase 3b） | 付费 API 决策、CosyVoice / MiniMax 决策 |
| Pricing / payment / points 变更 | Human Gate | 财务 / 合规 review |
| Trial / Paid YouTube 法律文案 | Human Gate | 法律 review |
| 写入 `.env` / secrets / production config | Human Gate | 部署 review |
| `docker-compose.yml` / `Dockerfile*` 改动 | Human Gate | 部署 review |
| Frontend API client 接入 | Human Gate | endpoint 落地后才允许 |
| 任何 `src/`、`gateway/`、`frontend-next/`、`public/` 运行时改动 | Human Gate | 本设计完成 + Human 批准 |

---

## 5. 未来 runtime wiring 的拆分建议

如果 Human 后续批准 APF2c runtime wiring，建议**不**一次端到端实现，而是拆成更小、独立可 Gate 的任务，每个任务都有自己的允许文件清单与 fail-closed 守卫：

1. **APF2c-1 adapter module（src/services/<adapter>.py）**：纯 adapter 模块，仅消费 pure intake helper；**不**接 Gateway、**不**接 upload handler；带专属 contract 测试。
2. **APF2c-2 storage health probe**：判定 `temp_upload_dir` 是否可写；输出布尔；带 fail-closed 测试。
3. **APF2c-3 counter store（fake local JSON 起步）**：fail-closed 包装；选型决策与 Redis-like 替换在更后期任务。
4. **APF2c-4 probe wrapper**：对 ffprobe / 自研工具的薄包装，把异常 / timeout 翻译成 `ProbeResult.failure_reason` 或 `raise`。
5. **APF2c-5 compliance wrapper**：对 local prefilter / ASR teaser / LLM 的串联包装，输出 `ComplianceResult`；**不**在 APF2 阶段调用任何付费 clone provider。
6. **APF2c-6 upload handler stub**：在 Gateway 之外完成 single-request upload + duration / source hash 计算；接 adapter；**不**暴露公网。
7. **APF2c-7 Gateway endpoint**：HTTP 入口；走鉴权、限频、route 到 adapter；带契约测试与对外 API 文档。
8. **APF2c-8 frontend API client**：消费 status-only response；显示 status / soft reject / rate limited / failed UI。
9. **APF3 preview pipeline / Phase 3b clone**：仅在 APF2c-7 完成、Human 显式批准后才启动。

每个子任务都应：

- 单独允许文件清单（不允许跨 src/ + gateway/ + frontend-next/ 一次性改）；
- 单独 fail-closed 测试；
- 单独 Human Gate 批准；
- 单独部署 / 回滚策略。

---

## 6. 测试 scaffold 范围（`tests/test_apf2c_backend_adapter_contract.py`）

`tests/test_apf2c_backend_adapter_contract.py` 是一份**合同测试 scaffold**：

- 允许 import 的：`src.services.anonymous_preview_intake` 中的纯 dataclass / enum / helper / 异常 / 常量；
- **不允许** import：`gateway`、`frontend`、`frontend_next`、`src.pipeline`、`src.services.jobs`、`src.services.tts`、`src.services.voice_clone`、`src.services.assemblyai`、`src.services.gemini`、`src.services.llm`、`src.services.mainland_worker`、`src.services.express`、`src.modules.ingestion.youtube`、`src.modules.output`、`src.modules.draft`、`requests`、`urllib`、`socket`、`httpx`、`boto3`、`aiohttp`、`subprocess` 等真实 provider / network / process 模块；
- fake adapter / counter / storage health / probe / compliance 全部 in-file；
- 文件 I/O 仅限 `tmp_path`；不读写生产路径；
- 不使用 `skip` / `xfail`。

测试至少覆盖：

1. **adapter 成功路径**：fake adapter 接收合法 local upload facts → 调 pure helper → 返回 status-only `PreviewRecord(status=READY_FOR_MODE)`；
2. **`IntakeRejected` → status-only response**：reject 不暴露异常给 caller，转成 status-only `PreviewRecord(status ∈ {REJECTED, RATE_LIMITED, SOFT_REJECTED, FAILED})`；
3. **missing config / temp storage unavailable / counter unavailable** 三类 fail-closed；
4. **`youtube_url` 对 anonymous + free** 双双 fail closed；
5. **`PreviewRecord` 不含禁字段**（`preview_url` / `download_url` / clone voice id / pricing / payment / credit）；
6. **fake adapter 不调** preview media / clone provider / pricing / payment / points / Gateway / API / production counter store —— 通过 "forbidden_calls" ledger 验证空集；
7. **scaffold 自身导入卫生**（AST 扫，确保 import 名单合规）。

---

## 7. APF2c 绝对禁用清单（design / scaffold 阶段）

下列调用 / 操作 / 文件改动在 APF2c **design + scaffold** 阶段（即本任务）**绝对禁止**：

- 任何对 `src/`、`gateway/`、`frontend-next/`、`frontend-next/public/`、`public/`、`migrations/` 的运行时改动；
- 任何对 `.env`、secrets、production config 的改动；
- 任何对 `docker-compose.yml`、`Dockerfile*`、`alembic.ini`、`main.py` 的改动；
- 任何 ASR / LLM / TTS / clone provider / 对象存储 / DB / 网络调用——无论是真实 provider 还是 fake stub 走真实路径；
- 任何 pricing / payment / points / 套餐变更；
- 任何 Gateway / Job API endpoint 注册或路由改动；
- 任何 preview media 生成。

本设计只允许写：

- 本设计文档；
- `tests/test_apf2c_backend_adapter_contract.py` scaffold（in-file fake、纯值、`tmp_path`）；
- 给 CodeX 的完成报告。

---

## 8. 变更记录

- **2026-06-02**：初版，凝固 APF2c adapter design 边界 + scaffold 范围。
