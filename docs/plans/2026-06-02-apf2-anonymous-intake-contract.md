# APF2 匿名 intake 契约（design + fail-closed test scaffold）

**状态：** DESIGN ONLY  
**日期：** 2026-06-02  
**任务：** APF2a-anonymous-intake-contract-test-scaffold  
**关联：**

- 漏斗 UX 方案：`docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md`
- APF2 设计边界：`docs/ai-workgroup/inbox/Human/2026-06-02T170807_from-CodeX_to-Human_type-report_task-APF2-design-boundary-spec.md`
- APF2 Human Gate 决策表：`docs/ai-workgroup/inbox/Human/2026-06-02T171740_from-CodeX_to-Human_type-report_task-APF2-human-gate-decision-table.md`
- Human 决策实例：`docs/ai-workgroup/inbox/CodeX/2026-06-02T175427_from-Human_to-CodeX_type-instruction_task-APF2-human-gate-decision-table-human-decision.md`
- 任务说明：`docs/ai-workgroup/working/Claude-Code/2026-06-02T175950_from-CodeX_to-Claude-Code_type-instruction_task-APF2a-anonymous-intake-contract-test-scaffold.md`

本文不是实施授权。本文只是把 Human 已批准的决策表逐条凝固成可被 fake fail-closed 测试验证的契约形式，并给出测试 → 契约的映射。任何 `src/`、`gateway/`、`frontend-next/`、`migrations/`、`docker-compose.yml`、`Dockerfile*` 变更仍在 Human Gate 之内。

---

## 1. 范围

本契约覆盖匿名 intake 流程从“匿名访客点 立即试用”到 preview record 创建（status-only）之间的全部决策：

- 匿名 session 创建与生命周期；
- 匿名本地 upload 校验；
- probe（媒体探测）行为；
- 合规检测（local prefilter → ASR teaser → LLM compliance）；
- 限频（global / IP / cookie/device / source hash）；
- preview record 数据契约；
- YouTube 不开放给匿名/Free；
- 失败/异常/超时一律 fail-closed。

本契约 **不**覆盖、且 APF2 阶段 **必须不实现** 的内容：

- preview media 生成、preview artifact 投递；
- 任何 clone provider 调用（CosyVoice / MiniMax / 其他）；
- pricing / points / payment / 套餐变更；
- migrations / schema changes / 任何 DB 表新增或修改；
- deployment、secrets、production config；
- 后端 / API client / upload / preview / clone 真实代码；
- Trial/Paid YouTube 路径；
- YouTube 法律文案；
- claim token 实际实现（只保留 placeholder 字段）。

---

## 2. Human Gate 决策 → 契约凝固

下表把 `APF2-human-gate-decision-table` 的 25 行决策转成契约。第 3 列是 fake fail-closed 测试要覆盖的契约编号，可在 `tests/test_apf2_anonymous_intake_contract.py` 中按编号查到 case。

