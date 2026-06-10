# APF 匿名 Free 预览 P0 — 垂直切片实施方案（v2，含外部双评审修正）

**状态：** APPROVED-DRAFT / 待项目主批准开工
**日期：** 2026-06-10（v2 同日修订）
**基线：** main @ f05a454a（三方合并完成，566 tests green）
**上游方案：** [`2026-06-01-anonymous-preview-funnel-ux-plan.md`](2026-06-01-anonymous-preview-funnel-ux-plan.md)（主方案）；契约层：apf2 / apf2c / apf3a 三份子方案
**评审记录：** v1 = 架构师草案 + 安全/合规评审（21 条，F1–F21）+ 交付节奏评审（19 条）。v2 = 并入 Gemini 外部评审 5 条（G1–G5：record 存储改 PG、结算 bypass 落点、匿名标记穿透、AD-4 隔离声明、in-flight 索引）+ CodeX 外部评审 5 条（C1–C5：Free-only 定位、合规 skip 路径 fail-closed、限频 mode 维度、风险接受标注、T6 薄 adapter）+ CodeX 审计报告确认项（S2 Pass3 硬验收、T3/T4 并行澄清、payload 白名单提前到 T1）。

> **与主方案 Phase 3a 的关系（C1）：** 本切片只交付 **Free 档**匿名预览（P0 灰度），**不**覆盖 Phase 3a 的 Express 档匿名预设预览与方案选择漏斗。Phase 3a 完整交付 = 本切片 + 后续 `Express preset preview` 任务（见 §11 backlog）。文档/汇报不得将本切片表述为"Phase 3a 已完成"。

---

## 0. 一句话定义

**匿名用户本地上传视频 → ffmpeg 重编码截取 ≤180s teaser → ffprobe → 风控四 key fail-closed → 本地规则预筛 → 走现有 free 非交互 pipeline（内含 ASR+LLM 合规、MiMo 预设音色、水印）→ gateway stream-only 播放（不可下载）→ TTL 清理。** 双端 flag 默认关，对生产零影响。**全程零 clone provider 调用。**

## 1. 背景

APF 前几个 Phase 只落了纯契约层（5 个 side-effect-free 模块 + 契约测试，已全部合入 main），没有任何端到端可产出预览的路径；首页"立即试用"CTA 仍是占位。评审结论：停止契约抛光，转垂直切片。本切片复用 free tier 既有水印/时长门/预设音色/下载 gate，复用 gateway 上传/路由/migration/startup 模板，**合规和付费调用只在 pipeline 内跑一次**（见 AD-2，这是对草案最重要的修正）。

## 2. 目标

1. 端到端跑通匿名 → 预览播放的最小路径，不依赖登录。
2. 已合入的 5 个契约模块全部被运行时消费（不重写、不绕开）。
3. 风控四 key 落 Postgres，计数存储不可用一律 fail-closed 拒绝。
4. 双端 feature flag 默认关；每个任务合并后 main 绿、生产零影响。
5. 匿名 job 的所有权与终态结算**显式定义**，不撞"终态结算单一入口"红线。

## 3. 非目标（明确砍掉）

- **YouTube / URL 源**：intake 只允许 `local_upload`。
- **任何 clone provider**：CosyVoice / MiniMax / MiMo voiceclone 一律不碰。`voice_strategy=preset_mapping` 确保 dispatch 走 `_generate_one_mimo` 纯预设路径（`tts_generator.py:1385-1391` 分支条件不满足、reference 不 stamp）。
- **Smart / 600 定价 / 平滑抵扣 / 信用账本写入**：全部后置；本切片对 credit ledger **零写入**。
- **claim token 消费/绑定**：仅生成随机串存入 record 占位（Phase 4 再接）。
- **Express 档匿名预设预览（Phase 3a 另一半）**：后续任务，见 §11（C1）。
- **≤720p 降分辨率**：全仓库无现成实现，v1 砍掉——水印 + stream-only + 180s 已足够防搬运。**【P0 灰度风险接受（C4）：主方案 Phase 0 已拍板 ≤720p，此处砍掉只代表灰度期取舍，不代表正式匿名漏斗规格，正式化前须回填】**
- **device fingerprint / IP-/24 / UA 风控**：不做。**【同上，P0 灰度风险接受（C4），主方案 §14 的完整风控矩阵留 backlog】**
- **R2 / 对象存储**：预览产物只走 local，不进 `EAGER_PUSH_TO_R2_KEYS_*`。
- **低优先级 pull 队列改造**：项目无 pull 队列；"低优先级"以 in-flight 并发 gate 实现（AD-8）。

