# Phase 4.3a — Express 快捷版 CosyVoice 自动克隆 canary spec

**作者：** Claude (Opus 4.7 / 1M)
**版本：** v0.3.1（Codex 2026-05-28 四轮 review 通过 + P3×2 文案清理；可开 PR 实施）
**日期：** 2026-05-28
**Codex review 状态：** ✅ 四轮全部通过（P1×11 + P2×5 全闭合，P3×2 文案清理已落档；建议按 §12 阶段拆 PR：**第一 PR 限定在 gateway 层 D / D1 / E / E1 / E2，不动 pipeline**）
**变更摘要 v0 → v0.1：**
- §5.3 OSS 上传方案 C→D（gateway internal upload endpoint，pipeline 不依赖 boto3）
- §6.4 新增 is_temporary 隔离矩阵（list / count / match 函数行为变更）
- §7.3 新增 worker clone 成功但 register 失败时 best-effort `delete_voice` 孤儿清理
- §11 改口：不承诺"7 天自动清理"，sweeper 留 Phase 4.3b 单独 spec
- §3.1 / §9.1 / §8 加 P2 修正（consent fail-audit / register-smart PR 描述约束 / admin settings full-body save 保护）

**变更摘要 v0.1 → v0.2：**
- §1.1 G4 / §1.3 DoD / §11 / §13 / §17 / §附录 A 全文统一删除"7 天自动清理"承诺；`temporary_expires_at` 仅作为数据自描述 + Phase 4.3b sweeper 入选条件
- 新增 **§2.5 成本闸**：per-user 每日 Express auto-clone 次数上限 + per-user active temporary clone count 上限，admin 可调；闸不通过 → skip + 审计
- §5.3 / 新增 **§5.5 internal upload endpoint 安全合同**：鉴权 + size cap + content-type 白名单 + 日志脱敏 + 4 个 HTTP code 测试覆盖
- §6.3 / §6.4 全文 v0.1 残留的旧函数名（query_routing_metadata）→ `lookup_clone_voice_routing_metadata`（grep 确认真实函数名 `gateway/user_voice_service.py:285`）
- §3.1 / §6.3 consent 时间戳拆 `client_confirmed_at`（可选 audit hint）+ `server_confirmed_at`（必填，后端 `compute_job_policy` 生成）
- §6.3 register-smart 写入 `created_from="express_auto"`（已确认 endpoint line 1107 接受该字段，默认 `smart_auto`）+ §10.3 新增 backward-compat 守卫断言 Smart caller 仍落 `smart_auto`

**变更摘要 v0.2 → v0.3：**
- §9.1 audit JSON schema：`express_consent_at` → 拆为 `express_consent_server_at` + `express_consent_client_at`，与 §3.1.a 时间戳合同对齐
- §6.3 新增 **`add_user_voice` 临时字段更新合同**：新建 row + existing revive 两条路径都必须**显式覆盖** `is_temporary` / `temporary_expires_at`（与 Phase 4.1 routing 字段同 group，不走 `_set_if_empty`）；非 temporary 写入必须把 `temporary_expires_at` 清成 None（防 stale）；insert + existing update 两条路径都要测试覆盖
- 全文残留 `query_routing_metadata` 字面量（§6.4 与 §10.3 两处测试名）→ `lookup_clone_voice_routing_metadata` 统一
- 新增 **§2.6 Layer 顺序锁定表**：policy → allowlist → consent → **budget** → main speaker → sample extraction → OSS upload → worker clone → register；明确 budget gate 必须在 sample extraction / OSS / worker 之前发生（避免 fail 后才发现 cap 用尽）

**变更摘要 v0.3 → v0.3.1（Codex 四轮 P3 文案清理）：**
- §11 NG 表"原本计划 7 天后清理" → "**仅作为 Phase 4.3b sweeper 未来入选条件的元数据**"，消除最后一处可能被误读为 TTL 承诺的字面值
- 新增 **§10.6 守卫扫描边界**：明确"全文无旧字面量"类守卫只扫代码 + 活 spec 主体，跳过变更日志块（避免变更日志里的 `query_routing_metadata` / `express_consent_at` 历史说明被误伤）