| # | 决策项 | 契约 | 契约编号 |
|---|---|---|---|
| 1 | 匿名 session TTL | 24 小时；session 创建时显式写入 `expires_at = created_at + 24h` | C1 |
| 2 | session 承载方式 | HttpOnly same-site cookie 携带 session id；服务端持久化只保存 `session_id_hash`，**不**存原始 id | C2 |
| 3 | 设备指纹 | APF2 不做侵入式 fingerprinting；仅用 cookie + IP + source hash 三因素 | C3 |
| 4 | 上传文件类型 | 仅 `mp4`、`mov`、`m4v`、`webm`；其他类型直接 reject | C4 |
| 5 | 最大上传大小 | 500 MB 硬上限；> 500 MB reject | C5 |
| 6 | 最大源时长 | intake 30 分钟硬上限；preview 仍 3 分钟（preview 在 APF3） | C6 |
| 7 | 上传模式 | 单请求上传；chunked 后置（APF2 不支持） | C7 |
| 8 | 临时上传存储 | 受控临时目录，必须有 TTL cleanup 责任人；config 未指定则 fail closed | C8 |
| 9 | 合规拦截后处理 | 写审计 metadata 后**删除** source bytes；不保留 blocked media bytes | C9 |
| 10 | preview record 存储 | 临时 JSON/object-store record contract；**禁止** 在 APF2 提交任何 migration 或 schema | C10 |
| 11 | preview record TTL | 24 小时 | C11 |
| 12 | rate limit 维度 | 全局每日 + IP 每日 + cookie/device 每日 + source hash 每日，4 维同时生效 | C12 |
| 13 | 初始限额 | global 500/day；IP 3/day；cookie/device 2/day；source hash 1/day | C13 |
| 14 | rate-limit 存储 | fake/local JSON scaffold；counter store 读写不可用时 fail closed | C14 |
| 15 | 合规顺序 | local prefilter → ASR teaser only → LLM compliance → 通过才允许进入 translation/TTS/clone | C15 |
| 16 | 匿名 `needs_manual_review` | 视为 soft reject；引导登录/人工，**不**自动放行 | C16 |
| 17 | 合规超时/异常 | fail closed | C17 |
| 18 | 合规审计保留 | metadata 30 天；不保留 blocked media bytes | C18 |
| 19 | 匿名/Free YouTube | 禁用，不显示 UI 字段；后端也 fail closed | C19 |
| 20 | Trial/Paid YouTube | APF2 **不实现**；契约层面占位 placeholder | 占位 |
| 21 | YouTube 法律文案 | APF2 **不实现** | 占位 |
| 22 | claim token | APF2 只保留 record 字段 placeholder；不签发 token、不消费 token | C22 |
| 23 | captcha/login 升级 | 超限后 escalate 到登录；APF2 不引入 captcha 第三方依赖 | C23 |
| 24 | APF2 输出 | **status-only**：intake/probe/compliance 状态；**不**生成 preview media | C24 |
| 25 | clone providers | APF2 **绝对禁用** clone provider 调用 | C25 |

---

## 3. 契约细节

### 3.1 IntakeConfig（C1–C13 / C18 配置真源）

intake 流程要消费一份 `IntakeConfig`（值对象），字段及默认：

| 字段 | 默认 | 用途 |
|---|---|---|
| `session_ttl_seconds` | `24 * 3600` | C1 session 过期 |
| `allowed_upload_types` | `("mp4", "mov", "m4v", "webm")` | C4 |
| `max_upload_bytes` | `500 * 1024 * 1024` | C5 |
| `max_source_duration_seconds` | `30 * 60` | C6 intake 接受时长 |
| `single_request_upload_only` | `True` | C7 |
| `temp_upload_dir` | `None`（生产环境必须显式注入） | C8 |
| `temp_upload_ttl_seconds` | `24 * 3600` | C8 cleanup |
| `preview_record_ttl_seconds` | `24 * 3600` | C11 |
| `rate_limit_global_per_day` | `500` | C13 |
| `rate_limit_per_ip_per_day` | `3` | C13 |
| `rate_limit_per_device_per_day` | `2` | C13 |
| `rate_limit_per_source_hash_per_day` | `1` | C13 |
| `compliance_audit_retention_seconds` | `30 * 86400` | C18 |
| `youtube_enabled_for_anonymous` | `False` | C19 |
| `youtube_enabled_for_free` | `False` | C19 |
| `escalate_to_login_after_rate_limit` | `True` | C23 |

任何字段缺失或为非法值，intake runner 必须 fail closed。

### 3.2 AnonymousSession（C1 / C2 / C3）

字段：

- `session_id_hash`：服务端只持久化 hash；
- `created_at`、`expires_at`；
- `ip_hash`、`device_cookie_hash`（cookie 派生）；
- `source_hash`（upload 完成后填入）；
- `escalated_to_login: bool`；
- **不**包含设备指纹、UA、Canvas、WebGL 等字段。