## 4. 架构决策（AD）

### AD-1 窗口处理 = 上游 ffmpeg 预截取，不穿透 pipeline

teaser 截取在 intake/probe 之后、create_job 之前：ffmpeg **重编码精确切割**（非 `-c copy`，避免关键帧溢出 180.x s 触发 `>` 拒绝）截前 180s 生成子文件，子文件走**完整未改**的 free 非交互 pipeline。理由：pipeline 是 ~12000 行单体编排，窗口语义穿透 8+ 阶段改动面巨大；180s 子文件天然把 ASR/LLM/TTS 成本封顶。

**两个时长 cap 严格分层（评审 F4）：**
- `IntakeConfig.max_source_duration_seconds` = **源级**上限（30min，intake 契约默认），拒绝超长源；
- teaser 180s 由 `evaluate_free_duration_cap(duration_ms, max_minutes=3)`（`src/utils/free_duration_gate.py:24`，keyword 参数零改动复用）把守**子文件**；
- `ProbeResult.duration_seconds` = teaser 时长；`ProbeResult.source_hash` = **源文件** hash（必须与 upload 一致，否则 adapter:243 mismatch fail-closed）；
- 测试含 179/180/180.04/181s 容差用例。
- **不动** `FREE_DURATION_CAP_MINUTES=10` 常量（free tier 正式任务仍是 10 分钟）。

### AD-2 合规与付费调用只在 pipeline 内跑一次（草案最重大修正）

pipeline 已自带完整合规链（`src/pipeline/process.py:10482-10518`：本地规则 → LLM → `combine_content_compliance_results`）。gateway 侧若再跑 ASR+LLM 等于**每个预览双倍付费调用**，且同步 intake 会把上传请求挂 1-5 分钟。因此：

- gateway 注入 adapter 的 `ComplianceFn` 只做**本地规则预筛**（纯 stdlib、同步、免费；输入 = 文件名等可得文本，作 sanity 预筛）；
- 重合规（ASR teaser + LLM）由 pipeline 既有 stage 跑**一次**；
- **匿名身份显式穿透（G3+C2 合并拍板）**：create payload 携带 `anonymous_preview=true` 标记（进字段白名单），pipeline 据此进入**匿名严格合规 lane**——不靠 sentinel user_id 字符串匹配，也**不**对所有登录 free 任务一刀切（否则 admin 关合规总开关会误杀整个登录 free tier）；
- **匿名 lane 三条 skip 路径全部 fail-closed（C2，已核验代码）**：现有 `_run_content_compliance_review` 有三条放行/跳过路径，匿名 lane 必须逐条堵死——
  1. `is_content_compliance_enabled()` False → 现状整体 `skipped`（`process.py:10474-10479`）→ 匿名 lane 改为**拒绝任务**；
  2. LLM 第二层未启用/未配置（`is_content_compliance_llm_enabled()` False 或 `llm_generate_json is None`）→ 现状静默跳过第二层（`process.py:10515-10516`）→ 匿名 lane 改为**拒绝任务**；
  3. LLM 跑了但出错 → `llm_fail_closed` 代码级强制 True（pipeline 默认读 env `AVT_CONTENT_COMPLIANCE_LLM_FAIL_CLOSED`，默认 False fail-open——匿名 lane 不读 env；评审 F20）。
  登录用户 free 任务三条路径行为**不变**；
- 转录**近空**（有音轨但转录字符数低于阈值）→ 按 `needs_manual_review` 软拦截，不是 pass（评审 F7）;
- `needs_manual_review` → blocked（匿名无人工复审，主方案 §8.1），结果映射回 preview record。