**前置：** Phase 4.2 全套（A–F）已合并并部署到 US prod；最新 hotfix 0ba02c7 / 4d6f5696 / 58a72dc5 / ce825322 / afced068
**关联文档：**
- [Phase 4.2 主方案 §13 Phase 4.3 方向声明](2026-05-26-cosyvoice-clone-frontend-wiring-plan.md#13-phase-43-方向--快捷版自动克隆不进入-phase-42-实施)
- [Phase 4 go-live plan](2026-05-24-cosyvoice-phase4-go-live-plan.md)
- [GitNexus Smart 自动审核图](../graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md)
- [GitNexus Workflow Core 图](../graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md)
- [GitNexus CosyVoice / Mainland Worker 图](../graphs/GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md)

---

## §0 触发事件 & 当前事实证据

### 0.1 触发事件

用户在 2026-05-28 跑了一个真实的 Express 任务 `job_c7d1ba7b055b41188ce6a9585f7e5d45`，期望验证"快捷版能不能自动用 CosyVoice v3.5 flash 克隆音色"。实际结果：

```
JobRecord:
  service_mode = express
  voice_clone_enabled = False                            （gateway 硬编码）
  voice_strategy = "preset_mapping"
  smart_consent = None                                   （Express 不带 consent）

Segments (运行结束后):
  selected_voice = "longshuo_v3"                         （CosyVoice 官方预设）
  voice_id = None                                        （无任何克隆 voice）
  requires_worker = False                                （不走武汉 worker）
  worker_target_model = ""                               （无目标模型）
  tts_provider = "cosyvoice"
```

即：当前 Express 路径只做 CosyVoice 官方预设音色匹配（`longshuo_v3` 等），**没有任何**克隆调用。这是 Phase 4.2 §13 明确声明"不实施"的范围。

### 0.2 当前代码现状（grep 已确认）

- **Gateway** `gateway/job_intercept.py:441-462`：Express 分支硬编码 `voice_clone_enabled=False, voice_strategy="preset_mapping"`，**不**做 consent 校验（`validate_smart_consent` 只在 `service_mode=="smart"` 时跑，line 1046-1057）
- **Pipeline** `src/pipeline/process.py:3186-3190`：S2 阶段对非 `wait_for_review` 模式打印 `[S2] 快捷模式：跳过音色库查找和自动克隆，由下游自动匹配音色。`，`voice_id_a / voice_id_b` 留 `None`
- **Voice match** `src/services/tts/cosyvoice_voice_selector.py:80+`：下游基于 S2 speaker profile（gender / age / persona）从 `_BASE_MAP` 预设里挑 `longshuo_v3 / longanyang / longanwen_v3` 等
- **Smart 路径**（`process.py:3640-4100`）已有完整的自动 clone 闭环（consent + admin + sample extractor + provider + register-smart mirror + UsageMeter），**但 provider 写死 MiniMax**（`_MiniMaxCloneAdapter` in `src/services/smart_wiring.py`）

### 0.3 Phase 4.3a vs Phase 4.3 完整版的范围切分

| 维度 | Phase 4.3a（本 spec） | Phase 4.3（未来阶段） |
|---|---|---|
| Service mode 覆盖 | **仅 Express** | Express + Smart 都加 CosyVoice 选项 |
| Provider 选择 | **CosyVoice 唯一**（固定 v3.5-flash） | MiniMax vs CosyVoice 二选一（admin 配 default） |
| Speaker 覆盖 | **仅 main speaker（占比最高者）** | All main speakers（≥10% 占比） |
| 灰度 | **canary：默认 `false` + admin allowlist** | GA flag（所有用户） |
| Consent | **任务级一次性勾选**（Express 提交时） | 与 Phase 4.3a 一致 |
| UX 入口 | 提交页新增"自动克隆主说话人音色（实验性）" checkbox | 与 Phase 4.3a 一致 |

**为什么先做 a 而不是直接做完整 4.3：**

1. Express 比 Smart 简单——无 eligibility gate、无可能匹配自动复用、无 handoff 状态机。先在 Express 验证"自动 clone CosyVoice"的全链路，再把它推广到 Smart 时风险更低
2. canary 模式（默认 off + 显式 allowlist）让我们能在生产实测 1-2 周，期间出问题影响范围限定在内部用户
3. Phase 4.2 §13 已经明确把"快捷版自动克隆"列为独立后续阶段，不进 Phase 4.2 主线

---

## §1 目标 / 非目标

### 1.1 目标 G

- **G1**：Express 任务在用户**显式勾选** consent 后，自动从主说话人选 10–20s 样本，调武汉 worker 克隆出 `cosyvoice-v3.5-flash` 音色，把克隆 voice 注入到 pipeline segments
- **G2**：克隆产出的 `user_voices` 行带完整 routing 元数据（`requires_worker=True, target_model="cosyvoice-v3.5-flash", region_constraint="mainland_only", clone_api_model="cosyvoice-v3.5-flash"`），下游 TTS 自动走 `_generate_one_cosyvoice_via_worker`
- **G3**：admin 后台有一个硬开关 `express_cosyvoice_auto_clone_enabled`，默认 `False`，配合 user allowlist 做 canary 灰度
- **G4**：克隆 voice 默认为**任务级临时音色**（`is_temporary=true`，标记 + 数据隔离），与 Phase 4.2 §12 保存策略对齐——用户没勾"保存到我的音色库"就不进长期库。`temporary_expires_at` 字段写入 `now+7d` **仅作为 sweeper 未来入选条件的元数据**，Phase 4.3a 阶段**不**承诺自动清理（详见 §11 NG11）
- **G5**：任何降级路径**必须**显式落审计 JSONL（`smart_decisions.jsonl` 同款），用户在工作台看到 `longshuo_v3` 时一定能在 audit 里查到"为什么没用克隆"
- **G6**：失败硬约束——不允许"自动克隆失败 → 静默回落付费的备选 clone provider"。回落只能去 **CosyVoice 预设音色匹配**（即当前行为）

### 1.2 非目标 NG（**重要**）

- **NG1**：**不动** Smart 的 MiniMax 自动 clone 路径（`process.py:3640-4100`）。Smart 仍然只走 MiniMax，与本 spec 完全独立
- **NG2**：**不动** Studio 手动 clone modal（Phase 4.2 已落地的 `CosyVoiceCloneModal`）
- **NG3**：**不接** MiniMax 作为 Express 自动 clone 的回落 provider。Phase 4.3a Express 自动 clone 失败 = 回到预设音色，**不**调任何其它付费 clone API
- **NG4**：**不做** auto-reuse / candidates / weak-match pause——Express 模式没有人工 review gate，匹配复用要等 Phase 4.3 全量
- **NG5**：**不做** 非主说话人的克隆。只克隆主说话人（占比最高一个 speaker），其余 speaker 仍走预设 voice matcher
- **NG6**：**不引入** 新的 pipeline → gateway HTTP 调用（除了已有的 `/api/internal/user-voices/register-smart`，下文 §6.5 会复用并轻量扩展）
- **NG7**：**不部署生产**。本 spec 只覆盖代码 + 测试 + admin allowlist 灰度方案，部署窗口由用户单独授权
- **NG8**：**不允许** `src/pipeline/*` import `gateway/*`（项目硬约束，Phase 4.1 D.7 守卫）。Clone 必须经 `src/services/mainland_worker/client.py` 的 worker client，**不**经 `gateway/cosyvoice_clone/api.py` 的 HTTP endpoint
- **NG9**：**不做** UI 上的 cost preview / 配额展示 UI。Phase 4.3a 是 canary，不展示给普通用户
- **NG10**：**不在 app 容器引入 boto3 / botocore 依赖**。app 当前 `pyproject.toml` 主依赖里没有它们（已 grep 确认）；引入会扩大镜像体积与 OSS secret 持有面。详见 §5.3 选 D。
- **NG11**：**不实现** `temporary_expires_at` 清理 sweeper（cron / scheduled task）。Phase 4.3a 写入字段是数据自描述（"该 voice 是临时的"），但不承诺自动清理；sweeper 留给 **Phase 4.3b 单独 spec**。详见 §11 与 §7.3 中关于孤儿清理的硬约束。

### 1.3 验收 DoD

部署灰度前必须全部通过：

1. ✅ 关 admin 开关 (`express_cosyvoice_auto_clone_enabled=False`) → 任何 Express 任务行为字节级不变（守卫 G6.1 验证）
2. ✅ 开 admin 开关 + 用户在 allowlist + 用户勾 consent + 通过成本闸 → 任务跑出来 `user_voices` 新行 `provider="cosyvoice_voice_clone", requires_worker=True, target_model="cosyvoice-v3.5-flash", is_temporary=true, temporary_expires_at=now+7d, created_from="express_auto"`（`temporary_expires_at` 仅是元数据，Phase 4.3a 不承诺自动清理）
3. ✅ 同上场景，segments 里 `voice_id=<clone>, requires_worker=True, worker_target_model="cosyvoice-v3.5-flash"`，TTS 阶段实际走 `_generate_one_cosyvoice_via_worker`
4. ✅ 开开关但用户**没在** allowlist → endpoint 403 / pipeline 内部直接跳过 → 段落仍走预设 `longshuo_v3` + 审计 JSONL 写 `reason_code=express_auto_clone_user_not_in_allowlist`
5. ✅ 用户在 allowlist 但**没勾** consent → 跳过克隆 → 审计 JSONL 写 `reason_code=express_auto_clone_consent_not_given`
6. ✅ 武汉 worker 503 / OSS 上传失败 / sample 抽取失败任一异常 → 不重试 → 回落预设音色 + 审计 JSONL 写明 `reason_code`
7. ✅ 主说话人占比 < 30% 或样本 < 10s → 跳过克隆 + 审计 JSONL（防糟糕样本浪费 DashScope 调用）
8. ✅ Smart 自动 clone 完全不受影响（守卫 G6.2 字节级验证）
9. ✅ Studio 人工 clone 完全不受影响（守卫 G6.3 字节级验证）
10. ✅ 所有 paid call（worker clone API）只在 **consent + allowlist + admin flag + sample 合格 + 主说话人识别成功** 5 条 AND 都满足时才发生，CI 守卫覆盖
11. ✅ **临时音色不污染"我的音色"**：GET `/gateway/user-voices` 返回的列表**不**含 `is_temporary=true` 的行；`count_active_voices_for_user_and_provider` 也不把临时音色计进 `cosyvoice_clone_max_voices_per_user` 配额；`match_user_voices` 不把临时音色纳入跨任务 auto-reuse 候选（守卫 §6.4）
12. ✅ **孤儿清理**：worker clone 成功但 `/register-smart` 写库失败 → 必须 best-effort 调 `MainlandWorkerClient.delete_voice(voice_id, ...)` 清理 DashScope 孤儿 voice，并写审计 JSONL `reason_code=register_failed_orphan_cleanup_*`（不阻塞 pipeline 继续走预设；详见 §7.3）
13. ✅ **consent 解析失败也写 audit**：`express_consent` 非 dict / 字段类型错误 → soft skip + 审计 JSONL 写 `reason_code=express_auto_clone_consent_invalid_{reason}`（与 "用户没勾" 不同的 reason_code，排障可区分）

---

## §2 触发条件（5 层 AND）

任意 1 层不满足 → 跳过克隆 + 走预设匹配 + 写审计 JSONL。

**Layer 1：admin 硬开关**
- `admin_settings.express_cosyvoice_auto_clone_enabled == True`
- 默认 `False`，**生产部署后保持 False**
- 通过 admin 后台 UI 翻 True（无需 container restart）

**Layer 2：worker 就绪**
- `services.mainland_worker.client_factory.is_worker_enabled_in_env() == True`
- 即 `AVT_MAINLAND_VOICE_WORKER_ENABLED=true` + url/key_id/secret 三件套全配
- 不就绪 = canary 环境压根没接通 worker，直接跳过

**Layer 3：用户在 allowlist**
- `admin_settings.express_cosyvoice_auto_clone_user_allowlist` 含当前 `user_id`（UUID 字符串）
- 灰度结束后改成 `general_availability` 开关（Phase 4.3 完整版）
- allowlist 空数组 = 没人能用，与 admin flag = False 等效（双保险）
- Admin 自身（`user.role == "admin"`）自动 bypass allowlist（方便自测）

**Layer 4：用户显式 consent**
- 提交 payload 含 `express_consent.auto_voice_clone == True` + `express_consent.confirmed_at` (ISO 8601)
- 见 §3 数据流
- consent 缺失或为 False → 跳过克隆

**Layer 5：主说话人 + 样本合格**
- 主说话人识别成功（pipeline 已有 `_smart_main_speaker_ids` 等价逻辑，Express 需补一个轻量版，见 §4）
- 主说话人占比 ≥ 30%（避免噪音 speaker 触发克隆；阈值见 §11）
- 抽样后 `validate_sample()` 返回 `duration_s >= 10.0`（与 Smart MiniMax 路径相同的 `MIN_SAMPLE_DURATION_SECONDS`）
- 上述任一不过 → 跳过

### 2.5 成本闸（v0.2 Codex 二轮 P1-2 新增）

**问题（Codex 二轮提出）**：§6.4 让临时音色不计入 `cosyvoice_clone_max_voices_per_user`（合理：避免临时音色占用用户长期音色库配额）。但**完全不计任何配额** = allowlist 用户每跑一次 Express 任务就触发一次付费 worker clone，理论上 1 分钟 1 次，每天 1000+ 次，DashScope 余额会被快速消耗。

**v0.2 处置**：临时音色**单独**一套成本闸，与长期库配额平行：

| 闸 | admin_settings 字段 | 默认值 | 实现位置 |
|---|---|---|---|
| **每用户每日 Express auto-clone 次数上限** | `express_cosyvoice_auto_clone_per_user_daily_cap` | `5` | gateway 查 `user_voices` WHERE `provider='cosyvoice_voice_clone' AND created_from='express_auto' AND created_at >= today_start UTC AND user_id=:user_id`（**不**过滤 `expired_at` 也**不**过滤 `is_temporary`——配额按"曾经发生过的克隆次数"算，避免软删了又跑） |
| **每用户 active temporary clone 上限** | `express_cosyvoice_auto_clone_per_user_active_temp_cap` | `3` | gateway 查 `user_voices` WHERE `provider='cosyvoice_voice_clone' AND is_temporary=true AND expired_at IS NULL AND user_id=:user_id`（active 临时音色数；多任务并发场景下防止用户开 10 个 Express 任务把临时表撑爆） |

**触发位置**：pipeline 进入 §5 worker clone 调用**之前**，与 Layer 1-5 同层，作为 Layer 6 / Layer 7：

- **Layer 6（daily cap）**：app pipeline 通过 internal endpoint 查 daily count → 超 cap 跳过 + 审计 `reason_code=express_auto_clone_daily_cap_reached_{count}_of_{cap}`
- **Layer 7（active temp cap）**：同上查 active temp count → 超 cap 跳过 + 审计 `reason_code=express_auto_clone_active_temp_cap_reached_{count}_of_{cap}`

**为什么用 internal endpoint 不在 pipeline 直查 DB**：pipeline 不应该持有 DB 连接（D.7 关注点分离，避免在 app 容器装 SQLAlchemy ORM 依赖）。新增 internal endpoint：

```
GET /api/internal/express-auto-clone-budget?user_id=<uuid>
Auth: X-Internal-Key
Response 200:
  {
    "ok": true,
    "daily_count": 2,
    "daily_cap": 5,
    "daily_remaining": 3,
    "active_temp_count": 1,
    "active_temp_cap": 3,
    "active_temp_remaining": 2,
    "can_clone": true,
    "deny_reason": null  // or "daily_cap_reached" / "active_temp_cap_reached"
  }
```

调用方（pipeline）只读 `can_clone` + `deny_reason` 决策。`daily_count` / `active_temp_count` 写进 audit JSONL 作为证据。

**admin 暴露**：admin UI / `/api/admin/users/{id}/express-auto-clone-budget` 可查任意用户当前 budget 状态（便于排障/调整 allowlist 时预判）。Phase 4.3a 不引入 admin UI 表格展示，只 endpoint。

**与 Studio cosyvoice_clone quota 的区别**：

- Studio 手动 clone 走 `cosyvoice_clone_max_voices_per_user`（默认 3）→ 用户音色库总数上限（v0.1 §6.4 已经让临时音色**不**计入此处）
- Express 自动 clone 走**两条独立闸**（daily count + active temp count）→ 防止 canary 期内 allowlist 用户成本失控

**守卫测试**（§10 追加）：

- `test_layer_6_daily_cap_blocks_after_quota_reached` — fixture: today 已发生 5 次 express_auto clone → 第 6 次必须跳过，audit `daily_cap_reached`
- `test_layer_7_active_temp_cap_blocks_when_3_temp_voices_alive` — fixture: 用户有 3 个 active temp voice → 新 Express 任务必须跳过，audit `active_temp_cap_reached`
- `test_studio_manual_clone_quota_unchanged_by_phase43a` — Studio 手动 clone 配额逻辑不被影响

### 2.6 Layer 执行顺序锁定（v0.3 Codex 三轮 P2-4）

**问题（Codex 三轮提出）**：v0.2 把成本闸定义为 Layer 6 / Layer 7，但 spec 行文没明确"Budget 必须在 sample extraction / OSS upload / worker clone 之前发生"。如果实施者把 budget check 放到 worker call 之后（甚至 OSS upload 之后），即使 cap 已用尽也会先做无用功，浪费 OSS PUT 流量 + sample extraction CPU，排障时还得分清"是 budget block 还是 OSS / worker fail"。

**v0.3 锁定**——所有 7 个 Layer 必须按下表顺序执行，任一 fail → 立刻跳过、写 audit、回预设音色：

| Layer | 检查内容 | Fail 后跳过的后续动作 | 资源消耗（fail 时） | 实现位置 |
|---|---|---|---|---|
| **L1 admin flag** | `express_cosyvoice_auto_clone_enabled == True` | L2-L7 + main speaker + sample + OSS + worker + register | 0（纯 admin_setting read） | `run_express_auto_clone` 入口 |
| **L2 worker env** | `is_worker_enabled_in_env() == True` | L3-L7 + 所有 paid 路径 | 0（env read） | 入口 |
| **L3 allowlist** | `user_id ∈ allowlist OR user.role == "admin"` | L4-L7 + paid | 0（list 比对） | 入口 |
| **L4 consent** | `record.express_consent.auto_voice_clone == True AND parse_error is None` | L5-L7 + paid | 0（record read） | 入口 |
| **L5 budget**（v0.2 §2.5）| `GET /api/internal/express-auto-clone-budget` 返 `can_clone=true` | L6-L7 + sample + OSS + worker + register | **1 次 HTTP GET 到 gateway**（内网，~5ms） | 入口附近 |
| **L6 main speaker** | `identify_express_main_speaker(...) is not None` | sample + OSS + worker + register | 0（pure function over lines） | 入口附近 |
| **L7 sample 合格** | `extract_sample(...) → validate_sample(...).duration_s >= 10` | OSS + worker + register | **几秒 CPU + ffmpeg subprocess + tmp 文件**（可接受，sample 抽取本身就是 pipeline 内部产物） | 入口附近 |
| **OSS upload**（不是 gate，是付费动作）| `POST /api/internal/cosyvoice/express-sample-upload` | worker + register | **OSS PUT 1 次（~640KB）+ 内网 HTTP**（~10-30ms，~¥0.0001） | `run_express_auto_clone` 主路径 |
| **Worker clone**（付费）| `client.clone(...)` | register | **DashScope API 1 次（~¥0.01-0.03）+ 跨境 WG 隧道**（~1-3s） | 主路径 |
| **Register**（落库）| `POST /api/internal/user-voices/register-smart` | （若失败走 §7.3 孤儿清理） | DB 写入 / 失败时 + delete_voice 1 次 | 主路径 |

**关键不变量**：

- **L1-L7 必须按编号顺序执行**——前一层 fail 直接跳过后续所有 Layer，不可乱序（守卫 §10 AST 扫调用点顺序）
- **L1-L5 全是零资源消耗的 gate**（admin/env/list/dict read + 1 次内网 HTTP），fail 不浪费任何 paid 资源
- **L6 main speaker 是 pure function over transcript lines**（已经在内存），fail 也零成本
- **L7 sample extraction 消耗几秒 CPU + tmp 文件**，是允许的"可控浪费"（sample 抽取本身就是非付费的，且 fail 频率应该很低；如果上线后发现这一层 fail 率 > 5% 再考虑前移）
- **OSS upload / worker clone / register 之间没有 gate**——一旦 L1-L7 全通过 + 主说话人识别 OK + 样本 OK，就直接进入付费路径
- **`server_confirmed_at` 在 L4 时（gateway compute_job_policy）已生成**，pipeline L5 之后任何路径都用 record 里的 server_confirmed_at，**永远不**重新生成

**守卫**（§10 追加）：

- `test_layer_order_l1_to_l7_in_run_express_auto_clone` — AST 扫 `services/express/auto_clone.py::run_express_auto_clone`，断言 7 个 if-block 按 L1→L7 顺序出现（用注释 `# Layer N:` 锚定）
- `test_budget_gate_runs_before_sample_extraction` — fixture: 让 budget endpoint 返 `can_clone=false`，断言 `VoiceSampleExtractor.extract_sample` **不**被调用
- `test_budget_gate_runs_before_oss_upload` — 同上，断言 `POST /api/internal/cosyvoice/express-sample-upload` 不被调用
- `test_budget_gate_runs_before_worker_clone` — 同上，断言 `client.clone(...)` 不被调用
- `test_sample_validate_failure_skips_oss_and_worker` — fixture: `validate_sample` 返 `duration_s=8.0`，断言 OSS endpoint + worker 都不被调用

---

## §3 Consent 数据流

### 3.1 frontend 提交 payload schema 变更

**当前**（Express 提交 payload 大概形状）：
```json
{
  "service_mode": "express",
  "url": "...",
  "translation_config": {...}
  // 不带 consent
}
```

**Phase 4.3a 新增**（必填，但只在 admin 开关 + allowlist 双通过时前端才显示对应 UI；后端无条件接受）：
```json
{
  "service_mode": "express",
  "express_consent": {
    "auto_voice_clone": true,
    "client_confirmed_at": "2026-05-28T03:45:21.123Z"   // 可选，仅作辅助审计
  }
}
```

- **`auto_voice_clone`** (bool, 必填)：用户明确勾选 = `true`；未勾或没显示 UI = `false`
- **`client_confirmed_at`** (ISO 8601 UTC string, 可选)：前端在用户勾选时刻打的时间戳；**仅作辅助审计**，**不**用作 worker / audit 的关键 consent 时间

### 3.1.a 服务端 consent 时间戳（v0.2 Codex 二轮 P1-5 修订）

**问题（Codex 二轮提出）**：v0.1 只传前端 `confirmed_at` 给 worker `WorkerCloneConsent.confirmed_at` 和写进 audit。恶意客户端可以伪造确认时间（"2020 年就同意了" / "未来时间"），让审计链路上的关键 consent 时间不可信。

**v0.2 处置**：consent 时间戳拆 client / server，**两个并存**：

| 字段 | 来源 | 用途 | 可信度 |
|---|---|---|---|
| `express_consent.client_confirmed_at` | 前端 / 用户 | 辅助审计 | **不可信**（可伪造） |
| `express_consent.server_confirmed_at` | **gateway `compute_job_policy` 落 record 时由后端生成** `datetime.now(timezone.utc).isoformat()` | worker `WorkerCloneConsent.confirmed_at` + audit JSONL 关键时间 + DashScope 端 created_at 对账 | **可信** |

**Gateway 改造**（C 阶段 `compute_job_policy`）：

```python
# gateway/job_intercept.py 落地 record 时
parsed_consent, parse_error = validate_express_consent(
    request_data.get("express_consent")
)
if parsed_consent and parsed_consent.get("auto_voice_clone"):
    parsed_consent["server_confirmed_at"] = datetime.now(timezone.utc).isoformat()
record.express_consent = parsed_consent
record.express_consent_parse_error = parse_error
```

**Pipeline 改造**（F 阶段 `run_express_auto_clone`）：

```python
# 永远用 server_confirmed_at，绝不读 client_confirmed_at
consent_for_worker = WorkerCloneConsent(
    voice_clone_confirmed=True,
    confirmed_at=express_consent["server_confirmed_at"],  # ← 后端生成
)
audit_record["express_consent_server_at"] = express_consent["server_confirmed_at"]
audit_record["express_consent_client_at"] = express_consent.get("client_confirmed_at") or None
```

**守卫**：

- `test_express_consent_server_confirmed_at_is_set_by_gateway` — 提交不带 `client_confirmed_at` 的 payload，断言 record / audit 仍有 server_confirmed_at
- `test_express_consent_worker_call_uses_server_not_client_at` — fixture: client_confirmed_at="2020-01-01"，断言 worker request 的 confirmed_at 字段是 now 附近的时间（不是 2020-01-01）
- `test_express_consent_audit_has_both_timestamps_when_present` — 同时含 client + server

**Layer 0 后端校验**（gateway 侧，在 `compute_job_policy` 落地后立即校验）：

```python
# gateway/express_consent.py（新文件）
def validate_express_consent(raw: object) -> tuple[dict | None, str | None]:
    """Express 版 consent 校验。比 smart_consent 弱：只看 auto_voice_clone bool。
    缺 / 不是 dict / auto_voice_clone 不是 bool → 返 (None, reason)，让 caller
    决定是否当 hard fail（current spec: soft skip，把 consent 当成 False 处理）。
    """
    if not isinstance(raw, dict):
        return None, "express_consent_missing_or_invalid_type"
    if "auto_voice_clone" not in raw:
        return {"auto_voice_clone": False}, None  # 未声明 = False，不 fail
    if not isinstance(raw["auto_voice_clone"], bool):
        return None, "auto_voice_clone_not_bool"
    if raw["auto_voice_clone"] and not isinstance(raw.get("confirmed_at"), str):
        return None, "confirmed_at_required_when_auto_voice_clone_true"
    return {
        "auto_voice_clone": bool(raw["auto_voice_clone"]),
        "confirmed_at": raw.get("confirmed_at") or "",
    }, None
```

**Smart_consent 不同点 / 为什么不复用**：
- Smart consent 6 字段（含 `prefer_official_voice_match / on_budget_exhausted` 等），Phase 4.3a 不需要这些
- Smart consent 校验失败是 hard fail（任务 reject）；Express 校验失败是 soft skip（按未勾处理，任务继续，回到预设音色）。Express 是快捷版，不该因为 consent 解析错把用户的任务整个 fail
- 守卫禁止 Express 走 `validate_smart_consent` 函数（避免漂移）

**P2 修正（Codex 2026-05-28 v0.1）—— consent 解析失败必须写 audit**：

soft skip 安全（不会触发付费 API），但**必须**在 audit JSONL 写明 reason 否则排障困难。`compute_job_policy` 落 record 时必须捕获 `validate_express_consent` 的 `(None, reason)` 分支并把 reason 同步到 JobRecord：

```python
# gateway/job_intercept.py 落地 record 时
consent_ok, consent_reason = validate_express_consent(
    request_data.get("express_consent")
)
record.express_consent = consent_ok  # 可能为 None
record.express_consent_parse_error = consent_reason  # 可能为 None
```

Pipeline 在 `run_express_auto_clone` 内部读 `record.express_consent_parse_error`：

- `parse_error != None` → `reason_code=express_auto_clone_consent_invalid_{parse_error}` 写 audit
- `parse_error == None` 且 `consent.auto_voice_clone == False` → `reason_code=express_auto_clone_consent_not_given` 写 audit
- `parse_error == None` 且 `consent.auto_voice_clone == True` → 进入下一层 gate

**为什么两个 reason_code 而不是合并**：排障时分得清"用户没勾" vs "前端传了错的 payload"。前者是产品决策，后者是 bug。

### 3.2 JobRecord 持久化

新增 JobRecord 字段：
```python
@dataclass
class JobRecord:
    ...
    express_consent: dict | None = None  # {"auto_voice_clone": bool, "confirmed_at": str}
```

- Snapshot 在 `gateway/job_intercept.py::compute_job_policy` 后立即写入
- 与 Smart 一样进 `JobRecord` JSON store + 后续 mirror 到 PG（如果走那条路）
- pipeline 从 record 读出，作为 §4 Layer 4 的判据

### 3.3 frontend UI

**`TranslationForm.tsx` 在 Express tab 下新增**（仅当 admin 开关 + allowlist 双通过时显示）：

```tsx
{expressAutoCloneAvailable ? (
  <label className="...">
    <input
      type="checkbox"
      checked={autoCloneConsent}
      onChange={(e) => setAutoCloneConsent(e.target.checked)}
    />
    <span>
      自动克隆主说话人音色（实验性）
      <Tooltip>
        勾选后将基于视频中占比最高的说话人的 10-20 秒音频片段自动克隆音色，
        生成的临时音色不会进入"我的音色库"。需要使用阿里云 DashScope CosyVoice v3.5 服务。
      </Tooltip>
    </span>
  </label>
) : null}
```

- 默认 unchecked
- `expressAutoCloneAvailable` 来源：调 `/api/auth/me` 或新加 `/api/internal/express-auto-clone-availability`，按 admin flag + allowlist 计算
- 不显示 = 等价于 unchecked

---

## §4 样本选择算法

### 4.1 复用 Smart 路径已有组件

`src/services/voice/sample_extractor.py::VoiceSampleExtractor` 已经实现：
- 按 speaker 提取所有 transcript lines
- 按 RMS / dBFS 过滤低音量段
- 按 contiguous range（gap ≤ 1.5s 合并）聚合
- ffmpeg 拼接出 16kHz mono s16 WAV，时长 cap 在 `[min_duration_s=10, max_duration_s=300]`
- `validate_sample()` 返回 `{duration_s, is_valid, silence_ratio, ...}`

**Phase 4.3a 直接复用，不重写**。理由：
- 已在生产跑了 1 年+，OOM-safe（subprocess ffmpeg）
- Smart 路径已经在用，行为字节级共享
- 守卫禁止 Express 路径自己实现新的样本算法

### 4.2 主说话人识别（轻量版）

Smart 用 `_smart_main_speaker_ids`（取自 `eligibility_gate.py`），Express 当前没这套。Phase 4.3a **不引入** eligibility_gate（那是 Smart 专属），改用一个**最小化**实现：

```python
# src/services/express/main_speaker.py（新文件，纯函数无副作用）
def identify_express_main_speaker(
    transcript_lines: list[TranscriptLine],
    *,
    min_ratio: float = 0.30,
    min_line_count: int = 5,
) -> str | None:
    """选主说话人：占比最高且占比 ≥ min_ratio 且至少 min_line_count 行。

    返回 speaker_id 或 None。None 表示不适合自动 clone（可能是单段噪音 / 
    平均分配的 N speaker）。
    """
    from collections import Counter
    counts = Counter(
        ln.speaker_id for ln in transcript_lines if getattr(ln, "speaker_id", "")
    )
    if not counts:
        return None
    total = sum(counts.values())
    top_speaker, top_count = counts.most_common(1)[0]
    if top_count < min_line_count:
        return None
    if top_count / total < min_ratio:
        return None
    return top_speaker
```

**为什么 30% 阈值**：
- 单人独白：top speaker 占比 > 90%
- 1 对 1 对话：top speaker 50-65%
- 群聊 / 多人会议：可能 25-40%，30% 是合理切点

**为什么 5 行 line floor**：
- 防止极短视频 / 单句噪音段触发克隆
- 5 行约对应 10-15s 真实说话（与 sample_extractor 的 10s min duration 兼容）

阈值在 admin_settings 暴露（可调），见 §7。

### 4.3 抽样流程

```
identify_express_main_speaker(transcript_lines)
  ↓ speaker_id != None
VoiceSampleExtractor().extract_sample(
    audio_path=source_audio_path,
    speaker_lines=[ln for ln in lines if ln.speaker_id == speaker_id],
    output_path=<project_dir>/express_clone_samples/<speaker_id>.wav,
    min_duration_s=10.0,
    max_duration_s=20.0,           # ← Phase 4.3a cap 在 20s（不 300s）
)
  ↓ no exception
validate_sample(output_path)
  ↓ duration_s >= 10
进入 clone 调用
```

**为什么 20s cap（不 300s）**：
- CosyVoice v3.5-flash 推荐 10-20s prompt
- 节省 OSS PUT 流量
- 抽样越长越可能掺杂低质段；10-20s 已经够 DashScope 用

---

## §5 Clone 调用位置（Pipeline 内）

### 5.1 调用位置：`process.py` S2 阶段之后、translation 之前

参照 Smart 路径的位置（`process.py:3640-4100`），但**不**进 Smart 分支，而是新加一个 Express 子分支。位置：

```
process.py 现有结构（粗化）:

S0: download / ingest
S1: ASR
S2: transcript review (pass 1 / 2 / 3) ← Phase 4.3a 在这里之后
   ↓
[NEW] Express auto-clone gate
   ↓
   if all 5 layers AND:
       ├── identify main speaker
       ├── extract sample
       ├── upload to OSS (via shared uploader)
       ├── call worker.clone()
       ├── register in user_voices (via gateway /register-smart with cosyvoice fields)
       ├── inject voice_id + routing into _speaker_voices[main_speaker_id]
       └── write audit JSONL
   else:
       ├── skip clone
       ├── write audit JSONL with reason_code
       └── leave _speaker_voices unchanged → downstream uses preset matcher
   ↓
S3: translation
S4: TTS (sees voice_id + requires_worker=True → routes to worker)
S5: alignment
...
```

**为什么这个位置**：
- 必须在 transcript review 之后（需要 speaker assignment 才能挑主说话人）
- 必须在 translation 之前（translator 接收 `voice_id_a / voice_id_b`，需要在 S3 之前敲定）
- TTS 在 S4，那时 `requires_worker=True` 的段会自动 dispatch 到 `_generate_one_cosyvoice_via_worker`，无需再改 TTS 代码

### 5.2 模块组织（避免 process.py 继续膨胀）

新增专门的 helper 模块：

```
src/services/express/
├── __init__.py
├── main_speaker.py         # identify_express_main_speaker()（§4.2）
├── auto_clone.py           # run_express_auto_clone()——本 spec 的核心入口
└── audit.py                # emit_express_clone_audit()——审计 JSONL emitter
```

`run_express_auto_clone` 函数签名：

```python
def run_express_auto_clone(
    *,
    user_id: str,
    job_id: str,
    project_dir: Path,
    source_audio_path: Path,
    transcript_lines: list[TranscriptLine],
    speaker_voices: dict[str, str],  # 输入：当前 _speaker_voices（被修改）
    speaker_routing: dict[str, dict[str, object]],  # 输入：当前 routing snapshot（被修改）
    express_consent: dict | None,
    usage_meter: UsageMeter | None,
) -> tuple[bool, str]:
    """如果触发条件全满足且 clone 成功，原地修改 speaker_voices / speaker_routing。

    返回 (clone_did_run, reason_code):
        clone_did_run=True, reason="success_voice_id={...}"  — 实际 clone 成功
        clone_did_run=False, reason="<具体原因>"           — 跳过 / 失败原因（写审计）
    """
```

**为什么单独模块**：
- `process.py` 已经 8000+ 行，再往里加 200 行 Express auto-clone 会失控
- 单独模块便于单元测试（不需要起整个 pipeline）
- 与 Smart 路径并列（`src/services/smart/`），命名风格一致
- 守卫禁止 `src/services/express/` import `gateway/`（保持 D.7 不变性）

### 5.3 OSS 上传：通过 gateway internal endpoint 间接上传（v0.1 Codex P1 fix）

**问题**：sample upload 当前的实现在 `gateway/cosyvoice_clone/sample_uploader.py`（boto3 PUT + sha256 + presigned GET URL TTL=120s），pipeline 不能 import gateway。

**v0 原方案 C 致命缺陷（Codex 2026-05-28 P1-1）**：原方案让 pipeline 在 `src/services/express/auto_clone.py` 里直接调 `boto3.client("s3").put_object(...)`。**但 grep 已确认** `pyproject.toml` 主依赖（line 6-15）里**没有** `boto3 / botocore`——它们只在 `gateway/requirements.txt`（独立容器）。app 容器跑这段会 `ModuleNotFoundError: No module named 'boto3'`。

**候选重新评估**：

| 方案 | 优点 | 缺点 | 评估 |
|---|---|---|---|
| **A**：把 `sample_uploader.py` 抽到 `src/services/cosyvoice_uploader/`，gateway 改 import | 复用最干净 | 改两个容器；改动面 + 回归面大；OSS secret 持有面从 gateway 扩到 app 容器（多了一份 env 持有点） | ❌ Phase 4.3a 范围不该这么大 |
| **B**：加 `boto3` 到 app 依赖 + lazy import | 直接照搬 v0 方案 C | 增加 ~15 MB 镜像 + OSS secret 持有面扩大 + 把 Phase 2 R2 / Pan 用的 boto3 path 暴露到 app（违反 D.7 关注点分离） | ❌ |
| **D** ✅：**新增 gateway internal endpoint** `POST /api/internal/cosyvoice/express-sample-upload`，body 是 sample bytes（multipart），response 含 `oss_presigned_get_url + sha256`，TTL 与 Phase 4.2 一致（120s）。Pipeline 用 `requests` 调（与 `_register_smart_clone_in_user_voices` 走同一 HTTP-internal 链路） | 改动最小：app 不引新依赖；OSS secret 仅 gateway 持有（与现状一致）；逻辑完全复用 `sample_uploader.py`；与 Phase 4.2 同套 endpoint 风格 | 多一次 app→gateway 的 HTTP hop（gateway 与 app 同 docker network 内网，延迟 < 5ms 可忽略） | ✅ |

**选 D**。理由：
- Phase 4.3a 是 canary，应最小改动
- D.7 关注点分离：OSS secret / boto3 调用集中在 gateway 容器
- 复用 sample_uploader 已经在生产稳定 1 个月+ 的代码（无需重新验证 sha256 / TTL / OSS error handling）
- pipeline 仅用 `requests`（已在 `pyproject.toml:11`），与现有 `register_smart_clone_in_user_voices` 走同套 `X-Internal-Key` 鉴权

**v0.1 新增 gateway endpoint 契约**：

```
POST /api/internal/cosyvoice/express-sample-upload
Auth: X-Internal-Key only
Body: multipart/form-data
  sample: UploadFile (audio/wav, 16kHz mono s16, ≤2MB)
  user_id: form field (str)
  job_id: form field (str)
  speaker_id: form field (str)
Response 200:
  {
    "ok": true,
    "presigned_get_url": "https://...",  // TTL=120s
    "sha256": "<hex>",
    "expires_at": "<ISO 8601>",
    "object_key": "express_clones/{user_id}/{job_id}/{speaker_id}/{sha256}.wav"
  }
Response 503: {"ok": false, "error": {"code": "uploader_not_configured" | "uploader_runtime_error"}}
```

实现：直接调 `gateway.cosyvoice_clone.sample_uploader.build_sample_uploader_from_settings(...)` + `uploader.upload(sample_bytes, ...)`，复用所有 fail-closed 检查。

**为什么不直接复用现有 `POST /api/voice/cosyvoice/clone` endpoint**：那个 endpoint 自己也调武汉 worker（一站式 upload + clone）。Phase 4.3a 需要 pipeline 自己控制 upload 与 worker call 之间的时序（中间还要做主说话人识别 / sample 抽取），不适合让 endpoint 全包。

**OSS 凭证管理**：依然在 gateway `.env` 里，env 同套 `AVT_COSYVOICE_OSS_ENDPOINT/BUCKET/ACCESS_KEY_ID/ACCESS_KEY_SECRET`。app 容器**不需要**任何 OSS env。守卫：app 容器启动 `startup_checks` **不**做 OSS env 校验（避免误把 OSS 依赖渗到 app 容器）。

**Phase 4.3 完整版**可以考虑方案 A（抽公共到 src/services/cosyvoice_uploader/），把多次 HTTP hop 简化掉。

### 5.5 Internal upload endpoint 安全合同（v0.2 Codex 二轮 P1-3 新增）

新增 `POST /api/internal/cosyvoice/express-sample-upload` 是**新的内部攻击面**，Phase 4.3a 必须在 endpoint 落地时一并锁死安全合同，否则后续维护者可能逐步放松。

#### 5.5.1 鉴权

- **必须** 通过 `X-Internal-Key` 鉴权（与 `register-smart` 同套 `AVT_INTERNAL_API_KEY` 校验）
- **不接受** session cookie / Bearer token / 任何用户级凭证（pipeline 子进程持有 internal key，用户浏览器不持有）
- 鉴权失败 → 401 `{"ok": false, "error": {"code": "unauthorized"}}`
- 守卫：endpoint 路由必须挂在 `internal_router`（已有），不能挂在 public `router`

#### 5.5.2 输入校验白名单

| 字段 | 类型 | 约束 | 失败 HTTP |
|---|---|---|---|
| `sample` | UploadFile | content-type ∈ `{"audio/wav", "audio/x-wav", "audio/wave"}`；body bytes ≤ **2 MB**（与 §2 Layer 5 cap 一致） | 415（content-type）/ 413（size） |
| `user_id` | form str | UUID 格式（`uuid.UUID(str)` 解析成功） | 400 `invalid_user_id` |
| `job_id` | form str | 长度 1-64，匹配 `^[a-z0-9_]{1,64}$`（与 segment_id 同 regex） | 400 `invalid_job_id` |
| `speaker_id` | form str | 匹配 `^speaker_[a-z]{1,3}$` | 400 `invalid_speaker_id` |

任一不通过 → endpoint 返对应 HTTP，**不**进入 `uploader.upload()`，**不**消耗 OSS PUT 流量。

#### 5.5.3 日志脱敏（hard requirement）

endpoint 内任何 `logger.info / warning / error / exception` 调用**绝不**出现下列字面值：

- presigned GET URL（含 query string 的 `X-Amz-Signature` / `OSSAccessKeyId`）
- OSS access key id 或 secret
- HMAC key / secret
- DashScope API key
- sample bytes 内容（hexdump / base64）

允许 log 的字段：`user_id`、`job_id`、`speaker_id`、`sha256[:16]`（截断 16 字符）、`object_key[:32]`（截断 32 字符）、HTTP status code、错误 code。

**守卫**：
- `test_express_sample_upload_endpoint_logs_redact_secrets` — 把 endpoint 跑在 caplog fixture 下，提交 happy path 请求 + 错误请求各一次，断言 caplog 文本不含 `X-Amz-Signature` / `OSSAccessKeyId` / OSS secret 等字面量 substring

#### 5.5.4 错误处理

| 场景 | HTTP | response body |
|---|---|---|
| 鉴权失败 | 401 | `{"ok": false, "error": {"code": "unauthorized"}}` |
| Content-Type 不在白名单 | 415 | `{"ok": false, "error": {"code": "unsupported_content_type", "expected": [...]}}` |
| Sample size > 2 MB | 413 | `{"ok": false, "error": {"code": "sample_too_large", "max_bytes": 2097152}}` |
| user_id / job_id / speaker_id 格式不对 | 400 | `{"ok": false, "error": {"code": "invalid_<field>"}}` |
| sample bytes 为空 | 400 | `{"ok": false, "error": {"code": "empty_sample"}}` |
| Uploader 未配置（缺 OSS env） | 503 | `{"ok": false, "error": {"code": "uploader_not_configured"}}` |
| Uploader 运行时错误（OSS 5xx / 网络） | 503 | `{"ok": false, "error": {"code": "uploader_runtime_error", "detail": "..."}}` |
| 成功 | 200 | 见 §5.3 schema |

**守卫**（§10 集成测试覆盖全 8 场景）：

- `test_express_sample_upload_401_no_internal_key`
- `test_express_sample_upload_401_wrong_internal_key`
- `test_express_sample_upload_415_unsupported_content_type`
- `test_express_sample_upload_413_oversize_body`（mock UploadFile 返 3 MB bytes）
- `test_express_sample_upload_400_invalid_user_id`
- `test_express_sample_upload_400_empty_sample`
- `test_express_sample_upload_503_uploader_not_configured`（mock build_sample_uploader_from_settings 返 None）
- `test_express_sample_upload_200_happy_path_returns_presigned_url`（mock uploader 返 fixed URL，断言 response 完整）

#### 5.5.5 与 Studio CosyVoice clone endpoint 的边界

Studio 的 `POST /api/voice/cosyvoice/clone`（Phase 4.2）也接受 sample 但**一站式**调武汉 worker。Express 不复用那个 endpoint：

- 那个 endpoint 含 consent / allowlist / clone-gate 等 5 层 fail-closed，与 Express 的 5 层 AND 部分重叠但语义不同（Studio 是 user-initiated session 请求，Express 是 pipeline 内部触发）
- 复用 = 把两条不同 trigger 路径耦合，未来 Studio 改了某层 gate 会意外影响 Express
- 新 endpoint 只做"上传样本拿 presigned URL"一件事，**不调** worker（worker 由 pipeline 在拿到 URL 后单独调）

**守卫**：
- `test_express_sample_upload_endpoint_does_not_call_mainland_worker` — endpoint 实现里不 import / 不调 `MainlandWorkerClient.clone`

### 5.4 Worker clone 调用

```python
# in src/services/express/auto_clone.py
from services.mainland_worker.client_factory import build_client_from_env
from services.mainland_worker.types import (
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerCloneConsent,
)

client = build_client_from_env()
if client is None:
    return False, "worker_unavailable_at_runtime"

try:
    with client:  # MainlandWorkerClient 支持 context manager
        response = client.clone(WorkerCloneRequest(
            job_id=job_id,
            user_id=user_id,
            speaker_id=main_speaker_id,
            speaker_name=main_speaker_display_name,
            target_model="cosyvoice-v3.5-flash",  # ← Phase 4.3a 硬编码
            sample=WorkerCloneSample(
                kind="download_url",
                url=oss_presigned_get_url,
                sha256=sample_sha256,
            ),
            source_segments=tuple(main_speaker_segment_indices),
            consent=WorkerCloneConsent(
                voice_clone_confirmed=True,
                confirmed_at=express_consent["confirmed_at"],
            ),
        ))
except WorkerError as exc:
    return False, f"worker_clone_failed_{exc.code}"
except WorkerNetworkError:
    return False, "worker_network_error"
```

注：`MainlandWorkerClient.clone` 的 max_attempts = 1（永不重试），与 CLAUDE.md 付费 API "无上限调用" 硬约束对齐（`client.py:249`）。

---

## §6 Routing 注入位置

### 6.1 segments 注入

Express 不走 voice_selection_review（`approved_voice_review` 永远为 None），所以 Studio 路径的 `_speaker_voice_routing` 注入点（`process.py:3313-3340`）**不适用**。

Phase 4.3a 直接修改 `process.py` S2 阶段后已经存在的 `_speaker_voices` dict：

```python
# in src/pipeline/process.py 调用点（伪代码）
if not approved_voice_review:
    # Express path
    print("[S2] 快捷模式：跳过音色库查找和自动克隆，由下游自动匹配音色。")
    
    # ← Phase 4.3a 新增 ↓
    from services.express.auto_clone import run_express_auto_clone
    _express_clone_did_run, _express_clone_reason = run_express_auto_clone(
        user_id=user_id,
        job_id=job_id,
        project_dir=final_project_dir,
        source_audio_path=source_audio_path,
        transcript_lines=transcript_result.lines,
        speaker_voices=_speaker_voices,        # 原地修改（如果成功克隆）
        speaker_routing=_speaker_voice_routing, # 原地修改
        express_consent=record.express_consent,
        usage_meter=usage_meter,
    )
    if _express_clone_did_run:
        voice_id_a = _speaker_voices.get("speaker_a") or voice_id_a
        voice_id_b = _speaker_voices.get("speaker_b") or voice_id_b
        print(
            f"[S2] Express 自动克隆成功 → "
            f"voice_id={_speaker_voices.get(main_speaker_id)}, "
            f"routing={_speaker_voice_routing.get(main_speaker_id)}"
        )
    else:
        print(f"[S2] Express 自动克隆跳过：{_express_clone_reason}")
    # ← Phase 4.3a 结束 ↑
```

### 6.2 segment-level routing 持久化

下游 `process.py:8097-8139`（commit 0ba02c7 修的那块）已经从 `_speaker_voice_routing` 把 `requires_worker / worker_target_model` 写回每个 segment。Phase 4.3a 不需要改这一段——只要把 routing 写进 `_speaker_voice_routing` dict，segments persistence 会自动接管。

**守卫**：`_speaker_voice_routing` dict 的 shape 必须保持 `{speaker_id: {requires_worker: bool, worker_target_model: str}}`，与 Studio 人工选 clone voice 的 schema 字节级一致（Phase 4.1 E.4 已锁定）。Phase 4.3a 不引入新字段。

### 6.3 user_voices 行注册

调 `POST /api/internal/user-voices/register-smart`（gateway/user_voice_api.py:1022）。复用现有 endpoint，但需要**轻量扩展**：

**当前 endpoint 已接受**（line 1079-1109）：
- `provider`（可 override，默认 `minimax_voice_clone`）→ 传 `cosyvoice_voice_clone`
- `tts_provider`（可 override）→ 传 `cosyvoice`
- `platform`（可 override）→ 传 `dashscope_mainland`
- `clone_sample_seconds / clone_sample_segment_ids / source_*`（已支持）

**当前 endpoint 不接受**（Phase 4.3a 需新增）：
- `region_constraint` → 传 `mainland_only`
- `requires_worker` → 传 `True`
- `target_model` → 传 `cosyvoice-v3.5-flash`
- `worker_provider` → 传 `cosyvoice`
- `worker_region` → 传 `cn-wuhan`
- `clone_api_model` → 传 `cosyvoice-v3.5-flash`
- `billing_sku` → 传 `cosyvoice_clone_v3_5_flash`
- `clone_provider_request_id` → 传 worker response 的 `provider_request_id`
- `clone_worker_request_id` → 传 worker response 的 `worker_request_id`
- `is_temporary` → 传 `True`
- `temporary_expires_at` → 传 `now + 7d` 作为元数据（Phase 4.2 §12 字段语义，**不**触发 Phase 4.3a 自动清理；详见 §11 NG11）

#### 6.3.1 `add_user_voice` 临时字段更新合同（v0.3 Codex 三轮 P1-2）

**问题（Codex 三轮提出）**：v0.2 说 `add_user_voice` 加 `is_temporary` / `temporary_expires_at` 两个 kwarg，但没明确两条调用路径（新建 row vs existing revive）的具体行为。当前函数（`gateway/user_voice_service.py:730-816`）已经有 existing 分支（line 738-776）和新建分支（line 778-816），且明确把字段分两组：

- **`_set_if_empty` group**（保留已有值，新值仅在原字段为空时写入）：`source_speaker_id / source_job_id / source_*` / `clone_sample_*` / `created_from`
- **直接覆盖 group**（每次 clone 重新填）：Phase 4.1 routing 字段 `region_constraint / requires_worker / target_model / worker_*` / `clone_*_request_id`

`is_temporary` / `temporary_expires_at` 必须进**第二组（直接覆盖）**，理由：

- 这两个字段表达"本次 clone 决定该 voice 是否临时"——不是"once-and-for-all"语义
- 用户 Phase 4.3a 跑了一次任务（写 `is_temporary=True`），后来在 Phase 4.3b UI 显式勾"保存到我的音色库"（升级为长期）→ 需要在 add_user_voice 路径覆盖为 `is_temporary=False` + `temporary_expires_at=None`，**不能**走 `_set_if_empty` 保留旧值
- 反向同理：如果某用户的长期音色在 Phase 4.4 / 未来被降级为临时（理论可能），也必须能覆盖

**v0.3 实施合同**（E 阶段必须按此写）：

```python
# gateway/user_voice_service.py::add_user_voice 内部
# v0.3 新增（与 Phase 4.1 routing 字段同 group）：
async def add_user_voice(
    db, *, user_id, voice_id, label, ...,
    # 已有 kwargs ...
    is_temporary: bool = False,                  # ← v0.3 新增
    temporary_expires_at: datetime | None = None,  # ← v0.3 新增
) -> UserVoice:
    ...
    if existing is not None:
        # existing revive 分支
        # ... 已有逻辑 ...
        # ---- v0.3 新增（与 routing 字段同 group，**直接覆盖**） ----
        existing.is_temporary = is_temporary
        # 关键：非 temporary 写入必须把 temporary_expires_at 清成 None（防 stale）
        existing.temporary_expires_at = (
            temporary_expires_at if is_temporary else None
        )
        await db.commit()
        return existing
    
    # 新建 row 分支
    voice = UserVoice(
        ...,
        is_temporary=is_temporary,
        temporary_expires_at=(temporary_expires_at if is_temporary else None),
    )
    ...
```

**非 temporary 写入清 stale 的规则**：

- `is_temporary=True` + `temporary_expires_at=<ts>` → 写入 `temporary_expires_at=<ts>`
- `is_temporary=True` + `temporary_expires_at=None`（caller 漏传）→ 写入 `temporary_expires_at=None`（让 sweeper 看到 None 时跳过，不至于误删；但同时 caller 是 bug）
- `is_temporary=False` + `temporary_expires_at=<任何值>` → **强制**写入 `temporary_expires_at=None`（防 caller 误传 + 防 existing row 残留旧 ts）

**守卫测试**（§10 追加）：

- `test_add_user_voice_insert_writes_is_temporary_true_and_expires_at` — 新行 `is_temporary=True` 路径
- `test_add_user_voice_insert_non_temp_keeps_expires_at_none` — 新行 `is_temporary=False` 路径，断言 `temporary_expires_at IS NULL`
- `test_add_user_voice_existing_revive_overwrites_is_temporary` — fixture: existing row `is_temporary=False`，新 caller 传 `is_temporary=True` → revive 后断言已变 True
- `test_add_user_voice_existing_revive_clears_stale_temporary_expires_at` — fixture: existing row `is_temporary=True, temporary_expires_at=<old ts>`，新 caller 传 `is_temporary=False, temporary_expires_at=None` → 断言 existing 行 `temporary_expires_at IS NULL`
- `test_add_user_voice_non_temp_forces_expires_at_none_even_if_caller_passes_ts` — 防御性测试：caller bug 同时传 `is_temporary=False` + `temporary_expires_at=<ts>`，断言 DB 行 `temporary_expires_at IS NULL`

**`created_from` 字段**（grep 已确认 endpoint line 1107 接受该字段，默认 `smart_auto`；ORM line 738 `created_from: String(32) nullable`）：

- Express 自动 clone → **必须**传 `created_from="express_auto"`
- Smart MiniMax 旧 caller（`process.py::_register_smart_clone_in_user_voices`）→ 不传 → endpoint 默认 `smart_auto`，行为字节级不变
- 守卫禁止 Express 路径漏传 `created_from` 字段：endpoint 收到 Express 的 routing 元字段（`requires_worker=True` + `provider="cosyvoice_voice_clone"`）但 `created_from` 落 `smart_auto` → 视为客户端 bug → 400 拒绝（**新约束**，避免审计混淆）

**Codex 二轮 P1-6 锁定的 endpoint 行为**：

```python
# gateway/user_voice_api.py::internal_register_smart_clone 内
provider = str(body.get("provider") or "minimax_voice_clone")
created_from = body.get("created_from") or "smart_auto"

# v0.2 新增防漂移：CosyVoice provider + smart_auto created_from 组合不合法
if (
    provider == "cosyvoice_voice_clone"
    and created_from == "smart_auto"
):
    return _json(400, {
        "error": "created_from_required_for_cosyvoice_clone",
        "detail": (
            "cosyvoice_voice_clone provider requires explicit created_from "
            "('express_auto' for Phase 4.3a, 'studio_manual' or "
            "'cosyvoice_clone_endpoint' for Studio paths)"
        ),
    })
```

**守卫**（§10.3）：

- `test_register_smart_smart_minimax_caller_defaults_to_smart_auto` — mock 现有 `_register_smart_clone_in_user_voices` 调用（MiniMax + 不传 created_from），断言写入行 `created_from='smart_auto'`
- `test_register_smart_express_caller_writes_express_auto` — mock Express auto-clone 调用，断言写入行 `created_from='express_auto'`
- `test_register_smart_cosyvoice_provider_without_explicit_created_from_rejected_400` — 故意构造 bug case（CosyVoice + 漏 created_from），断言 400
- `test_register_smart_smart_minimax_still_works_without_created_from` — backward compatibility hard guard

**Gateway 改动**（`gateway/user_voice_api.py::internal_register_smart_clone`）：
- 增加 12 个可选 body 字段的 pass-through
- 在调用 `add_user_voice(...)` 时把它们透传过去（`add_user_voice` 已支持 Phase 4.1 routing fields，line 716-725；只缺 `is_temporary / temporary_expires_at`）
- 顺带在 `add_user_voice` 加 `is_temporary / temporary_expires_at` 两个 kwarg（migration 031 列已经在 ORM，line 780/799）

**为什么不开一个新 endpoint**：
- `/register-smart` 是 internal-only（X-Internal-Key），新增可选字段对现有 Smart MiniMax caller 完全透明（不传就是 None / 默认）
- 守卫确保 Smart 调用方不会被新字段影响（pass-through，默认值与旧行为字节级相同）
- 开新 endpoint 会有"为什么有两套？"的运维迷惑

**Phase 4.3 完整版**可以考虑改名 `/register-smart` → `/register-auto-clone`（涵盖 Smart 和 Express），但 Phase 4.3a 不动名字。

### 6.4 `is_temporary` 隔离矩阵（v0.1 Codex P1-2 fix）

**问题（grep 已确认 2026-05-28）**：

- `gateway/user_voice_service.py::list_user_voices` (line 223-234) 只过滤 `expired_at IS NULL`，**不**看 `is_temporary`
- `GET /gateway/user-voices` (line 162-170) 直接调它 → 临时音色会进"我的音色"列表 UI
- `count_active_voices_for_user_and_provider` (line 237-258) 也只看 `expired_at IS NULL` → 临时音色撑爆 `cosyvoice_clone_max_voices_per_user` 配额
- `match_user_voices` (line 519+, 572, 618) 也只看 `expired_at IS NULL` → 临时音色被 Smart 跨任务 auto-reuse 误选

如果 Phase 4.3a 写出 `is_temporary=true` 的临时音色而不修这些函数，会让用户看到不该看到的 voice、占用配额、被其它任务复用。这与 Phase 4.2 §12 / 本 spec §1.1 G4 "默认不进长期库" 的承诺矛盾。

**变更矩阵**（按 case 决策，**不**一刀切，避免破坏 routing decision）：

| 函数 / endpoint | 当前过滤 | Phase 4.3a 期望 | 改法 |
|---|---|---|---|
| `list_user_voices` | `expired_at IS NULL` | **隐藏** `is_temporary=true` 行 | 加 kwarg `include_temporary: bool = False`；默认 False（隐藏） |
| `GET /gateway/user-voices` | 间接 | 同上 | 调用 `list_user_voices(...)` 默认 False，UI 不传 query param 时维持默认 |
| `count_active_voices_for_user_and_provider` | `expired_at IS NULL + provider` | **不计入** 临时音色 | 加 kwarg `include_temporary: bool = False`；CosyVoice clone-gate quota check 用默认值 |
| `match_user_voices` | `expired_at IS NULL` | **不参与** 跨任务 auto-reuse 候选 | 加 kwarg `include_temporary: bool = False`；Smart `_match_smart_user_voice` 用默认值 |
| `lookup_clone_voice_routing_metadata` (line 287+) | `expired_at IS NULL + cosyvoice_voice_clone + requires_worker=True + target_model 非空` | **必须包含** 临时音色（Phase 4.3a 写出来就是要给本任务 segments 走 worker 路径用） | **不动**——这函数返 voice_id → routing dict 映射，对 `is_temporary` 透明，segments 持久化 `requires_worker / worker_target_model` 仍正常 |

**为什么不一刀切**：

`lookup_clone_voice_routing_metadata` 是给 segments persistence 路径用的（commit 0ba02c7 修过），它需要"任何 active worker 路由音色"——临时音色就是本任务克隆出来要用的，**必须**能查到。但 `list_user_voices` 是给用户的 UI 用的，要隐藏。这两个语义不同，函数行为必须分别处理。

**v0.1 后端改动清单（追加 §12 D 阶段）**：

- `gateway/user_voice_service.py`：
  - `list_user_voices(db, user_id, *, include_expired=False, include_temporary=False)` ← 加新 kwarg
  - `count_active_voices_for_user_and_provider(db, user_id, *, provider, include_temporary=False)` ← 加新 kwarg
  - `match_user_voices(...)` 加 `include_temporary: bool = False` 参数；内部 `select` 子句加 `UserVoice.is_temporary.is_(False)`（当 include_temporary=False 时）
- `gateway/user_voice_api.py`:
  - `GET /gateway/user-voices` 不变（默认调用即可隐藏）
  - **若用户后续想看临时音色（Phase 4.3 完整版需求）**，再加 `?include_temporary=true` query param，本 Phase 4.3a **不**实现

**守卫测试新增**（§10.3）：

- `test_list_user_voices_default_hides_is_temporary_true` — fixture 注入 1 个 `is_temporary=True` + 1 个 `is_temporary=False` 行，断言 GET /user-voices 只返回后者
- `test_count_active_voices_default_excludes_temporary` — 同上，断言 count 函数只数非临时
- `test_match_user_voices_default_excludes_temporary_for_smart_auto_reuse` — 构造跨任务的临时音色，断言 Smart `_match_smart_user_voice` 拿不到
- `test_lookup_clone_voice_routing_metadata_includes_temporary` — 反向 sanity：临时音色 routing 仍然能查到（segments persistence 路径不破坏）
- `test_smart_existing_callers_pass_default_include_temporary_false` — AST 扫 Smart caller 在 process.py 调用点不传 `include_temporary=True`（保持隔离）

---

## §7 失败降级策略

### 7.1 降级决策表

| 失败位置 | 处置 | 审计 reason_code |
|---|---|---|
| Layer 1 admin flag = False | 跳过 clone | `express_auto_clone_admin_flag_off` |
| Layer 2 worker env 未配 | 跳过 clone | `express_auto_clone_worker_not_configured` |
| Layer 3 用户不在 allowlist | 跳过 clone | `express_auto_clone_user_not_in_allowlist` |
| Layer 4 用户没勾 consent | 跳过 clone | `express_auto_clone_consent_not_given` |
| Layer 5a 主说话人识别失败 | 跳过 clone | `express_auto_clone_no_main_speaker` |
| Layer 5b 主说话人占比 < 30% | 跳过 clone | `express_auto_clone_main_speaker_low_ratio_{ratio}` |
| Sample extract 抛异常 | 跳过 clone | `express_auto_clone_sample_extract_failed_{type}` |
| Sample duration < 10s | 跳过 clone | `express_auto_clone_sample_too_short_{seconds}` |
| OSS PUT 失败 | 跳过 clone | `express_auto_clone_oss_upload_failed_{code}` |
| Worker 503 / 502 / 网络错误 | 跳过 clone（**不重试**，CLAUDE.md 付费 API 硬约束） | `express_auto_clone_worker_unavailable_{code}` |
| Worker 业务错（4xx 含 quota） | 跳过 clone | `express_auto_clone_worker_business_error_{code}` |
| `/register-smart` 写库失败 | 见 §7.3：必须 best-effort `delete_voice` 清理 DashScope 孤儿（Codex v0.1 P1-3） | `express_auto_clone_register_failed_orphan_cleanup_{ok\|failed}` |

### 7.2 共同行为约束

- **不重试**：与 worker client `max_attempts=1` 对齐。任何失败 → 立即回落预设
- **不降级到 MiniMax**：违反 NG3
- **不阻塞 pipeline**：失败只跳过 clone 这一步，pipeline 继续往 S3 走，下游 voice matcher 选预设音色（用户能看到结果，只是不是克隆音色）
- **不静默扣点**：clone 失败时不写 UsageMeter clone 事件（与 Smart `_looks_like_quota_error` 等区分量配额错误的方式一致）
- **审计可查**：失败原因写 `audit/express_decisions.jsonl`（与 Smart 的 `smart_decisions.jsonl` 同款）

### 7.3 register 失败的孤儿清理（v0.1 Codex P1-3 fix）

worker 已经克隆成功 → DashScope 已经扣费 → 但 `/register-smart` 写库失败的场景：

**v0 错误处置**：v0 写"Phase 4.3a 不自动 cleanup，只发 admin notification"。Codex 驳回——这会让 DashScope 那边累积孤儿 voice，撑爆 quota 上限。`MainlandWorkerClient.delete_voice(...)` 已经在 client 里（`client.py:348-379`），用 `WorkerDeleteVoiceRequest(job_id, user_id, reason="register_failed")` 调即可，**幂等 + 最多 3 次重试**（`client.py:365` 的 `max_attempts=MAX_NETWORK_RETRIES`）。

**v0.1 处置**（hard requirement，不是 best-effort 模糊承诺）：

```
worker.clone() 返回 ok=True, voice_id="cosyvoice-v3.5-flash-..."
  ↓
post register-smart → HTTP error or ok=False
  ↓
**必须** 调 client.delete_voice(voice_id, WorkerDeleteVoiceRequest(
    job_id=job_id,
    user_id=user_id,
    reason="express_register_failed",
))
  ↓ delete 成功
audit JSONL 写：
  reason_code="express_auto_clone_register_failed_orphan_cleanup_ok"
  details: {register_error_detail, delete_voice_response.worker_request_id}
  ↓ delete 失败（连续 3 次失败）
audit JSONL 写：
  reason_code="express_auto_clone_register_failed_orphan_cleanup_failed"
  details: {register_error_detail, delete_voice_error}
emit admin notification（与 v0 一致）
```

**为什么强制 delete_voice 不算违反"不静默调付费 API"**：

`delete_voice` 是**幂等清理**操作，不是 fallback 路径。CLAUDE.md 硬约束禁止的是"用付费 API B 兜底付费 API A 的失败"（造成成本失控）。`delete_voice` 是**对自己刚刚创建的资源**做清理，本质是 commit-rollback 中的 rollback 分支，与 Pan backup 的 residue_cleanup / R2 sweeper 同性质——**未清理才是数据问题**。

**Pipeline 不能 import gateway 的约束**怎么满足：`MainlandWorkerClient.delete_voice` 在 `src/services/mainland_worker/client.py`（NOT gateway），pipeline 已经持有同一个 `client` 实例做 clone 调用，直接 `client.delete_voice(...)` 即可。

**幂等保证**：worker `/cosyvoice/voices/{voice_id}` DELETE 如果资源不存在返 200（与 PUT 一致）；client 重试 3 次是 idempotent 安全。

**审计字段对齐**：写 audit JSONL 时同时记 `worker_clone_response.worker_request_id`（首次 clone 调用） + `delete_voice_response.worker_request_id`（清理调用），两个 request_id 串起来给 admin 排障。

**Phase 4.3b sweeper 范围（未来）**：Phase 4.3a 仅做"register 失败"这一窄路径的 inline 清理。`temporary_expires_at` 到期的批量清理 sweeper（Phase 4.2 §12.4）留给 Phase 4.3b 单独 spec。本 Phase 4.3a **不**承诺 TTL 自动清理（详见 §11）。

---

## §8 Admin 开关 & Allowlist

### 8.1 新增 admin_settings 字段

`gateway/admin_settings.py` 在现有 `cosyvoice_clone_*` 字段（line 194-212）旁加：

```python
# Phase 4.3a — Express CosyVoice 自动克隆 canary
express_cosyvoice_auto_clone_enabled: StrictBool = False
express_cosyvoice_auto_clone_user_allowlist: list[str] = Field(default_factory=list)
express_cosyvoice_auto_clone_main_speaker_min_ratio: confloat(ge=0.10, le=1.0) = 0.30
express_cosyvoice_auto_clone_main_speaker_min_lines: conint(ge=1, le=100) = 5
express_cosyvoice_auto_clone_sample_max_seconds: confloat(ge=10.0, le=60.0) = 20.0
express_cosyvoice_auto_clone_target_model: str = "cosyvoice-v3.5-flash"
# v0.2 新增（§2.5 成本闸）
express_cosyvoice_auto_clone_per_user_daily_cap: conint(ge=0, le=1000) = 5
express_cosyvoice_auto_clone_per_user_active_temp_cap: conint(ge=0, le=100) = 3
```

`StrictBool` 模式与 `cosyvoice_clone_general_availability_enabled`（Phase 4.2 已落）一致。

### 8.2 admin UI 新增控件

`frontend-next/src/app/(app)/admin/settings/page.tsx` 新增 section "Express CosyVoice 自动克隆（canary）"：

- checkbox "启用 Express 自动克隆" → 写 `express_cosyvoice_auto_clone_enabled`
- textarea "Beta 用户白名单（user_id，每行一个 UUID）" → 解析为 `list[str]`
- 数字输入 "主说话人最小占比" + "主说话人最少行数" + "样本最大时长（秒）"
- 只读显示 `target_model = cosyvoice-v3.5-flash`（提示文字 "Phase 4.3a 固定，不可改"，对应 Phase 4.3 全量时再开下拉）

**v0.1 Codex P2 修正（D.1 教训）—— full-body save 守卫**：

`admin_settings.py` 用 **full-body save** 语义：前端提交 settings PATCH 时是把当前页所有字段一起写回 DB（不是 partial update）。Phase 4.1 D.1 出过事故——某个 admin UI tab 没暴露的字段在 save 时被丢弃，导致 cosyvoice 的某个隐藏 flag 被意外 reset 成 default。

Phase 4.3a 新增 **8** 个 `express_cosyvoice_auto_clone_*` 字段（v0.2 + 2 个成本闸 daily_cap / active_temp_cap）：

- `frontend-next/src/app/(app)/admin/settings/page.tsx::DEFAULT_SETTINGS` 必须**显式列**全部 8 个新字段及其 default（与 admin_settings.py 一致）
- "Reset to defaults" 按钮的 reset payload 必须**显式列**全部 8 个新字段（reset 后回到 `False / [] / 5 / 3` 等）
- save 请求体必须**显式包含**全部 8 个新字段，即使用户没动它们（避免 backend 把它们当 None 处理）
- Section UI 渲染顺序不影响 save payload 完整性（D.1 教训是"UI 没渲染 → save 不发"，必须改正：save 用 settings state，不用 UI 渲染状态）

**守卫**（§10 新增）：

- `test_admin_settings_default_includes_express_cosyvoice_auto_clone_fields` — AST 扫 page.tsx 的 `DEFAULT_SETTINGS`，断言含全部 6 个新字段
- `test_admin_settings_reset_payload_includes_new_fields` — AST 扫 reset button onClick，断言 reset 时 payload 含全部 6 个新字段
- `test_admin_settings_save_payload_completeness` — 集成测试模拟修改单个字段后 save，断言其余 cosyvoice / smart / phase 1b 字段在 payload 中保持原值（不被 None 覆盖）

### 8.3 pipeline app-safe reads

pipeline 子进程**不**能 import `gateway.admin_settings`（D.7 守卫），用 `services.admin_settings.read_admin_setting()` 读：

```python
# in src/services/express/auto_clone.py
from services.admin_settings import read_admin_setting

_admin_enabled = bool(read_admin_setting(
    "express_cosyvoice_auto_clone_enabled", default=False
))
_allowlist = list(read_admin_setting(
    "express_cosyvoice_auto_clone_user_allowlist", default=[]
))
_min_ratio = float(read_admin_setting(
    "express_cosyvoice_auto_clone_main_speaker_min_ratio", default=0.30
))
# 等
```

### 8.4 Express auto-clone availability endpoint

frontend 需要知道"当前用户能不能看到 consent checkbox"，新增轻量 endpoint：

```
GET /api/auth/me/express-auto-clone-availability
返回：
{
  "available": true|false,
  "reason": "admin_flag_off" | "not_in_allowlist" | "ok"
}
```

- 不需要返回 allowlist 内容（隐私）
- `available=true` ⇔ `admin_enabled AND (user in allowlist OR user.role == "admin")`
- 前端只用 boolean 决定渲染

---

## §9 审计日志

### 9.1 文件

`<project_dir>/audit/express_decisions.jsonl`

每个 Express 任务一行（无论 clone 跑没跑），结构：

```json
{
  "kind": "express_auto_clone_decision",
  "ts": "2026-05-28T03:45:21.123Z",
  "job_id": "job_...",
  "user_id": "<uuid>",
  "service_mode": "express",
  "phase_version": "4.3a",
  "decision": "skipped" | "cloned" | "register_failed_orphan_cleanup_ok" | "register_failed_orphan_cleanup_failed",
  "reason_code": "<§7.1 table>",
  "main_speaker_id": "speaker_a" | null,
  "main_speaker_line_count": 42,
  "main_speaker_ratio": 0.73,
  "sample_seconds": 14.2,
  "sample_segment_ids": [3, 5, 7, 11, 13],
  "voice_id": "cosyvoice-v3.5-flash-...",
  "worker_request_id": "...",
  "provider_request_id": "...",
  "is_temporary": true,
  "temporary_expires_at": "2026-06-04T03:45:21.123Z",
  "express_consent_server_at": "2026-05-28T03:44:51.345Z",
  "express_consent_client_at": "2026-05-28T03:44:50.000Z",
  "express_consent_parse_error": null | "auto_voice_clone_not_bool" | "confirmed_at_required_when_auto_voice_clone_true",
  "register_failure_detail": "<http_status>: <body excerpt>",
  "delete_voice_worker_request_id": "...",
  "delete_voice_error": null | "<error code>",
  "admin_settings_snapshot": {
    "express_cosyvoice_auto_clone_enabled": true,
    "express_cosyvoice_auto_clone_target_model": "cosyvoice-v3.5-flash",
    "express_cosyvoice_auto_clone_main_speaker_min_ratio": 0.30
  }
}
```

**v0.1 关键约束**（Codex P2-2）：

- **每个** Express 任务**必写一行** audit JSONL，无论 decision 是什么。"用户没勾 consent" / "consent 解析失败" 等场景必须写，不能因为"没扣费就不记录"——audit 是为排障，不是为计费
- `express_consent_parse_error` 字段在 consent dict 形态错误时填具体错误 code（与 `validate_express_consent` 的返回 `reason` 字面值对齐），让排障人员能快速分清"用户没勾" vs "前端 bug 传错"
- `register_failure_detail` 字段在 register-smart 失败时填具体错误（最多 200 字符），与 §7.3 配合
- `delete_voice_*` 两字段在 register 失败 + 走孤儿清理路径时填，反映 best-effort delete 的结果（§7.3）

**v0.3 关键约束**（Codex 三轮 P1-1）：

- `express_consent_server_at`：**服务端生成**（gateway `compute_job_policy` 落 record 时 `datetime.now(timezone.utc).isoformat()`），是 audit / worker / DashScope 对账的**可信**关键时间。永远存在（即使 consent.auto_voice_clone=False，consent payload 解析成功且 auto_voice_clone=True 时才生成；reason_code=consent_not_given 场景这字段为 `null`）
- `express_consent_client_at`：**前端传**的辅助审计字段（可选）。可能为 `null`（用户没勾）或被恶意客户端伪造的时间（不可信）。`null`-safe，仅作"客户端勾选时刻"参考
- v0.2 字段名 `express_consent_at` 在 v0.3 起**作废**——任何 audit / log 出现这个名字视为合规漂移，由 §10 守卫扫住

### 9.2 Job API 可访问性

- audit 文件路径与 Smart 的 `smart_decisions.jsonl` 类似，进 admin-only 审计区
- **不**通过 Job API 暴露给普通用户（包含 worker_request_id 等运维内部字段）
- admin 通过现有 `/api/admin/jobs/{job_id}/audit-files` 或类似入口拉取（若不存在则 spec 阶段不引入新 endpoint，等 Phase 4.3 完整版再做监控面板）

### 9.3 UsageMeter 集成

成功 clone 时调：

```python
usage_meter.record_voice_clone(
    provider="cosyvoice_voice_clone",
    model="cosyvoice-v3.5-flash",
    voice_id=clone_voice_id,
    speaker_id=main_speaker_id,
    source_audio_seconds=sample_seconds,
    selected_segment_count=len(sample_segment_ids),
    clone_count=1,
    billable=True,
    success=True,
    extra={
        "service_mode": "express",
        "phase_version": "4.3a",
        "worker_request_id": worker_request_id,
        "provider_request_id": provider_request_id or "",
    },
)
```

成本数据进现有 admin cost API（`/api/admin/jobs/{id}/cost`），Phase 4.3a 不引入新计费策略——`voice_clone_cost_credits` admin setting 已经存在（Phase 4.1 落地），Express 自动 clone 与 Studio 手动 clone 走相同费率。

---

## §10 测试清单

### 10.1 单元测试（不打 worker / 不打 OSS / 不打 DashScope）

文件 `tests/test_phase43a_express_auto_clone.py`，约 15-20 条：

1. **触发条件 5 层 AND**（每层独立测）：
   - `test_skip_when_admin_flag_off`
   - `test_skip_when_worker_env_disabled`（mock `is_worker_enabled_in_env() → False`）
   - `test_skip_when_user_not_in_allowlist`
   - `test_skip_when_consent_not_given`（`express_consent=None`、`auto_voice_clone=False` 两种）
   - `test_skip_when_main_speaker_below_ratio`（构造 25% top speaker）
   - `test_skip_when_main_speaker_below_min_lines`（构造 3 lines）
   - `test_skip_when_sample_too_short`（mock validate_sample → duration_s=8.0）

2. **主说话人识别**：
   - `test_identify_main_speaker_solo_speaker_returns_top`
   - `test_identify_main_speaker_balanced_two_speakers_returns_top_if_above_ratio`
   - `test_identify_main_speaker_3_way_split_returns_none`

3. **Worker 调用契约**（mock client.clone）：
   - `test_clone_request_uses_v3_5_flash_target_model`
   - `test_clone_request_includes_consent_confirmed_at_from_payload`
   - `test_clone_request_source_segments_matches_sample_extractor_emitted_line_ids`
   - `test_worker_503_writes_skipped_audit_no_register_call`
   - `test_worker_business_error_writes_skipped_audit_no_register_call`

4. **Routing 注入**（断言 `_speaker_voices / _speaker_voice_routing` shape）：
   - `test_success_writes_voice_id_into_speaker_voices`
   - `test_success_writes_routing_into_speaker_routing_dict`
   - `test_success_routing_uses_requires_worker_true_and_v3_5_flash_target`

5. **Register-smart 调用**（mock requests.post）：
   - `test_register_smart_includes_is_temporary_true`
   - `test_register_smart_includes_temporary_expires_at_now_plus_7d_metadata_only`（断言字段写入，但**不**断言任何 sweeper 调用——Phase 4.3a 没 sweeper）
   - `test_register_smart_includes_cosyvoice_routing_fields`
   - `test_register_smart_failure_treats_clone_as_failed_writes_audit`

6. **审计 JSONL**：
   - `test_audit_emitted_for_skipped_decision_includes_reason_code`
   - `test_audit_emitted_for_success_includes_worker_request_id`

7. **守卫（regression invariants）**：
   - `test_express_consent_validator_does_not_import_smart_consent`（AST 扫）
   - `test_run_express_auto_clone_does_not_import_minimax_voice_clone`（AST 扫）
   - `test_services_express_does_not_import_gateway`（AST 扫，与 D.7 同模式）
   - `test_express_consent_failure_is_soft_skip_not_hard_fail`

### 10.2 集成测试（带 fake worker / fake OSS uploader）

文件 `tests/test_phase43a_express_auto_clone_integration.py`，约 5-8 条：

- 用 `httpx.ASGITransport` 起 fake worker app（与 Phase 4.1 测试同款）
- 用 fake OSS uploader（in-memory）
- 跑一遍 `run_express_auto_clone(...)` 端到端
- 断言 `_speaker_voices / _speaker_voice_routing / audit JSONL` 全部正确

### 10.3 跨 phase 守卫（regression）

新增 `tests/test_phase43a_unchanged_smart_minimax_path.py`：

- `test_smart_consent_validator_unchanged_in_express_phase` — AST 扫 `gateway/smart_consent.py` 字节级相同
- `test_register_smart_endpoint_backward_compatible_with_smart_minimax_caller` — mock Smart caller 仍能成功（不传新字段）
- `test_add_user_voice_signature_backward_compatible` — 旧 caller 不受影响
- `test_studio_manual_clone_path_unchanged` — `gateway/cosyvoice_clone/api.py` 主入口字节级相同

**v0.1 新增（Codex P1×3 / P2×3 守卫）**：

文件 `tests/test_phase43a_codex_v0_1_invariants.py`：

- **P1-1（boto3 隔离）**：
  - `test_app_pyproject_does_not_import_boto3` — 解析 `pyproject.toml` 主 dependencies，断言不含 `boto3 / botocore`
  - `test_services_express_does_not_import_boto3` — AST 扫 `src/services/express/**/*.py`，无 `import boto3` 或 `from boto3` 字面量
  - `test_gateway_internal_upload_endpoint_exists` — sanity：grep `gateway/cosyvoice_clone/api.py` 含 `/api/internal/cosyvoice/express-sample-upload` route
  - `test_pipeline_calls_internal_upload_endpoint_not_boto3` — AST 扫 `src/services/express/auto_clone.py` 含 `requests.post.*express-sample-upload`，不含 `boto3.client` / `put_object` 等

- **P1-2（is_temporary 隔离矩阵）**（参见 §6.4 末尾 5 条守卫，列在这里编号统一）：
  - `test_list_user_voices_default_hides_is_temporary_true`
  - `test_count_active_voices_default_excludes_temporary`
  - `test_match_user_voices_default_excludes_temporary_for_smart_auto_reuse`
  - `test_lookup_clone_voice_routing_metadata_includes_temporary`（反向 sanity）
  - `test_smart_existing_callers_pass_default_include_temporary_false`

- **P1-3（孤儿清理）**：
  - `test_register_failed_triggers_delete_voice_call` — mock register-smart 返 500，断言 `client.delete_voice(...)` 被调一次
  - `test_register_failed_with_delete_voice_failed_writes_orphan_cleanup_failed_audit` — mock 两个都失败，断言 audit JSONL 写 `decision="register_failed_orphan_cleanup_failed"` + admin notification 被发
  - `test_register_failed_with_delete_voice_ok_writes_orphan_cleanup_ok_audit` — mock register 失败 + delete 成功，断言 audit 写 `decision="register_failed_orphan_cleanup_ok"` + voice_id 不出现在 user_voices 行
  - `test_delete_voice_failure_does_not_throw_into_pipeline` — pipeline 继续走预设音色，不因 delete_voice 异常 crash

- **P2-1（PR description 模板）**：纯 docs 守卫，跳过自动化（PR review 时人工检查）

- **P2-2（consent 解析失败写 audit）**：
  - `test_consent_invalid_payload_writes_audit_with_parse_error_reason_code` — 提交 `express_consent={"auto_voice_clone": "yes"}` (string, not bool)，断言 audit JSONL `reason_code` 包含 `consent_invalid_auto_voice_clone_not_bool`
  - `test_consent_none_writes_audit_with_consent_not_given_reason` — 提交 `express_consent=None`，断言 audit 写 `consent_not_given`（与 invalid 区分开）

- **P2-3（admin settings full-body save）**：
  - `test_admin_settings_default_includes_express_cosyvoice_auto_clone_fields` — AST 扫 `frontend-next/.../admin/settings/page.tsx::DEFAULT_SETTINGS`
  - `test_admin_settings_reset_payload_includes_new_fields`
  - `test_admin_settings_save_payload_completeness` — 集成测试：save 单字段后其余 cosyvoice / smart 字段保持原值

### 10.4 CI guard

`.github/workflows/*.yml`（如果有）需要确保：
- 单元测试默认跑（无外部依赖）
- 集成测试在 mock worker fixture 下跑
- 不引入 DashScope / OSS / 真武汉 worker 的网络 mock（应该 fail，因为 CI 没那些 env）

### 10.6 守卫扫描边界（v0.3 Codex 三轮 P3-2）

文档自身在变更日志 / 闭合检查项里**解释**旧 → 新字段映射会出现旧字面量（如 `query_routing_metadata` / `express_consent_at`）。守卫扫描时必须**只扫**代码 + 活 spec 主体，**不**扫这些解释性段落，避免误伤：

**白名单扫描区**（守卫"全文无旧字面量"类测试只扫这些）：

- `src/**/*.py`、`gateway/**/*.py`、`frontend-next/src/**/*.{ts,tsx}`、`tests/**/*.py` 全部源码
- 本 spec 的 §0-§17 主体段（**不**含 spec 顶部"变更摘要"+ §16 闭合检查项 + §17.1 PR description 模板示例）
- 其它 plan 文档的活段（如果引用本 spec）

**黑名单扫描区**（守卫**跳过**，允许旧字面量出现）：

- 本 spec 顶部 "变更摘要 v0 → v0.1 / v0.1 → v0.2 / v0.2 → v0.3" 块
- §16 v0.x 闭合验证清单（解释 v0.x 改了什么）
- 其它 plan 文档的 "历史决策" / "已废弃" 章节

**实施约束**：

- AST / grep 守卫脚本必须先用 marker 注释（如 `<!-- guard-scan-skip: changelog -->`）跳过变更日志块
- 或者守卫只读"# §0" 到 "# §16" 行号范围（spec 主体）
- §10 任何 "test_no_*_in_source" 类测试必须明示 spec 行号范围 / 文件过滤规则

**为什么这么做（Codex P3-2 原话）**："审计/变更日志里还有 `query_routing_metadata`、`express_consent_at` 的历史说明，这些是解释旧名，不是活 spec。可以保留，但 PR 实施时守卫要扫**代码和活 spec 段**，不要被变更日志误伤。"

### 10.5 Mock 策略汇总

| 模块 | mock 方式 |
|---|---|
| `services.mainland_worker.client.MainlandWorkerClient.clone()` | 用 `httpx.ASGITransport` + fake FastAPI app |
| OSS PUT | monkeypatch `boto3.client("s3")` 返回 stub session |
| `services.voice.sample_extractor.VoiceSampleExtractor.extract_sample()` | monkeypatch 写一个 silence WAV 到 output_path |
| `services.voice.sample_extractor.VoiceSampleExtractor.validate_sample()` | monkeypatch 返 `{"duration_s": <ctrl>, "is_valid": True, ...}` |
| `requests.post("/api/internal/user-voices/register-smart", ...)` | monkeypatch requests，捕获 payload 做断言 |
| `services.admin_settings.read_admin_setting()` | monkeypatch 返 dict |

**绝不在测试代码里出现的字符串**（CI 守卫扫）：
- 真实 DashScope endpoint URL
- 真实 OSS bucket / endpoint
- 真实武汉 worker URL `8.148.83.128`（或部署后的内网 IP）

---

## §11 不做事项（明示）

| 项 | 原因 |
|---|---|
| ❌ Phase 4.3a 不接 Smart 路径 | Smart 的 MiniMax 自动 clone 字节级不变，是 NG1 |
| ❌ 不做 user-facing cost preview | canary 阶段不展示给普通用户 |
| ❌ 不做 admin 灰度仪表盘 | Phase 4.3 完整版做（先看运行 1-2 周的 audit 数据） |
| ❌ 不做"克隆失败提示用户手动 review" | Express 没有 review UI，提示无处可去 |
| ❌ 不做多主说话人克隆 | NG5；Phase 4.3 全量再做 |
| ❌ 不做"Express → Smart 升级"路径 | UI 复杂度大，等 Phase 4.3 完整版讨论 |
| ❌ 不做 user_voices 候选复用 | Express 无 candidates 查询；Phase 4.3 完整版补 |
| ❌ 不引入 VolcEngine 自动 clone | VolcEngine clone 不在生产路径 |
| ❌ 不实现 temporary_expires_at 批量 sweeper（cron / scheduled task） | Phase 4.2 §12.4 定义了 sweeper 但未实现。Phase 4.3a **写入** `is_temporary=true + temporary_expires_at=now+7d` 字段，**仅作为 Phase 4.3b sweeper 未来入选条件的元数据**（不是 Phase 4.3a 阶段的清理承诺）。Sweeper 落地之前，临时音色靠 §6.4 隔离矩阵（不显示 / 不计配额 / 不参与 Smart auto-reuse）保证不污染产品体验。**孤儿清理** ≠ **TTL sweeper**——register-failed 路径有 inline `delete_voice`（§7.3），但 TTL 到期不会触发自动调用 |
| ❌ 不动 `/register-smart` endpoint 名字 | 改名会冲击 Smart 现有 caller（process.py:1468）；新增字段是 backward-compatible |
| ❌ 不抽公共 sample_uploader | NG6 暗含；Phase 4.3 完整版再考虑 |
| ❌ 不开放 plus 模型 | `cosyvoice-v3.5-flash` 唯一；admin setting 是 read-only display "固定 flash" |
| ❌ 不部署生产 | 用户单独授权 |

---

## §12 实施分阶段（B-G）

| Phase | 内容 | 估时 | 关键产物 |
|---|---|---|---|
| **B** | 守卫测试先行（§10.3 跨 phase 不变性 AST 扫 + v0.1 新增 P1×3 / P2×3 守卫 + v0.2 新增 P1×6 守卫） | 5-7h | `tests/test_phase43a_unchanged_smart_minimax_path.py` + `tests/test_phase43a_codex_v0_1_invariants.py` + `tests/test_phase43a_codex_v0_2_invariants.py` |
| **C** | 后端：`gateway/express_consent.py` + JobRecord schema 扩展（含 `express_consent_parse_error`）+ `compute_job_policy` 把 `express_consent` 落 record + **v0.2 生成 server_confirmed_at** | 4-5h | gateway PR |
| **D** | 后端：admin_settings **8** 字段（v0.2 +daily_cap+active_temp_cap）+ admin UI controls + availability endpoint + **v0.2 budget endpoint** + frontend page.tsx full-body save 守卫 | 5-7h | gateway PR + frontend PR |
| **D1** | 后端：`user_voice_service.py` 三函数 (`list_user_voices` / `count_active_voices_*` / `match_user_voices`) 加 `include_temporary` kwarg（v0.1 §6.4 P1-2 fix） | 2-3h | gateway PR |
| **E** | 后端：`/register-smart` 扩展新字段（routing 9 + temporary 2 = 11 个 optional kwargs）+ `add_user_voice` 加 `is_temporary` / `temporary_expires_at` kwarg + **v0.2 防漂移 400**（CosyVoice provider + smart_auto created_from 组合拒收） | 4-5h | gateway PR |
| **E1** | 后端：**新增** internal endpoint `POST /api/internal/cosyvoice/express-sample-upload`（v0.1 §5.3 选 D），复用 `sample_uploader.py`，**v0.2 §5.5 完整安全合同**（鉴权 / size cap / content-type / 日志脱敏 / 8 个 HTTP scenario） | 4-6h | gateway PR |
| **E2** | 后端：**新增** budget endpoint `GET /api/internal/express-auto-clone-budget`（v0.2 §2.5 daily_count + active_temp_count + can_clone） | 2-3h | gateway PR |
| **F** | pipeline：`src/services/express/{main_speaker,auto_clone,audit}.py` + process.py 调用点 + sample_extractor 复用 + **HTTP POST 到 E1 endpoint**（不 boto3）+ **HTTP GET E2 budget**（Layer 6/7）+ worker client 调用 + register 调用 + register-failed 孤儿清理（§7.3） | 10-14h | app PR |
| **G** | frontend：TranslationForm consent checkbox + availability fetch + Tooltip 文案（**v0.2 不含 "7 天清理" 字面承诺**） + page.tsx DEFAULT_SETTINGS / reset 覆盖**全部 8** 个新字段 | 4-5h | frontend PR |
| **H** | §10 单元 + 集成测试全部落地（含 v0.1 16+ + v0.2 17+ = ~35 条守卫） | 8-10h | test PR |
| **I** | Codex review（多轮） + 修 P1 + 修 P2 | 8-12h | review iterations |

**关键依赖顺序**：B → C/D/D1（并行）→ E/E1/E2（并行）→ F → G/H（并行）→ I

**总估时（v0.2 更新）**：~56-77 工时（约 7-10 工作日 + review）

**v0.2 vs v0.1 估时变化**：
- B 阶段守卫从 ~16 条增到 ~33 条：+1-2h
- C 阶段加 server_confirmed_at：+1h
- D 阶段加 2 个 admin 字段 + admin budget endpoint：+1-2h
- E 阶段加防漂移 400：+1h
- 新增 E2（budget endpoint）：+2-3h
- F 阶段加 budget HTTP GET（Layer 6/7）：+2h
- H 测试规模 ~35 条：+2h
- I review 轮次预计再增加：+2h

---

## §13 灰度 & rollback 草案（部署阶段单独 spec 化）

部署不属于 Phase 4.3a spec 范围（NG7），但灰度策略骨架先写出来供 review：

**Stage 0 - 仅开发**：
- `express_cosyvoice_auto_clone_enabled = False`
- 守卫测试全绿
- 不部署

**Stage 1 - admin 自测**：
- 部署到 US prod，flag 仍 `False`
- admin 后台翻 `True`，allowlist 加 admin 自己的 user_id
- 跑 3-5 个真实任务，断言：
  - 武汉 worker audit JSONL 出现 `provider=cosyvoice_voice_clone` 行
  - DB user_voices 出现新行带 `is_temporary=True`
  - segments 落 `requires_worker=True, worker_target_model="cosyvoice-v3.5-flash"`
  - audit JSONL 写 `decision=cloned`
  - 普通用户访问 availability endpoint 返 `available=false`（前端不显示 checkbox）

**Stage 2 - 内部用户 canary**：
- allowlist 增加 1-2 个内部用户的 user_id
- 监控 7-14 天
- 关注信号：
  - clone 成功率 / 失败率
  - worker latency
  - DashScope 余额扣费速率
  - audit JSONL 的 reason_code 分布（理解谁被 skip 了）

**Stage 3 - GA（Phase 4.3 完整版）**：
- 改 `general_availability_enabled` 模式，类似 Phase 4.2 §8.1 的两段灰度
- **不**进 Phase 4.3a 范围

**Rollback**：
- Level 1 - admin 后台翻 `False` → 立即停止新 clone（运行中任务跑完）
- Level 2 - allowlist 清空 → 类似 Level 1，但保留 flag 为后续灰度
- Level 3 - revert frontend + gateway commits（最坏情况）

---

## §14 风险矩阵

| # | 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|---|
| R1 | 改 `/register-smart` 误破 Smart MiniMax caller | M | High | §10.3 backward-compat 守卫 + 新字段全 optional |
| R2 | OSS 60 行精简 PUT 与 gateway sample_uploader 漂移（presign URL TTL / sha256 不一致） | M | M | §5.3 选 C 时明确：复用同套 env，sha256 算法字节级一致（hashlib.sha256(bytes).hexdigest()），守卫测试断言 |
| R3 | 主说话人识别误判（30% 阈值偶尔放走噪音） | L | M | §4.2 双重门：min_ratio + min_line_count；7-14 天 canary 观察 reason_code 调参 |
| R4 | 用户在 allowlist 但 worker 临时挂 → clone 失败但用户期望它成功 → 抱怨 | M | L | §7 降级到预设音色 + 显式 audit；UI 文案明确 "实验性" |
| R5 | 任务级临时音色 7d 后未清理（sweeper 未实现）→ DashScope 积压孤儿 voice_id | H | L（费用低）/ M（quota 上限） | **v0.1 修正**：§11 NG11 明示**不**承诺 7 天 TTL 自动清理；sweeper 留 Phase 4.3b 单独 spec。Phase 4.3a 接受短期累积，由 §6.4 隔离矩阵保证临时音色不污染 UI / 配额 / Smart auto-reuse。监控 DashScope quota 余量，超阈值时手动调 admin delete API 或起 Phase 4.3b sweeper |
| R10 | v0.1 §5.3 方案 D（gateway internal upload endpoint）增加一次 app → gateway 内网 HTTP hop，sample bytes（~640KB）走 HTTP body 可能压力大 | L | L | 与 `/register-smart` 同套 internal channel，已经在 prod 跑了几个月；sample bytes 体积有 §2 Layer 5 的 ≤2MB cap（与 sample_uploader 一致），docker network 内网带宽充裕；如果 latency 实测 > 500ms 再考虑 Phase 4.3b 引入 Unix socket |
| R11 | v0.1 §7.3 best-effort delete_voice 自身失败（worker 网络 / DashScope 503） | M | M（孤儿累积） | client.py 已经 `max_attempts=3` + 指数退避；3 次都失败 → 写 admin notification + audit `orphan_cleanup_failed`，依赖 Phase 4.3b sweeper 兜底；Phase 4.3a canary 期间预期单日孤儿 ≤ 5 个，可手工处理 |
| R12 | v0.1 §6.4 给 `match_user_voices` 加 `include_temporary` kwarg，Smart 路径调用点漏改默认值 → Smart 跨任务误复用 Express 临时音色 | M | High（违反 NG1 Smart 不动） | §10.3 守卫 `test_smart_existing_callers_pass_default_include_temporary_false` 用 AST 扫 process.py 所有 `_match_smart_user_voice` 调用点，确保 `include_temporary=True` 不存在；新 kwarg 默认 False 也是 fail-closed |
| R13 | v0.2 §2.5 daily_cap 默认 5 太松 → 单个 allowlist 用户每天能跑 5 次 paid worker clone，1 个月 ~150 次 × ¥0.01-0.03 = ¥1.5-4.5（不致命但要监控） | L | L | §13 灰度 Stage 1 把 daily_cap 临时调到 1（admin 翻 admin_settings），Stage 2 加到 3，Stage 3（Phase 4.3 完整版）再放到 5；admin 后台 budget endpoint 每天扫一次 |
| R14 | v0.2 §3.1.a 把 client_confirmed_at 标记为辅助、用 server_confirmed_at 落 worker / audit。如果 pipeline / gateway 时钟漂移（NTP 异常），worker 收到的 confirmed_at 与 client 实际勾选时间差几分钟 | L | L | gateway 容器跑在管理的 host 上有 systemd-timesyncd / chrony；server_confirmed_at 漂移几秒不影响 consent 法律意义；client_confirmed_at 仍作为 audit 辅助证据保留 |
| R15 | v0.2 §6.3 endpoint 防漂移 400（CosyVoice provider + smart_auto created_from 拒收）误伤未来其它 CosyVoice 自动 clone 路径（比如 Phase 4.3b Smart-CosyVoice mix） | L | M | endpoint 错误信息明确列出允许的 `created_from` 值（`"express_auto" / "studio_manual" / "cosyvoice_clone_endpoint"`）；Phase 4.3b 加新路径时同步加新允许值并更新 endpoint validation；测试守卫 `test_register_smart_explicit_express_auto_accepted` 锁住当前允许列表 |
| R6 | `express_consent` schema 校验失败把任务整个 reject（违反 "soft skip"） | L | High | §3.1 校验函数明确 `(dict | None, str | None)` 返回，caller 看到 None 就当 unchecked，不抛异常 |
| R7 | pipeline 子进程 import services.mainland_worker.client 时 httpx 没装 → ImportError | L | High | `client_factory.build_client_from_env()` 已 lazy import（Phase 4.1 E.1 PR #15 P1 二轮 fix）；Phase 4.3a 走它，不直接 top-level import client |
| R8 | Express UI 加 checkbox 让原本 zero-touch 的快捷版变复杂 | M | M | 默认不显示（仅 allowlist 用户 + admin flag True 才显），普通用户完全无感知 |
| R9 | 任务跑到一半 admin 翻 flag → 半截 clone 状态 | L | L | flag 在 pipeline 启动时 read 一次，跑到一半不会重 read |

---

## §15 Open questions（用户决策点）

下列项需要用户确认后才能开 PR：

1. **主说话人占比阈值 30% / min lines 5 是否合理？** 还是先用更保守的 50% / 10 行？
   - 建议：先用 30% / 5 行，canary 观察 1 周后调整
2. **canary 阶段 allowlist 是否只放 admin 自己？** 还是放 2-3 个内部测试账号？
   - 建议：Stage 1 只 admin，Stage 2 加 1-2 内部账号
3. **`/register-smart` endpoint 是否应该改名为 `/register-auto-clone`？**
   - 建议：Phase 4.3a 不改（保持向后兼容），Phase 4.3 完整版讨论
4. **是否需要 Express auto-clone 失败时给用户在工作台显示提示？**
   - 建议：Phase 4.3a 不显示（canary，普通用户看不到 checkbox），Phase 4.3 完整版讨论
5. **`express_consent` 是否要扩展更多字段（参考 Smart 的 6 字段）？**
   - 建议：Phase 4.3a 只 2 字段（auto_voice_clone + confirmed_at）够用；Phase 4.3 完整版讨论是否要 quality_tier / on_budget_exhausted 等

---

## §16 实施前自检清单（v0.1 更新）

PR 开之前必须确认：

- [ ] 用户已审 §0 / §1 范围声明
- [ ] 用户已审 §2 五层 AND 的具体阈值
- [ ] 用户已审 §3 consent schema 的 2 字段（不引入 6 字段）+ consent 解析失败的 audit 路径（P2-2）
- [ ] 用户已审 **§5.3 OSS 选 D 方案**（新增 gateway internal upload endpoint，pipeline 不引 boto3）— v0.1 Codex P1-1 fix
- [ ] 用户已审 **§6.3 `/register-smart` 扩展（不改名）+ §17.1 PR description 模板**（v0.1 P2-1）
- [ ] 用户已审 **§6.4 `is_temporary` 隔离矩阵**（3 个函数加 `include_temporary` kwarg）— v0.1 P1-2 fix
- [ ] 用户已审 §7 失败降级表（任何失败 = 回预设，不调 MiniMax）+ **§7.3 register 失败孤儿清理 hard-required**（v0.1 P1-3 fix）
- [ ] 用户已审 **§8 admin_settings 字段命名 + full-body save 守卫**（v0.1 P2-3 D.1 教训）
- [ ] 用户已审 §9 audit JSONL schema（含 v0.1 新字段 `express_consent_parse_error / register_failure_detail / delete_voice_*`）
- [ ] 用户已审 §10 测试清单（含 v0.1 新增 16+ 守卫）
- [ ] 用户已审 **§11 不做事项（含 NG10 boto3 + NG11 sweeper / TTL 撤回）**
- [ ] 用户已审 §15 5 个 open questions
- [ ] @codex review 此 spec v0.2
- [ ] Codex 反馈纳入 spec v0.3 后再开 PR
- [ ] **v0.1 闭合验证**：Codex 复审 spec v0.1 时确认下列 v0 → v0.1 关键变更已闭合：
  - boto3 不进 app 容器（NG10 + §5.3 方案 D）
  - 临时音色 list/count/match 全部隔离（§6.4 矩阵）
  - register 失败必走 best-effort delete_voice（§7.3）
  - 不再承诺 7 天自动 TTL（§11 NG11）+ DoD / UI / PR description / 附录 A 全文消除 7d 自动清理字面承诺
  - consent 解析失败有 audit + reason_code（§3.1 + §9.1）
  - admin full-body save 三守卫（§8.2 P2-3）
  - PR description 模板写明扩展 ≠ Smart 语义扩张（§17.1）
- [ ] **v0.2 闭合验证**（Codex 二轮 P1×6）：
  - §1.1 G4 + DoD #2 + §3.1 UI 提示 + 附录 A 表 + register-smart payload 全文统一"7d 是元数据不是 TTL 承诺"（无任何 "7 天后自动清理 / 7 天后过期" 字面）
  - §2.5 成本闸两个独立 cap（daily 默认 5 + active temp 默认 3） + admin endpoint + audit reason_code + 3 条守卫
  - §5.5 internal upload endpoint 完整安全合同（鉴权 / size cap / content-type / 日志脱敏 / 8 个 HTTP scenario 测试）
  - §6.3 / §6.4 函数名改为真实代码名 `lookup_clone_voice_routing_metadata`
  - §3.1.a consent 时间戳 client / server 拆分；worker request 用 server_confirmed_at
  - §6.3 register-smart 区分 Express via `created_from="express_auto"` + endpoint 防漂移 400 + 4 条 backward-compat 守卫
- [ ] **v0.3 闭合验证**（Codex 三轮 P1×2 / P2×2）：
  - §9.1 audit schema 字段从 `express_consent_at` 改为 `express_consent_server_at` + `express_consent_client_at`（旧字段名全文无残留 + §10 守卫扫住）
  - §6.3.1 `add_user_voice` 临时字段更新合同（insert + existing revive 双路径覆盖 + 非 temp 时清 stale + 5 条守卫）
  - 全文残留旧函数名 `query_routing_metadata` → `lookup_clone_voice_routing_metadata`（grep 0 残留）
  - §2.6 Layer 顺序锁定表（policy → allowlist → consent → budget → main speaker → sample → OSS → worker → register）+ 5 条 budget-before-paid-action 守卫

---

## §17 文档变更点（spec 通过后跟随）

- `docs/graphs/GITNEXUS_PROJECT_GRAPH.md` §2 / §5.x 关键基座表加 "Phase 4.3a Express auto-clone"
- `docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md` 主图加 Express auto-clone 分支
- `docs/graphs/GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md` 加 Express 触发链
- `docs/plans/README.md` 加本 spec 链接
- `CLAUDE.md` "付费 API 不能自动调用" 章节加：Express auto-clone 是显式 consent 触发的例外，不违反硬约束（与 Studio 手动 clone 同性质，只是 UI 入口在提交页而非选音色页）

### 17.1 PR 描述模板必含字段（v0.1 Codex P2-1）

每个 Phase 4.3a 子 PR（C / D / E / F / G / H）的 PR description **必须**包含：

**Scope 声明**：

> 本 PR 属于 **Phase 4.3a Express CosyVoice 自动克隆 canary**。
> Phase 4.3a 是**独立的 Express service mode 自动克隆路径**，**不是 Smart 路径的扩展**。
> Smart MiniMax 自动克隆（`process.py:3640-4100`）**字节级不变**，由守卫测试 `test_phase43a_unchanged_smart_minimax_path.py` 锁定。

**对 `/api/internal/user-voices/register-smart` 的扩展声明**（若 PR 涉及）：

> 本 PR 为 `/api/internal/user-voices/register-smart` endpoint 新增 12 个 optional body 字段（routing + temporary metadata）。
> 这是**向后兼容扩展**——现有 Smart MiniMax caller（`process.py::_register_smart_clone_in_user_voices`）不传新字段时行为字节级不变，由守卫 `test_register_smart_backward_compatible_with_smart_minimax_caller` 验证。
> **不要把** Express 自动 clone 路径理解为 Smart 语义的扩展；它们共享 endpoint 是为了避免在内部基础设施层做两套并行实现，但产品语义、触发条件、consent 流、UI 入口、reason_code 命名空间都是独立的。
> Phase 4.3 完整版会考虑将 endpoint 改名（`/register-auto-clone`），届时 Smart caller 也跟随迁移。

**Codex review tags**：

- 引用本 spec v0.1 章节号（§5.3 / §6.4 / §7.3 等）
- 列出本 PR 受影响行数（gateway / app / frontend / tests 分别）
- 列出新增的守卫测试数量（应 ≥ §10 列出的对应项）

---

## 附录 A — 与 Phase 4.2 §13 方向声明的对应

Phase 4.2 §13.1 列出了 Phase 4.3 目标与当前的差距。Phase 4.3a 完成的部分：

| §13.1 维度 | Phase 4.1 当前 | Phase 4.3 目标 | Phase 4.3a 实施 |
|---|---|---|---|
| Provider | MiniMax 唯一 | CosyVoice 加入 | ✅ Express 走 CosyVoice |
| 默认模型 | MiniMax v1 | cosyvoice-v3.5-flash | ✅ 硬编码 flash |
| 用户授权 | smart_consent.auto_voice_clone | 不变（显式任务级勾选） | ✅ express_consent.auto_voice_clone |
| 保存策略 | is_temporary=false | is_temporary=true + 7d 元数据（Phase 4.3a 不自动清理；4.3b sweeper） | ✅ 字段一致 / 行为分两阶段 |
| 多说话人 | 逐说话人 sequential | 不变 | ⏸ Phase 4.3a **只**主说话人，多说话人留 Phase 4.3 完整版 |
| 失败 fallback | hard fail | 不变 | ✅ 回预设音色（不调其它付费 API） |

Phase 4.3a 完成后，距离 Phase 4.3 完整版还差：
- 多主说话人覆盖
- Smart 路径加 CosyVoice 选项（admin 配 default）
- Candidates 复用 / weak-match auto-reuse
- temporary_expires_at sweeper
- General availability flip