`expires_at == created_at + IntakeConfig.session_ttl_seconds` 必须严格成立。

### 3.3 UploadIntake（C4–C9）

upload 完成回 intake runner 时携带：

- `file_name`、`extension`（取自 `Path(file_name).suffix.lstrip('.').lower()`）；
- `byte_length`；
- `duration_seconds`（来自 probe）；
- `source_hash`（upload 完成后才有的稳定 hash，进入 preview record + rate limit key）；
- `stored_path`（受 IntakeConfig.temp_upload_dir 管控）。

Reject 条件（任一命中即 fail）：

- `extension` 不在 `IntakeConfig.allowed_upload_types`；
- `byte_length > IntakeConfig.max_upload_bytes`；
- `duration_seconds > IntakeConfig.max_source_duration_seconds`；
- 多段/chunked 入参；
- `temp_upload_dir` 不可写 / 不存在 / config 未注入。

Reject 时：

- preview record 进入 `rejected` 状态；
- 已落地的 source bytes 必须由 cleanup 路径删除（C9 / C8）；
- **不**进入 probe / 合规 / 翻译 / TTS / clone。

### 3.4 ProbeResult（C15）

probe 必须先于翻译/TTS/clone 发生，输出：

- `duration_seconds`（覆盖 upload claim）；
- `source_hash`；
- `media_type`；
- `audio_present`、`audio_quality_score`；
- `teaser_candidate_range`（供 APF3 使用，APF2 阶段只填字段不消费）；
- `failure_reason`（字符串或 None）。

probe failure 时：

- preview record 进入 `failed`；
- **不**触发 ASR / LLM compliance / 翻译 / TTS / clone。

### 3.5 ComplianceResult（C15–C18）

合规顺序固化：

1. local prefilter（关键词/规则）；
2. ASR teaser only（且仅在 prefilter pass 后；ASR 输入限于 ≤180s teaser）；
3. LLM compliance on transcript；
4. 通过才进 translation/TTS/clone。

输出：

- `status ∈ {"pass", "block", "needs_manual_review"}`；
- `reason`；
- `audit_metadata`（30 天保留）；
- `blocked_media_retained: False`（恒为 False，C9 / C18）。

异常/超时 → status 必须为 `block`，并通过 `failure_reason` 标记原因；**禁止**将异常静默 fallback 到 `pass`。

匿名路径下 `needs_manual_review` 由 intake runner 转化为软拒绝（preview record 状态 `soft_rejected`），不允许后续 TTS/clone。

### 3.6 RateLimitCounters（C12–C14 / C23）

intake 创建会话或接收 upload 之前必须查 4 维计数：

- global daily：`global:{YYYY-MM-DD@Asia/Shanghai}`；
- per-IP daily：`ip:{ip_hash}:{day}`；
- per-cookie/device daily：`device:{device_cookie_hash}:{day}`；
- per-source-hash daily：`source:{source_hash}:{day}`。

counter store 选型在 APF2 阶段为 fake/local JSON（`tmp_path` 下），未来真实实现可换 Redis-like。任一读/写失败 → `RateLimitUnavailable` → intake runner fail closed。

超限路径：

- 任一维度超限 → preview record `rate_limited` + escalate-to-login hint（C23）；
- 不进入 probe / 合规 / 翻译 / TTS / clone。

### 3.7 PreviewRecord（C10 / C11 / C22 / C24）

status-only 数据契约，**不**含 preview media、不含可下载 artifact、不含克隆 voice id。

字段：

- `record_id`；
- `session_id_hash`；
- `source_hash`、`upload_hash`；
- `source_type`（`local_upload`；YouTube 在 APF2 永远不可达）；
- `status` ∈ `{"created", "source_uploading", "source_ready", "probing", "compliance_checking", "rejected", "rate_limited", "soft_rejected", "ready_for_mode", "failed", "expired"}`；
- `status_reason`；
- `duration_seconds`、`audio_present`；
- `compliance_status`、`compliance_audit_metadata`；
- `selected_mode_placeholder`、`recommended_mode_placeholder`（仅占位，APF2 阶段不写入实际 mode）；
- `claim_token_placeholder`（C22，**不**签发、**不**消费）；
- `created_at`、`expires_at` = `created_at + IntakeConfig.preview_record_ttl_seconds`。