### AD-3 preview record = gateway PG 表（v2 修订，G1）

~~v1：gateway-local 纯 stdlib JSON~~ → **v2 改为 PG 表 `anonymous_preview_records`**（并入 035 migration 第三张表）。改判理由：T2 反正要建 035，第三张表边际成本≈0；JSON 文件方案有锁竞争、sweep 全目录遍历、未来多副本不一致三个问题；Phase 4 claim 本来就需要跨会话查询。**保持不变的约束**：record store 仍封装在独立模块 `gateway/anonymous_preview_record_store.py` 后面（接口不变，PG-backed 实现），adapter wiring 无感；teaser/媒体文件仍落磁盘（只有元数据/状态进 DB）；**gateway 禁止 import `services.jobs`**（pydub 传染）依旧成立——PG 读写走 gateway 自有 models/SQLAlchemy。

**import 命名空间陷阱（评审 F18，必须有烟测）：** 契约模块内部用 `src.services.*`，gateway 全栈用 `services.*`；若两条路径都可达会装载两份模块，enum 身份比较静默失效（`isinstance` 全 False → 功能全灭）。T3 验收必须含 gateway 容器 sys.path 下的烟测：`services.anonymous_preview_intake is src.services.anonymous_preview_intake`（或统一单一命名空间 + 守卫）。可行性前提（已核验）：`docker-compose.yml:193-196` 将整棵 `src/` 只读 bind mount 进 gateway 容器；`content_compliance.py`、`assemblyai/transcriber.py` 均纯 stdlib 可被 gateway import；gateway 镜像自带 ffmpeg（`gateway/Dockerfile:9`）。

### AD-4 匿名 session = 新表 + 独立 cookie

`anonymous_sessions` 表（并入 035 migration，**含 `expires_at`** + 过期清理）、独立 cookie `avt_anon`（**HttpOnly + Secure + SameSite=Lax + Max-Age=TTL**，评审 F17）、新 dependency `require_anonymous_session`，照 `gateway/auth.py` 随机 token 模式。**不复用** `sessions` 表。给 Phase 4 claim 绑定留行。
**隔离声明（G4）**：此决策是对 AGENTS.md "Reuse existing session infrastructure where possible" 的**有意识偏离**——匿名 session 生命周期（24h TTL、无用户绑定、claim 单向升级）与登录 session 属性差异过大，强行复用会污染 `sessions` 表语义；T2 的 PR 描述必须显式写明这一点。

### AD-5 风控四 key 与 adapter 的真实映射（评审 F5 关键修正）

adapter `_enforce_rate_limits`（`backend_adapter.py:394-408`）硬编码的四 key 是 **global / ip / device / source**——没有 session key。拍板映射：

| adapter key | 实现 | cap（config 可调） |
|---|---|---|
| `global:` | 全局每日 | 500 |
| `ip:` | 受信代理提取的 client IP | 3 |
| `device:` | **`raw_device_cookie` := `avt_anon` session token**（即"匿名 session 1/天"的落点） | **1** |
| `source:` | 源文件 sha256 | 1 |

- 计数落新表 `anonymous_preview_daily_usage`（035，照 034 `free_service_quota` ledger + 原子 `try_acquire`；day key = Asia/Shanghai）；**唯一键含 `mode` 维度：`(scope, scope_key, mode, usage_date)`，v1 恒 `mode='free'`（C3）**——为主方案"匿名 Free/Express 各 1 次/天"预留 schema 槽位，未来开 Express 预览不需要再迁移；caps 语义即"per-mode cap"，v1 只放开 free；
- 任何 DB 异常 → raise `RateLimitCounterUnavailable`（复用契约异常）→ adapter fail-closed；**不复用** `gateway/risk_control.py`（进程内 deque，重启清零）；
- **IP 提取必须复用 `gateway/auth_phone.py:131-186` 受信代理版**（仅信 CF-Connecting-IP/受信代理，否则 socket IP），禁止裸读 XFF（评审 F10）；测试：不受信 peer 带伪造 XFF → 按 socket IP 计数；
- **Hasher = HMAC-SHA256(服务端密钥, value)**，密钥走 env（≥32B，启动校验）；表的 `scope_key` 只存不透明 hash，schema 不得出现 raw IP 列（评审 F14）；
- **`decrement` 仅限 adapter 多 key rollback 调用**（计数后、probe/合规前）；probe 开始后任何失败**永不退计数**，防 source-hash 槽位无限重放付费调用（评审 F3）；守卫测试钉死；
- global cap 命中时落结构化 WARNING + metric（配合 admin 开关作人工熔断，评审 F11）。

### AD-6 stream-only 经 Job API 代理字节流，不走 download 链

job_intercept download 链语义是"可下载 artifact"（attachment disposition），不复用。新端点 `GET /gateway/anonymous-preview/{preview_id}/stream`：**经 Job API 代理字节流**（照 Phase 2 local 直通分支）并改写 `Content-Disposition: inline`、支持 Range——**不要 gateway 直接 `FileResponse(app容器路径)`**：Job API 返回的是 app 容器视角路径（`/opt/aivideotrans/app/projects/...`），gateway 挂的是 `/opt/aivideotrans/data/projects`，直读本地测试绿、生产 404（评审 F21）。gate = record 存在 + session 匹配 + TTL 未过 + admin 开关 + `artifact_policy.stream_only_required`。`downloadable_keys.py` 加 `anonymous_preview` 显式分支（download=∅、stream={video}），沿用"显式分支、不默认 Studio"反绕过约定。

### AD-7 匿名 job 的所有权与终态结算（评审 F19/交付#4，开工前必须落实）

绕开 `intercept_create_job` 独立 surface，但匿名 job **不能游离**于 gateway 的 orphan reconciliation（`job_intercept.py:915`）/ terminal mirror / cleanup 之外。拍板：

- 建一个 **sentinel 匿名系统用户**（如 `anonymous-preview@system`，迁移时插入）持有匿名 job；
- gateway **照建 Job 行**（标记列 `jobs.is_anonymous_preview`，035 加列 + **partial index `ON jobs(status) WHERE is_anonymous_preview`**，供 in-flight 计数查询走索引不扫全表，G5）；
- 终态仍走 `mirror_job_terminal_state` **单一入口**（项目教训：旁路结算曾致扣点事故）。**结算 bypass 代码落点（G2，已核验）**：`gateway/job_terminal_mirror.py:61` 的行为矩阵在 terminal 转换时调 `settle_job_quota`——匿名 job 不加拦截则"零消耗"断言必红。修法：在 mirror **内部的结算步**对 `is_anonymous_preview` 显式跳过 settle/ledger 写入；**禁止整函数提前 return**（那会连状态字段镜像一起跳过，orphan reconciliation 和 cleanup 全部失灵）；
- **验收为断言级**：匿名任务到终态后 `credit_ledger` 零新行、`free_service_quota` 零消耗、**PG Job 行状态字段照常进终态**；orphan reconciliation **不收编**匿名 job（测试覆盖）；
- create payload 最小集：`job_type=localize_video, source_type=local_video, source_ref=<teaser路径>, output_target=editor, service_mode="free", requires_review=false, voice_strategy=preset_mapping, tts_provider="mimo", source_content_hash, anonymous_preview=true`（G3 标记字段）——payload **字段白名单**定义为 T1 的共享常量 `ANONYMOUS_PREVIEW_PAYLOAD_SPEC`（不得出现 `voice_clone`/`voiceclone_reference_path` 等字段），T3/T5/T8 引用同一份；
- **双门**：匿名 create 同时要求 `AVT_ENABLE_FREE_TIER=true` 与匿名自身 flag（匿名 surface 不绕过 free tier 总开关语义，交付#5）；
- consent 照 `gateway/free_consent.py` 三件套：strict-bool 验证 + 服务端盖 `server_confirmed_at` + 转发前 pop 客户端夹带值（新建 `gateway/anonymous_consent.py`）。