禁止字段：

- 任何 `preview_artifact_key`、`preview_url`、`download_url`；
- 任何 `clone_provider_voice_id` / `clone_reservation_id` / `voice_clone_*`；
- 任何 `payment_*` / `pricing_*` / `credit_*`。

### 3.8 YouTube gate（C19）

intake 入口 schema 中 `source_type` 仅允许 `local_upload`。`source_type == "youtube_url"` 在匿名/Free 路径下 fail closed，永不调用 entitlement 或 probe。

trial/paid YouTube 路径不在 APF2 范围内：当前契约不消费 entitlement flags，不写法律文案。

### 3.9 fail-closed 总则（覆盖 C1 / C14 / C17 / 任意配置/存储不可用）

下列情况一律拒绝创建 / 推进 intake，且不进入任何昂贵步骤：

- `IntakeConfig` 未注入或字段非法；
- session 存储不可写；
- rate-limit counter store 不可读/不可写；
- probe 抛异常或超时；
- compliance 抛异常或超时；
- temp upload dir 不可写或未配置。

intake runner 不得在 `except`/兜底分支自动放行；不得自动调用付费 API；不得用 fallback 覆盖失败原因。

### 3.10 APF2 绝对禁用清单（C24 / C25 / 全局）

下列调用在 APF2 实现阶段绝对不允许出现（无论是真实 provider 还是 fake stub 走真实路径）：

- preview media 生成（任何形式）；
- clone provider（CosyVoice / MiniMax / 其他）；
- pricing / points / payment 相关计算与扣除；
- migrations / schema changes；
- 写入 `.env` / secrets / production config；
- 任何对 `src/`、`gateway/`、`frontend-next/`、`public/` 的运行时改动；
- 任何对 `docker-compose.yml`、`Dockerfile*` 的改动。

---

## 4. 测试 scaffold 设计原则

`tests/test_apf2_anonymous_intake_contract.py` 是一份 contract test scaffold：

- 用 fake 数据类 + fake intake runner 表达上述契约；
- 不导入 `src/`、`gateway/`、`frontend-next/` 任何模块；
- 不调用任何真实外部服务（ASR / LLM / TTS / clone provider / 对象存储 / DB）；
- counter store 用 `tmp_path` 下的本地 JSON 文件 scaffold；
- 不使用 `skip` / `xfail`；
- 每个 contract case 都直接 assert intake runner 在该场景下的 fail-closed 行为；
- 测试只验证 **契约**，不验证未来真实实现的内部细节。

后续 APF2c backend 落地任务（Human 批准后）需以这份 scaffold 作为验收基准：真实 intake runner 必须满足这套契约的全部测试。如果未来契约需要演化，必须先改 design 文档与测试，再改实现。

---

## 5. 后续 Human Gate（未在本任务解锁）

下列项目仍在 Human Gate 之内，本任务**不解锁**：

- 真实 backend intake / Gateway endpoint 实现（APF2c）；
- 真实 upload / probe / compliance 接线（APF2c）；
- counter store 选型（Redis-like / DB / 持久 JSON）；
- preview media 生成（APF3）；
- Express 匿名 CosyVoice 临时克隆（Phase 3b）；
- Smart 增强预览与 claim flow（Phase 4）；
- Trial/Paid YouTube 法律文案与 entitlement flags；
- pricing / points / payment 任何变更；
- migrations / schema changes。

任何上述项目需要由 Human 在新的 Gate 中显式批准并由 CodeX 派发新任务。

---

## 6. 变更记录

- 2026-06-02：初版，凝固 Human Gate 决策表 25 行为契约 + 测试 scaffold 范围。