### AD-8 上传防滥用与并发上限（评审 F2/F12）

- **读 body 之前**廉价预检：flag + admin 开关 + session 存在 + `store.get` 非递增 peek global/IP 计数 + Content-Length 上限；
- 流式写盘 + 硬截断 + streaming sha256；`anonymous_preview_max_upload_bytes` 在 config 定死（**默认 200MB**，远小于登录上传 2GB）；
- 任何非 `READY_FOR_MODE` 结局**立即删除**已落盘文件（不等 sweeper）；
- **in-flight gate**：非终态匿名预览任务数 ≥ 2 → 429，同 fail-closed 语义——这就是"匿名不饿死付费任务"的 v1 实现；该值经 **admin settings 运行时可调**（500/天 ÷ 2 并发槽位灰度后可能偏紧），计数查询走 AD-7 的 partial index；
- 所有拒绝路径落结构化 WARNING（reason code，JSONL 模式），保证上线后 cap 可调参、滥用可发现。

### AD-9 契约模块消费映射（实现者不得绕开/重写）

| 契约模块 | 消费方 | 方式 |
|---|---|---|
| `anonymous_preview_intake.py` | T3/T4/T5 经 adapter 间接 | 值对象 + fail-closed helper，不直接调 |
| `anonymous_preview_backend_adapter.py` | T3 | 构造 `RequestFacts`/`UploadFacts`，注入 PG counter store / probe fn / 预筛 fn / HMAC hasher / clock，调 `handle_intake`；**adapter 永不 raise，失败=status-only record** |
| `anonymous_preview_admission.py` | T6 | `evaluate_anonymous_preview_admission(config, mode="free", source_duration_seconds=teaser_dur)`，`artifact_policy` 驱动 stream gate；Express/Smart/Studio decision 拒进 lane |
| `anonymous_preview_rate_limit.py` | T2 | PG store 不可用时 raise `RateLimitCounterUnavailable`；`InMemoryRateLimitCounterStore` 仅测试夹具 |
| `anonymous_preview_storage_health.py` | T1/T3 | startup 校验 + 每次 intake 前算 `temp_storage_available`，不可写 fail-closed |

## 5. 单预览付费调用清单与日成本上界（评审 F1）

admitted 预览走 pipeline 一次，付费调用 = **ASR(≤180s) + LLM 合规 ×1 + 翻译 LLM + S2 Pass1/2（多模态）+ MiMo TTS(≤180s)**。乘 global cap 500/天即最坏日成本上界——这是**已知可被恶意打满的预算承诺**，须在 T1 时按当前各 provider 单价算出数字写进 admin 告警阈值说明。
**待核验项（T5 验收）：** free `preset_mapping` 路径下 S2 Pass 3（音色画像，多模态付费）是否会跑；预设音色不需要画像，若跑则匿名 lane 跳过。

## 6. 任务拆解

> 规模：S ≈ 半天 / M ≈ 1 天 / L ≈ 1.5–2 天。每条单 agent 单 PR；合并后 main 绿、默认 flag 关生产零影响。

| T | 标题 | 主要改动 | 关键验收（可测试） | 依赖 | 规模 |
|---|---|---|---|---|---|
| **T1** | 双端 flag + admin 开关 + 启动校验 + config 数值 | `gateway/config.py`（`enable_anonymous_preview=False`、`anonymous_preview_max_seconds=180`、`anonymous_preview_max_upload_bytes=200MB`、caps 500/3/1/1、HMAC 密钥 env）；**payload 字段白名单常量 `ANONYMOUS_PREVIEW_PAYLOAD_SPEC`**（T3/T5/T8 共用）；**§5 日成本上界按当前 provider 单价算出数字**；`gateway/admin_settings.py`（`anonymous_free_preview_enabled: StrictBool=False`、`anonymous_preview_max_in_flight=2` 运行时可调）；`gateway/startup_checks.py`（降级型校验，照 `validate_mainland_voice_worker_config`，CRITICAL+降级不崩容器）；`docker-compose.yml`/`.env.example`；**`frontend-next/Dockerfile` + compose 的 `NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW` build args**（f81aa6e0 同坑，构建期烘焙） | flag 默认全 False；StrictBool 拒 coercion；HMAC 密钥缺失且 flag 开 → CRITICAL+降级；build args 贯通 | 无 | M |
| **T2** | PG migration 035 + counter store | 新 `035_anonymous_preview.py`（**三表一列一索引一次建**：`anonymous_preview_daily_usage` 照 034 ledger **含 mode 列**、`anonymous_sessions` 含 expires_at、`anonymous_preview_records`（G1）、`jobs.is_anonymous_preview` 列 + partial index（G5）、sentinel 用户行）；新 `gateway/anonymous_preview_quota.py`（`try_acquire` 原子、HMAC hasher、`RateLimitCounterUnavailable`）；**PR 描述写明 AD-4 有意识隔离声明（G4）** | up/down 可逆；并发不超 cap；DB 异常 raise 契约异常；满足 `CounterStore` Protocol（结构子类型测试）；scope_key 无 raw IP；decrement 仅 rollback 可调（守卫）；mode 维度入唯一键 | T1 | M |
| **T3** | 匿名上传 + record store + adapter wiring | 新 `gateway/anonymous_upload.py`（预检在读 body 前、流式截断、streaming sha256、`uploads/anonymous/{session}/` 隔离、受信代理 IP、CSRF same-origin、非 admit 即删）；新 `gateway/anonymous_preview_record_store.py`（**PG-backed，写 `anonymous_preview_records` 表**，接口对 adapter 无感，G1）；adapter wiring 用 **Protocol stub** 的 probe/预筛 fn 先行 | record 入库全链（注入 fake）；故障 store → status=FAILED 不 raise；**import 烟测 F18**；不 import `services.jobs`（测试）；拒绝路径结构化 WARNING | T2 | L |
| **T4** | teaser 重编码截取 + probe fn | 新 `gateway/anonymous_preview_probe.py`（ffmpeg 重编码精确切 180s；ffprobe；`evaluate_free_duration_cap(max_minutes=3)`；duration=teaser、hash=源；失败 reason redacted） | NaN/inf/0/None fail-closed；179/180/180.04/181s 用例；hash 与 upload 一致；与 T3 stub 接口吻合 | T2（与 T3 **真并行**：T4 开发/测试用 fixture 视频文件，不依赖 T3 上传链路运行时；运行时接线在 T7） | M |
| **T5** | 合规：gateway 预筛 + pipeline 匿名 lane 强化 | 新本地规则预筛 fn（纯 stdlib，给 adapter）；`src/pipeline/`：读 payload `anonymous_preview=true` 标记进匿名严格 lane（G3），**三条 skip 路径全 fail-closed**（总开关关/LLM 未启用未配置/llm 出错，AD-2 v2，C2）；近空转录 → 软拦截；needs_manual_review → blocked 映射回 record；**S2 Pass3 跳过**（preset 不需要画像，付费多模态调用） | **断言级**：总开关关 → 匿名任务拒绝（非 skipped）；LLM 未配置 → 拒绝；fail_closed 不读 env；登录 free 任务三路径行为不变（回归断言）；空转录矩阵；**Pass3 不跑为硬验收（不接受"核验结论记录"替代）**；合规结果回写 record | T2（与 T3/T4 并行） | M |
| **T6** | admission **薄 adapter**（非第二套 policy 真源，C5） | 新 `gateway/anonymous_preview_policy.py`：**只做** `anonymous_preview_admission.py` 契约的调用翻译（admission 调用 + `artifact_policy`→gate 映射，T8 消费；**独立可合并**，不动 T7 的 router 文件）；**不得新增/复写任何决策规则**——decision/duration/voice_strategy/artifact_policy 全部取自 admission 返回值 | mode 四分支 decision 矩阵；teaser>180 拒；import 黑名单（无 minimax/cosyvoice provider 模块）；**守卫断言：模块内无独立决策常量/分支，决策值与 admission 输出逐字段相等** | T2（可并行） | S |
| **T7** | session + router + upload/status/stream 骨架 | 新 `gateway/anonymous_session.py`（`require_anonymous_session`、cookie 属性 F17）；新 `gateway/anonymous_preview_api.py`（`APIRouter(prefix="/gateway/anonymous-preview")` 照 `user_voice_api.py`）；端点 `POST /upload`、`GET /{id}/status`（**实时代理 Job API 翻译状态**）、`GET /{id}/stream`（**经 Job API 代理字节流 + inline + Range**，AD-6）；`downloadable_keys.py` 显式分支；`main.py` 注册（catch-all 前） | flag 关 → 全端点 404；**admin 运行时开关挂全部端点、读失败默认关（F9）**；session 不匹配/TTL 过期拒；stream inline + Range 工作、无下载 URL | T3,T6 | M |
| **T8** | consent + create 编排 + job 所有权 + **e2e 冒烟（硬验收）** | 新 `gateway/anonymous_consent.py`（三件套）；`POST /create`：**仅 `status==READY_FOR_MODE` 且未过期（F6 硬门）**、双门（free tier env + 匿名 flag）、in-flight gate、sentinel owner + Job 行 + `is_anonymous_preview` 标记 + payload 含 `anonymous_preview=true`、白名单引用 T1 `ANONYMOUS_PREVIEW_PAYLOAD_SPEC`；**`job_terminal_mirror.py` 结算步 bypass（G2：跳 settle 不跳状态镜像）** | **断言级**：终态后 credit_ledger 零新行、free_service_quota 零消耗、**PG Job 行照常进终态**、orphan 不收编；**flag-on 本地 compose 全链冒烟：真上传→预览可播放→不可下载**（这是切片存在理由，不可选） | T4,T5,T7 | L |
| **T9** | TTL 清理 sweeper（两条生命周期） | (a) 合规审计 JSONL（仅 status/reason/hash，**无转录文本/媒体**）保留 30d；(b) block/reject **即删**源+teaser；通过路径 record/媒体 24h；**job 工作区清理走 Job API 既有删除面**（F16，不 gateway 直接 rm）；`anonymous_sessions`/`daily_usage` 过期行清理 | 不误删未过期；审计链 30d 可查；**发布前置条件：任何环境开 flag 前 T9 必须已合并** | T8 | M |
| **T10** | 前端面板 + 播放器 + admin UI | 替换 `anonymous-trial-launcher.tsx` 为可用面板（拖放/进度/状态，照 `TranslationForm.tsx:326-393` 上传模式）；播放器照 `hero-sample-player.tsx`；admin settings 页加 `anonymous_free_preview_enabled` 控件（手写渲染页，不自动出现）；`NEXT_PUBLIC` flag 关不渲染 | flag 关零渲染；**不渲染任何下载 UI/URL**（服务端不可下载断言在 T8）；零 R2 字样（既有守卫） | T8 | M |

**依赖图**：`T1 → T2 → {T3 ∥ T4 ∥ T5 ∥ T6} → T7 → T8 → {T9 ∥ T10}`。关键路径 6 跳。

**规模合计**：S×1 / M×6 / L×2，串行约 10–11 人日；T3/T4/T5/T6 三-四路并行 + T9/T10 并行，日历周期约 **7–8 天**。

## 7. 测试计划

- **契约回归**：7 个既有 APF 测试文件保持绿；新任务不得改契约模块语义。
- **单元**：T2 原子性/异常 fail-closed/cap 边界；T4 probe 失败矩阵 + 时长容差；T5 合规矩阵（local blocked / llm error+fail_closed / needs_manual_review / 空转录）；T6 admission 四 mode；consent strict-bool。
- **集成**：T3 上传→adapter→record（fake 注入）；T7 端点 gate 矩阵；T8 create 硬门 + 所有权/结算断言。
- **守卫（并入各 T，不单开守卫任务）**：create payload 字段白名单；匿名模块 import 黑名单（services.jobs / clone providers）；F18 import 烟测；前端零 R2。
- **E2E**：T8 flag-on 全链冒烟为**硬验收**；T10 再补带前端一次。
- 新模块覆盖率 ≥80%。

## 8. 风险与缓解（修正后）

| 风险 | 缓解 |
|---|---|
| 误触发 clone provider | `preset_mapping` 纯预设 dispatch；payload 白名单 + import 黑名单测试 |
| 合规双跑双倍付费 | AD-2：gateway 只预筛，重合规 pipeline 一次；matrix 测试 |
| pipeline 合规 fail-open（env 默认 False） | 匿名 lane 代码级强制 `llm_fail_closed=True`，断言不读 env |
| **合规总开关关闭 / LLM 未配置 → 整体静默 skip（process.py:10474/10515）** | 匿名 lane 三条 skip 路径全 fail-closed（AD-2 v2），断言级验收；登录 free 行为不变 |
| 终态结算误结算匿名 job（mirror 矩阵自动 settle） | `job_terminal_mirror` 结算步显式 bypass；禁止整函数 return（保状态镜像） |
| T6 演化成第二套 policy 真源 → admission 规则漂移 | 薄 adapter 约束 + 守卫断言决策值逐字段等于 admission 输出 |
| device key 错位 → 全站自 DoS 或 session 限额无落点 | AD-5 拍板 `raw_device_cookie := avt_anon token`、cap=1；契约测试钉死 |
| create 旁路合规/风控 | F6 硬门：仅 READY_FOR_MODE + 未过期 |
| 匿名 job 撞终态结算红线 / 变孤儿 | AD-7 sentinel owner + Job 行 + mirror 单一入口 + 断言级验收 |
| import 双命名空间 enum 失效 | F18 烟测进 T3 验收 |
| XFF 伪造刷 per-IP | 受信代理 IP 提取（auth_phone 版）+ 测试 |
| 带宽/磁盘白嫖（gate 前写盘） | 读 body 前预检 + 200MB + 流式截断 + 非 admit 即删 |
| 攻击者烧穿 global 500（DoS+成本拉满） | 已知预算承诺（§5）+ cap 命中告警 + admin 熔断开关 |
| 数百并发挤占付费任务 | in-flight gate ≥2 → 429 |
| IP hash 字典还原 | HMAC-SHA256 带密钥 |
| 审计链 24h 消失 / blocked 媒体留存 | 两条生命周期：审计 30d（无媒体/转录）、block 即删媒体 |
| 匿名内容在 job 工作区永久留存 | T9 走 Job API 删除面同生命周期清理 |
| gateway 直读 app 容器路径 404 | stream 经 Job API 代理字节流 |
| T9 未上线先开 flag → 无清理写盘面 | 发布前置条件明文：T9 合并前任何环境不得开 flag |

## 9. 发布顺序与回滚

每 T 合并即可部署（flag 关零影响）。**开 flag 前置条件**：T1–T9 全部合并 + HMAC 密钥已配 + §5 成本数字已确认。回滚三层：admin 运行时开关（秒级）→ 后端 env flag（重建 gateway）→ 前端 NEXT_PUBLIC（重建前端）。

## 10. 与支付轨的并行约定

文件级零交集（已验证）。本切片对 credit ledger 零写入，不碰 `credits_service.py`/`billing.py`/`pricing_schema.py`；`.env.example`/`docker-compose.yml`/`config.py`/`startup_checks.py` 遵守 append-only 约定，小步勤合 main。

## 11. 后续 backlog（本切片明确不做，正式化 Phase 3a 前回填）

1. **Express 档匿名预设预览**（C1，Phase 3a 另一半）：复用本切片全部基建——admission 已支持 `mode="express"`、daily_usage 已留 mode 维度、teaser/合规/stream 链路同构；增量 ≈ payload 改 express 非交互 + 前端档位呈现。
2. **≤720p 预览降分辨率**（主方案 Phase 0 拍板项，C4 风险接受的回填）。
3. **完整风控矩阵**（IP-/24 / UA / fingerprint，主方案 §14）。
4. **claim token 消费/登录绑定**（Phase 4）。
5. **方案选择漏斗 UI**（匿名态多档对比呈现，主方案 §9/§17）。
