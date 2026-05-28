# Phase 4.3a PR2 — Atomic reservation + pipeline 调用链 spec

**作者：** Claude (Opus 4.7 / 1M)
**版本：** v0.2（Codex 2026-05-28 二轮 review 修订；待三轮 review，未 commit）
**日期：** 2026-05-28
**变更摘要 v0.1 → v0.2：**
- §4.1 reserve transaction 内**先 inline expire** 当前 user 的 stale reserved（`expires_at < now`）**再计数**——不依赖 TTL sweeper，防 sweeper 挂了 stale 占 cap。+ 测试 `test_reserve_inline_expires_stale_before_count` / `test_reserve_stale_same_key_not_idempotent_reused`
- §4.1 `users FOR UPDATE` 查不到 user → **404 user_not_found fail-closed**，不建 reservation；pipeline skip + audit `express_auto_clone_reserve_user_not_found`。+ 测试 `test_reserve_unknown_user_no_insert`
- §10.7 CI 细节确认：GitHub Actions 新增 `backend-pg-integration` job（postgres service），asyncpg 已在 gateway/requirements.txt 无需新依赖
- §3/§12 reservation 表归属确认：`gateway/alembic/versions/032_*.py`（down_revision='031'）+ ORM `gateway/models.py` + migration/ORM guard

**变更摘要 v0 → v0.1：**
- 修自洽：reserve 顺序统一为 **sample extract/validate 之后、upload/worker 之前**（sample 是本地 CPU 不占名额）。DoD #4 + 测试名全改成 "before upload/worker"，不再写 "before sample"
- §3 RESERVATION_TTL 默认 30 分钟 + admin_settings 新字段 `express_cosyvoice_auto_clone_reservation_ttl_minutes` + validator `5 <= ttl <= 120`
- §4.1 reservation lock 方案精确化：锁 `users` row（`SELECT id FROM users WHERE id=:user_id FOR UPDATE`）而非 `pg_advisory_xact_lock`（避免 hash collision；PG-only，sqlite 降级见 R5）
- §7 reserve 成功 + upload 失败必 release；release 自身失败 → audit 记 `reservation_release_failed`，不静默
- §10.7 新增：PG 并发 integration 测试 `tests/test_phase43a_pr2_reservation_pg_atomic.py`（CI 单独 job 起 postgres，只跑这一个文件），sqlite 单测保留状态机/约束/sweeper
**前置：** PR1（gateway foundation）已 merge（origin/main `9df5b4f8`）。PR1 交付了 consent / admin_settings / is_temporary 隔离 / register-smart 自洽 / upload endpoint / **advisory** budget endpoint。
**主 spec：** [`docs/plans/2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md`](2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md) v0.4（§2.6a 把 atomic reservation 列为 PR2 hard requirement）

---

## §0 背景：PR1 留给 PR2 的契约

PR1 把 Express auto-clone 的 **gateway 基础层**做完了，但**没有任何 pipeline 调用它**——Express 任务现在行为与合入前完全一致（admin 主开关默认 False）。PR1 明确留了一个**契约债**给 PR2：

> **§2.6a（主 spec）**：`GET /api/internal/express-auto-clone-budget` 是 **advisory snapshot，不是 atomic gate**。并发下多个 Express job 读到相同 count 全部 `can_clone=true` → 一起进付费 worker → 突破 cap。**付费前最终成本闸必须由 PR2 的 atomic reservation 实现**。

PR2 = 把这条契约债还掉（atomic reservation）+ 把 pipeline 调用链接上（让 Express 任务真的能自动 clone）。

PR1 已就位、PR2 直接复用的资产：

| 资产 | 位置 | PR2 用途 |
|---|---|---|
| `validate_express_consent` + JobRecord `express_consent`（含 `server_confirmed_at`） | gateway（PR1-C） | pipeline 读 consent gate |
| admin_settings 8 字段（含 `per_user_daily_cap` / `per_user_active_temp_cap`） | gateway（PR1-D） | reserve 时读 cap |
| `is_temporary` 隔离矩阵 | user_voice_service（PR1-D1） | 临时音色不污染 UI/配额/reuse |
| `/register-smart` 11 字段 + cosyvoice 自洽 + 临时音色强制 expiry | user_voice_api（PR1-E/E-fix/review-fix） | clone 成功后注册 |
| `POST /api/internal/cosyvoice/express-sample-upload`（§5.5 安全合同 + readiness gate） | cosyvoice_clone/api（PR1-E1/E1-fix） | pipeline 上传 sample 拿 presigned URL |
| `GET /api/internal/express-auto-clone-budget`（**advisory**）+ 2 counter | user_voice_api（PR1-E2） | pipeline 早期 fail-fast 短路（非最终闸） |
| `MainlandWorkerClient.clone / delete_voice` + `build_client_from_env` | src/services/mainland_worker | worker clone + 孤儿清理 |
| `VoiceSampleExtractor.extract_sample / validate_sample` | src/services/voice/sample_extractor | 主说话人样本抽取（Smart 已用） |

---

## §1 范围 / 非范围

### 1.1 范围（PR2 in-scope）

- **G1 atomic reservation**：独立 reservation 表 + reserve/consume/release endpoints + TTL sweeper，付费前原子预占名额（还 §2.6a 契约债）
- **G2 budget count 含 active reservations**：daily / active_temp 计数把"已预占未消费"的 reservation 算进去，防并发穿透
- **G3 pipeline 调用链**：`src/services/express/{main_speaker,auto_clone,audit}.py` + `process.py` Express 分支调用点，把 5 层 gate + reserve + sample + upload + worker + register + consume/release 串起来
- **G4 失败降级 + 孤儿清理**：任何失败回 CosyVoice 预设音色（不调 MiniMax）；worker 成功 register 失败 → best-effort `delete_voice` + release
- **G5 调用顺序守卫**：测试锁死 reserve 必须在所有付费动作前 + consume/release 配对

### 1.2 非范围（PR2 out-of-scope）

- **NG1 前端 consent checkbox / availability wiring** → **PR3**（TranslationForm UI + 未勾选也发 `{auto_voice_clone: false}` 守卫）
- **NG2 不动 Smart MiniMax 自动 clone**（process.py:3640-4100 字节级不变，PR1 守卫继续生效）
- **NG3 不动 Studio 手动 clone**
- **NG4 不开启 Express auto-clone**（admin 主开关 PR2 仍默认 False；真正开启 + 部署是 PR3 之后的灰度阶段）
- **NG5 不在 app 容器引入 boto3 / SQLAlchemy ORM**（D.7；pipeline 全程走 gateway internal endpoints + `requests`）
- **NG6 temporary_expires_at 批量 sweeper（音色过期清理）仍不做**（Phase 4.3b；PR2 只做 **reservation** TTL sweeper，与音色 expiry sweeper 是两回事）

### 1.3 验收 DoD

1. ✅ reserve endpoint 在 DB transaction 内原子检查 cap + 预占；并发压测（N 个并发 reserve 同一 user）只有 cap 个成功，其余返 `cap_exceeded`
2. ✅ reserve 幂等：同 `user_id+job_id+speaker_id` 重试返回**同一** active reservation，不重复占额度
3. ✅ daily/active_temp count 把 active reservation 算进去（并发不穿透）
4. ✅ pipeline 调用顺序锁死：reserve 在 **upload/worker 之前**（sample extract/validate 在 reserve **之前**——本地 CPU 不占 reservation 名额）；worker 成功 → register → consume；worker/register 失败 → release（+ register 失败时 delete_voice 孤儿清理）
5. ✅ 任何失败 → 回 CosyVoice 预设音色 + audit JSONL reason_code（不调 MiniMax）
6. ✅ TTL sweeper：reserved 超 `expires_at` → expired（释放额度），幂等
7. ✅ Smart / Studio clone 路径字节级不变（PR1 守卫继续绿）
8. ✅ admin 主开关 False 时 Express 任务行为与 PR1 合入前一致（不触发任何 reserve / clone）

---

## §2 五个关键决策（Codex 拍死）

### 2.1 决策 1：独立 reservation 表，**不**复用 user_voices placeholder ✅

`user_voices` 是**音色事实表**（list / match / routing / count 都查它）。用它做 reservation placeholder 会污染所有这些查询的语义（D1 刚把 is_temporary 隔离干净，再塞 placeholder 又脏了）。

**独立表 `express_clone_reservations`**：状态机 `reserved / consumed / released / expired` 清晰，与音色事实表解耦。migration `032`（PR1 head 是 031）。

### 2.2 决策 2：reserve 必须在任何付费动作之前（锁死顺序）✅

```
L1 admin flag → L2 worker env → L3 allowlist → L4 consent
  → L5 budget advisory（fail-fast 快速短路，非最终闸）
  → L6 main speaker 识别
  → L7 sample extract + validate
  → ★ ATOMIC RESERVE ★（最终成本闸；失败则停，回预设音色）
  → upload sample（PR1 E1 endpoint）
  → worker clone（付费）
       ├─ 成功 → register-smart → consume reservation
       └─ 失败 → release reservation（回预设音色）
  → register 失败（worker 已成功）→ best-effort delete_voice 孤儿清理 + release
```

**关键**：reserve 是 L7（sample 抽取）**之后**、upload/worker（付费）**之前**的最后一道闸。为什么不更早（L5 budget 之后立刻 reserve）？因为 L6/L7（主说话人识别 + sample 抽取）可能 fail（占比不够 / 样本太短），那时还没付费，不该占 reservation 名额。把 reserve 放在 L7 之后，确保只有"马上要付费"时才预占。

### 2.3 决策 3：reservation endpoint 幂等 ✅

`user_id + job_id + speaker_id` 是幂等键。pipeline 重试（网络抖动 / 进程重启 resume）调 reserve 不能重复占额度——返回**同一** active reservation。

实现：partial unique index `(user_id, job_id, speaker_id) WHERE status = 'reserved'`。reserve 先查 active reservation，命中则返回；未命中则原子检查 cap + insert。

### 2.4 决策 4：budget count 含 active reservations ✅

否则并发仍穿透：job A reserve 成功（reservation 行还没 consume 成 user_voices 行），job B 此刻查 count 看不到 A 的预占 → B 也通过。

- `daily_count` = 今天 express_auto 的 `user_voices` rows（已消费）+ 今天 active/reserved reservations（预占未消费）
- `active_temp_count` = active 临时音色（user_voices）+ active reservations

去重：consume 会把 reservation → user_voices row，两者不能重复计。用 `status='reserved'`（未 consume）的 reservation 计数 + 已 consume 的算进 user_voices（consumed reservation 不再单独计）。

### 2.5 决策 5：调用顺序守卫 ✅

静态 + 单元测试都要挡住：

- pipeline 直接调 advisory GET 后就进 worker（绕过 reserve）
- 没 reserve 就 upload / worker
- register 成功不 consume（泄漏 reservation）
- worker 成功 register 失败不 delete orphan（DashScope 孤儿 + 不 release）

---

## §3 reservation 表 schema（migration 032）

```sql
CREATE TABLE express_clone_reservations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id              VARCHAR(64) NOT NULL,
    speaker_id          VARCHAR(64) NOT NULL,
    -- 状态机：reserved → consumed | released | expired
    status              VARCHAR(16) NOT NULL DEFAULT 'reserved',
    target_model        VARCHAR(50) NOT NULL,            -- 预占时记录（cosyvoice-v3.5-flash）
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,            -- TTL（reserve 时 now + RESERVATION_TTL）
    consumed_voice_id   VARCHAR(200),                    -- consume 时填（关联 user_voices.voice_id）
    released_reason     VARCHAR(64)                      -- release / expire 时填原因（审计）
);

-- 决策 3 幂等：同 user+job+speaker 最多一个 active(reserved) reservation
CREATE UNIQUE INDEX uq_express_reservation_active
    ON express_clone_reservations (user_id, job_id, speaker_id)
    WHERE status = 'reserved';

-- 决策 4 count 查询：按 user + status + created_at 过滤
CREATE INDEX idx_express_reservation_user_status
    ON express_clone_reservations (user_id, status, created_at);

-- TTL sweeper 选行：reserved + expires_at < now
CREATE INDEX idx_express_reservation_ttl_pending
    ON express_clone_reservations (expires_at)
    WHERE status = 'reserved';
```

**状态机**：

| from | to | 触发 | 写字段 |
|---|---|---|---|
| (new) | reserved | reserve（原子检查 cap 通过） | created_at / expires_at / target_model |
| reserved | consumed | register-smart 成功后 consume | consumed_voice_id / updated_at |
| reserved | released | clone 失败 / register 失败 / pipeline 主动放弃 | released_reason / updated_at |
| reserved | expired | TTL sweeper（expires_at < now） | released_reason="ttl_expired" / updated_at |

**RESERVATION_TTL**：默认 **30 分钟**，admin_settings 新字段 `express_cosyvoice_auto_clone_reservation_ttl_minutes`（PR2-A 加，与 PR1 的 8 个 `express_cosyvoice_auto_clone_*` 同 section）。覆盖单次 Express clone 的 upload+worker 全程（正常 < 1 分钟），留足崩溃回收冗余。

**validator（Codex v0.1 要求）**：`5 <= ttl_minutes <= 120`，与 PR1 其它 `express_cosyvoice_auto_clone_*` validator 同模式（admin_settings.py field_validator）。下界 5 防误设 0（reservation 立即过期 → 永远 reserve 不到）；上界 120 防误设 7 天（崩溃的 reserved 名额长期占 cap）。full-body save 守卫（PR1 D.1 同模式）也覆盖此新字段：frontend `DEFAULT_SETTINGS` + reset payload 须含它（默认 30）。

---

## §4 reservation 状态机 endpoints

全部 internal（X-Internal-Key + loopback，与 PR1 internal endpoint 一致），挂 `user_voice_api.internal_router`（prefix `/api/internal`）。

### 4.1 `POST /api/internal/express-auto-clone-reservations/reserve`

**幂等 + 原子**。body：`{user_id, job_id, speaker_id, target_model}`。

逻辑（单 DB transaction）：

```
BEGIN;
-- 1. per-user 串行化锁（Codex v0.1：锁 users row，不用 advisory hash）
--    锁住该 user 的 users 行，串行化同一 user 的所有并发 reserve。
--    选 users row FOR UPDATE 而非 pg_advisory_xact_lock(hashtext(...))：
--    advisory lock 用 hashtext 有 collision 风险（不同 user_id 哈希撞同一
--    锁 key → 误串行化不相关用户）；锁 users row 用真实主键，零 collision。
SELECT id FROM users WHERE id=:user_id FOR UPDATE;
-- 1a. user 不存在 → fail-closed（Codex v0.2）：不建 reservation。
IF not found → ROLLBACK; return 404 {ok:false, error:"user_not_found"}

-- 2. 先 inline expire 当前 user 的 stale reserved（Codex v0.2）：
--    不依赖 TTL sweeper —— 即使 sweeper 延迟/挂了，过期 reservation
--    也不能继续占 cap。持 users row 锁后立即把该 user 所有
--    expires_at < now() 的 reserved 标 expired，再计数。
UPDATE express_clone_reservations
   SET status='expired', released_reason='ttl_expired', updated_at=now()
 WHERE user_id=:user_id AND status='reserved' AND expires_at < now();

-- 3. 幂等：查 active reservation（持锁 + stale 已 expire 后查）
SELECT * FROM express_clone_reservations
  WHERE user_id=:u AND job_id=:j AND speaker_id=:s AND status='reserved';
-- 命中 → 返回该 reservation（幂等，不重复占）；COMMIT。
-- NB: 上一步已 expire stale，所以这里**不会**幂等命中一条过期 reservation
--     （过期的同 job/speaker 会走下面新建，而非返回旧的 stale 行）。

-- 4. 原子 cap 检查（决策 4：count 含 active reservations）—— 持 users row 锁
--    stale 已在 step 2 expire，不再计入 count。
daily_count   = (今天 express_auto user_voices rows)
              + (今天 status='reserved' reservations)
active_temp   = (active 临时 user_voices)
              + (status='reserved' reservations)
IF daily_count >= daily_cap        → ROLLBACK; return 409 daily_cap_exceeded
IF active_temp >= active_temp_cap  → ROLLBACK; return 409 active_temp_cap_exceeded

-- 5. INSERT reservation
INSERT ... status='reserved', expires_at=now()+ttl;
COMMIT;  -- users row 锁随 transaction 结束自动释放
return 200 {reservation_id, status:"reserved", expires_at}
```

**并发原子性（PG）**：`SELECT id FROM users WHERE id=:user_id FOR UPDATE` 在 transaction 开头锁住该 user 的 users 行。同一 user 的 N 个并发 reserve 会在此处串行排队，逐个执行 stale-expire + cap 检查 + insert，只有 cap 个成功。锁随 transaction commit/rollback 自动释放。**PG-only**——sqlite 不支持行级 FOR UPDATE 阻塞语义（见 R5）。

**user 不存在 fail-closed（Codex v0.2）**：`SELECT ... FOR UPDATE` 查不到 user → ROLLBACK + 404 `user_not_found`，**不建 reservation**。pipeline 侧视为 skip + audit（`express_auto_clone_reserve_user_not_found`），**不进** upload/worker。理论上 pipeline 传的 user_id 来自 JobRecord（一定存在），但 internal endpoint 不能假设调用方可信——fail-closed。

**stale inline expire（Codex v0.2）**：TTL sweeper（§8）是后台兜底，但 reserve 时**不依赖**它——持锁后先把该 user 的过期 reserved 标 expired 再计数。这样即使 sweeper 挂了，过期 reservation 也不会占住 cap 让用户永远 reserve 不到。sweeper 仍保留（处理"创建了 reservation 但该 user 再也没发起新 reserve"的长期 stale）。

> **实现注意（两道防线）**：
> 1. **users row FOR UPDATE** 是并发串行化主防线（PG）
> 2. **partial unique index `uq_express_reservation_active`** 是幂等的**第二道防线**——即使锁逻辑有 bug，DB 唯一约束也会挡住同 (user,job,speaker) 的第二个 reserved 行（INSERT 冲突 → 捕获 IntegrityError → 返回已有 reservation，仍幂等）。这一层 sqlite 也支持，单测可验。

返回码：
- 200 `{ok:true, reservation_id, status:"reserved", expires_at}`（新建或幂等命中）
- 409 `{ok:false, deny_reason:"daily_cap_exceeded"|"active_temp_cap_exceeded"}`
- 404 `{ok:false, error:"user_not_found"}`（users row 查不到，fail-closed，不建 reservation）
- 400 invalid_user_id / invalid_job_id / invalid_speaker_id / target_model 校验（复用 PR1 regex + cosyvoice 自洽）
- 403 X-Internal-Key

### 4.2 `POST /api/internal/express-auto-clone-reservations/{reservation_id}/consume`

body：`{voice_id}`（register-smart 成功后返回的 voice_id）。

逻辑：`reserved → consumed`，填 `consumed_voice_id`。幂等：已 consumed 且 voice_id 相同 → 200；状态非 reserved（已 released/expired）→ 409 `reservation_not_reservable`（说明 TTL 已回收，调用方需重新 reserve 或放弃）。

### 4.3 `POST /api/internal/express-auto-clone-reservations/{reservation_id}/release`

body：`{reason}`。逻辑：`reserved → released`，填 `released_reason`。幂等：已 released/expired → 200（idempotent release）；已 consumed → 409 `reservation_already_consumed`（不能 release 已消费的）。

### 4.4 TTL sweeper（§8）

不暴露 endpoint；gateway lifespan 后台任务。

---

## §5 budget count 含 active reservations（决策 4 实现）

PR1 的两个 counter（`count_express_auto_clones_today` / `count_active_temporary_voices`）只查 `user_voices`。PR2 在 **reserve endpoint 内部**的 cap 检查要加上 reservation 计数：

```python
# 在 reserve transaction 内（不改 PR1 的 advisory budget GET）
daily_voices = await count_express_auto_clones_today(db, user_id)       # PR1
daily_reservations = await count_active_reservations_today(db, user_id) # PR2 新增
daily_total = daily_voices + daily_reservations

active_temp_voices = await count_active_temporary_voices(db, user_id)        # PR1
active_temp_reservations = await count_active_reservations(db, user_id)      # PR2 新增
active_temp_total = active_temp_voices + active_temp_reservations
```

**去重正确性**：reservation `consume` 后 status=consumed（不再被 `count_active_reservations*` 计入），同时它对应的 `user_voices` row 已写入（被 `count_*_voices` 计入）。所以一次 clone 在"reserved 阶段"算 reservation、"consumed 之后"算 voice，**不会双算**。临界点（consume 的瞬间）由 register→consume 的顺序保证：register 先写 user_voices row，consume 再把 reservation 标 consumed——极短时间窗内可能双算 1 次（保守，偏严，不会穿透 cap，可接受）。

**PR1 advisory budget GET 不变**：它仍只查 user_voices（advisory，不含 reservation）。PR2 不改它（主 spec §2.6a 明确它是 advisory）。pipeline 早期 fail-fast 用它（宽松短路 OK），最终闸用 reserve（含 reservation，严格）。

---

## §6 pipeline 调用链

### 6.1 模块组织

```
src/services/express/
├── __init__.py
├── main_speaker.py      # identify_express_main_speaker()（主 spec §4.2）
├── reservation_client.py # reserve/consume/release 的 HTTP client（requests + X-Internal-Key）
├── auto_clone.py        # run_express_auto_clone() —— 编排入口（锁死 §2.2 顺序）
└── audit.py             # emit_express_clone_audit()
```

`src/services/express/` 守卫：不 import gateway（D.7）、不 import boto3（NG5）。全程 HTTP 调 gateway internal endpoints。

### 6.2 `run_express_auto_clone` 调用顺序（锁死，§2.2）

```python
def run_express_auto_clone(*, user_id, job_id, project_dir, source_audio_path,
                           transcript_lines, speaker_voices, speaker_routing,
                           express_consent, usage_meter) -> tuple[bool, str]:
    # === L1-L4 gate（admin/worker/allowlist/consent；任一 fail → skip + audit）===
    # L1 admin flag（read_admin_setting express_cosyvoice_auto_clone_enabled）
    # L2 worker env（is_worker_enabled_in_env）
    # L3 allowlist（read_admin_setting user_allowlist；admin bypass 由 gateway 侧，
    #    pipeline 侧只读 allowlist 判断——但最终授权边界仍是 gateway）
    # L4 consent（express_consent.auto_voice_clone is True + server_confirmed_at 存在）

    # === L5 budget advisory（fail-fast 快速短路，NOT 最终闸）===
    # GET /api/internal/express-auto-clone-budget；can_clone=false → skip + audit
    # （明显超 cap 时省掉 L6/L7 的 CPU；但最终 gating 是 L7 之后的 reserve）

    # === L6 main speaker 识别 ===
    main_speaker_id = identify_express_main_speaker(transcript_lines, ...)
    # None → skip + audit（no_main_speaker / low_ratio）

    # === L7 sample extract + validate ===
    sample_path = VoiceSampleExtractor().extract_sample(...)
    validation = VoiceSampleExtractor().validate_sample(sample_path)
    # duration < 10s → skip + audit（sample_too_short）

    # === ★ ATOMIC RESERVE ★（最终成本闸；§2.2 顺序）===
    reservation = reservation_client.reserve(
        user_id, job_id, speaker_id=main_speaker_id, target_model="cosyvoice-v3.5-flash")
    # 409 cap_exceeded → skip + audit（daily_cap / active_temp_cap）；**不付费**

    try:
        # === upload sample（PR1 E1）===
        upload = upload_client.upload(sample_bytes, user_id, job_id, main_speaker_id)
        # 503 → _safe_release(reservation, "upload_failed") + skip + audit

        # === worker clone（付费）===
        clone_resp = worker_client.clone(WorkerCloneRequest(
            ..., sample=WorkerCloneSample(url=upload.presigned_get_url, sha256=upload.sha256),
            consent=WorkerCloneConsent(confirmed_at=express_consent["server_confirmed_at"])))
        # WorkerError/NetworkError → _safe_release(reservation, "worker_failed") + skip
        #   + audit（不重试，CLAUDE.md）

        # === register-smart（落库 user_voices）===
        register_ok = register_client.register(
            voice_id=clone_resp.voice_id, provider="cosyvoice_voice_clone",
            tts_provider="cosyvoice", platform="dashscope_mainland",
            requires_worker=True, target_model="cosyvoice-v3.5-flash",
            is_temporary=True, temporary_expires_at=now+7d, created_from="express_auto", ...)
        if not register_ok:
            # worker 已付费成功但 register 失败 → §7 孤儿清理
            worker_client.delete_voice(clone_resp.voice_id, ...)  # best-effort
            _safe_release(reservation, "register_failed")
            return False, "register_failed_orphan_cleanup_{ok|failed}"  # + audit

        # === consume reservation（成功路径）===
        reservation_client.consume(reservation.id, voice_id=clone_resp.voice_id)

        # === routing 注入（主 spec §6）===
        speaker_voices[main_speaker_id] = clone_resp.voice_id
        speaker_routing[main_speaker_id] = {"requires_worker": True,
                                            "worker_target_model": "cosyvoice-v3.5-flash"}
        usage_meter.record_voice_clone(provider="cosyvoice_voice_clone", ...)
        return True, f"cloned_{clone_resp.voice_id}"
    except Exception:
        _safe_release(reservation, "unexpected_error")
        return False, "unexpected_error"  # + audit


# _safe_release：release 自身失败不静默（Codex v0.1）
def _safe_release(reservation, reason):
    try:
        reservation_client.release(reservation.id, reason=reason)
    except Exception as exc:
        # 不吞：写 audit reservation_release_failed；TTL sweeper 仍会回收
        emit_express_clone_audit(
            reason_code="express_auto_clone_reservation_release_failed",
            reservation_id=reservation.id, details={"release_error": type(exc).__name__})
```

### 6.3 process.py 调用点

主 spec §6.1 已定位：S2 阶段后、translation 前的 Express 分支（`process.py:3186-3190` 现在打印"快捷模式：跳过音色库查找和自动克隆"）。PR2 在那里插 `run_express_auto_clone(...)` 调用，原地修改 `_speaker_voices` / `_speaker_voice_routing`。下游 segments persistence（`process.py:8097-8139`，commit 0ba02c7）自动接管 routing。

---

## §7 失败降级 + 孤儿清理

| 失败点 | reservation 动作 | clone 副作用清理 | audit reason_code |
|---|---|---|---|
| L1-L7 任一 gate fail | （还没 reserve）无 | 无 | `express_auto_clone_<layer>_*` |
| reserve 409 cap_exceeded | （reserve 本身失败）无 | 无 | `express_auto_clone_reserve_daily_cap` / `_active_temp_cap` |
| upload 503 | **必 release** | 无（还没 clone） | `express_auto_clone_upload_failed_*` |
| worker clone 失败 | **必 release** | 无（worker 没成功） | `express_auto_clone_worker_*` |
| register 失败（worker 已成功）| **必 release** | **best-effort `delete_voice`**（清 DashScope 孤儿） | `express_auto_clone_register_failed_orphan_cleanup_{ok\|failed}` |
| **release 自身失败**（reservation_client.release 抛/非 200）| 已尽力，记审计待 TTL 兜底 | — | **`express_auto_clone_reservation_release_failed`**（不静默；TTL sweeper 仍会回收该 reserved 名额） |
| 进程崩溃（reserve 后任意点）| TTL sweeper 兜底 expired | worker voice 可能成孤儿（Phase 4.3b 音色 sweeper / 人工） | （崩溃无 audit；sweeper 写 expired） |

**核心约束**（与主 spec §7 一致）：
- 任何失败 → 回 CosyVoice 预设音色（pipeline 继续，segments 走 voice matcher），**不**调 MiniMax（NG2）
- worker clone **不重试**（CLAUDE.md 付费 API + client.py max_attempts=1）
- `delete_voice` 是对**自己刚创建**资源的 rollback，不算违反"不静默调付费 API"（与主 spec §7.3 一致）
- **release 失败不静默（Codex v0.1）**：`reservation_client.release(...)` 自身失败（网络/gateway 5xx）→ **必写 audit `express_auto_clone_reservation_release_failed`**（含 reservation_id），不能吞掉。该 reserved 名额由 TTL sweeper（§8）在 `expires_at` 后回收，所以不会永久泄漏 cap，但审计必须留痕以便排障"为什么这个 user 的 cap 短期被占"。

---

## §8 reservation TTL sweeper

gateway lifespan 后台任务（参照现有 `r2_artifact_sweeper` / `editing_idle_scanner` 模式）：

- 周期（建议 5 分钟）扫 `status='reserved' AND expires_at < now()`（用 partial index `idx_express_reservation_ttl_pending`）
- 每行 `reserved → expired`，`released_reason="ttl_expired"`
- **幂等**：选行条件 `status='reserved'`，重跑跳过已处理行
- **只动 reservation 表**，不碰 user_voices / 不调任何付费 API（纯 DB 状态流转）
- 单批 cap（如 200 行）防长事务

**注意**：reservation TTL sweeper ≠ 音色 `temporary_expires_at` sweeper（Phase 4.3b）。前者回收"预占未消费"的额度名额（纯 DB）；后者删 DashScope 临时音色（付费 delete）。两者独立。

---

## §9 audit

`<project_dir>/audit/express_decisions.jsonl`（主 spec §9 schema）+ PR2 新增字段：

```json
{
  "kind": "express_auto_clone_decision",
  "phase_version": "4.3a-pr2",
  "decision": "cloned" | "skipped" | "register_failed_orphan_cleanup_ok" | "register_failed_orphan_cleanup_failed",
  "reason_code": "...",
  "reservation_id": "...",          // PR2 新增
  "reservation_status_final": "consumed" | "released" | null,  // PR2 新增
  "main_speaker_id": "...", "main_speaker_ratio": 0.73,
  "voice_id": "...", "worker_request_id": "...",
  "express_consent_server_at": "...",
  ...
}
```

---

## §10 测试守卫（决策 5 + 全链路）

### 10.1 reservation 状态机 / 幂等 / 约束（真 aiosqlite 单测）

> **范围（Codex v0.1 R5）**：sqlite 不支持 users row `FOR UPDATE` 的阻塞语义，
> 所以**并发原子性**（N 并发 reserve 串行）放 §10.7 真 PG。sqlite 单测覆盖
> **状态机 + 幂等约束 + 计数去重 + sweeper**（不依赖行锁阻塞的部分）。

- `test_reserve_idempotent_same_job_speaker` — 同 (user,job,speaker) reserve 两次返回**同一** reservation_id（partial unique + 先查后插）
- `test_reserve_partial_unique_index_blocks_second_active` — DB 层唯一约束兜底（第二个 reserved INSERT → IntegrityError → 返回已有）
- `test_reserve_cap_exceeded_sequential` — 顺序（非并发）reserve 到 cap 后第 N+1 个返 409（cap 检查逻辑本身，不测并发）
- `test_consume_transitions_reserved_to_consumed` / `test_release_transitions_reserved_to_released`
- `test_consume_already_released_returns_409` / `test_release_already_consumed_returns_409`
- `test_count_includes_active_reservations` — 决策 4：reserve 后 count 含它；consume 后转 user_voices 不双算
- **`test_reserve_inline_expires_stale_before_count`**（Codex v0.2）— 构造一条 stale reserved（expires_at < now）+ cap 已满 → reserve 应先 expire stale 腾出名额，新 reserve 成功（stale 不占 cap，不依赖 sweeper）
- **`test_reserve_stale_same_key_not_idempotent_reused`**（Codex v0.2）— 同 (user,job,speaker) 的 stale reserved → reserve **不**幂等返回旧 stale，而是 expire 旧的 + 新建一条（新 reservation_id ≠ 旧的）
- **`test_reserve_unknown_user_no_insert`**（Codex v0.2）— user_id 不存在 → 404 user_not_found + reservation 表**无**新行

### 10.2 pipeline 调用顺序守卫（决策 5）

- `test_reserve_called_before_upload_and_worker` — AST/mock：`run_express_auto_clone` 里 reserve 调用在 upload/worker.clone **之前**（注意：sample extract/validate 在 reserve **之前**，本地 CPU 不占名额——守卫断言 reserve 在 upload/worker 前，**不**断言 reserve 在 sample 前）
- `test_sample_validate_before_reserve` — 反向：sample extract+validate 在 reserve **之前**（sample fail 时不占 reservation 名额）
- `test_no_worker_without_successful_reserve` — reserve 409 → upload/worker **不**被调用
- `test_register_success_triggers_consume` — register ok → consume 被调
- `test_worker_success_register_fail_triggers_delete_and_release` — register 失败 → delete_voice + release 都被调
- `test_upload_fail_releases_reservation` — upload 503 → release 被调，worker 不被调
- `test_release_failure_emits_audit_not_silent` — `reservation_client.release` 抛异常 → audit 写 `express_auto_clone_reservation_release_failed`（_safe_release 不吞）
- `test_pipeline_never_uses_advisory_get_as_final_gate` — AST 扫 auto_clone.py：advisory budget GET 之后必有 reserve 才进 worker（不允许 advisory→worker 直连）

### 10.3 失败降级

- `test_any_failure_falls_back_to_preset_not_minimax` — 各失败点断言 speaker_voices 不被改成 clone voice + 不调 MiniMax
- `test_worker_clone_not_retried` — worker client max_attempts=1（PR1 已有，PR2 复用）

### 10.4 TTL sweeper

- `test_sweeper_expires_stale_reserved` — reserved + expires_at<now → expired
- `test_sweeper_idempotent` — 重跑不重复处理
- `test_sweeper_does_not_touch_consumed_or_released` — 只动 reserved
- `test_sweeper_no_paid_api_call` — sweeper 不调 delete_voice / worker

### 10.5 跨 phase regression

- PR1 全套 165 守卫继续绿
- Smart / Studio clone 字节级不变（PR1 守卫）
- `src/services/express/*` 不 import gateway / boto3（新 AST 守卫）

### 10.6 Mock 策略

- reservation 状态机 / 幂等 / 计数 / sweeper：真 in-memory aiosqlite（§10.1/§10.4）
- reservation **并发原子性**：真 PG（§10.7）—— sqlite 无行锁阻塞语义
- pipeline `run_express_auto_clone`：mock reservation_client / upload_client / worker_client / register_client / VoiceSampleExtractor（断言调用顺序 + 失败分支），**不**起真 worker / OSS / DashScope

### 10.7 PG 并发 integration 测试（Codex v0.1 要求）

**新文件**：`tests/test_phase43a_pr2_reservation_pg_atomic.py`

**为什么必须真 PG**：atomic reservation 的核心是 users row `FOR UPDATE` 串行化并发 reserve。sqlite `aiosqlite` 不支持该阻塞语义（`FOR UPDATE` 被忽略），用 sqlite 测并发是假绿。Codex 明确：不用 PG 跑并发锁测试，review 会被打回。

**只测 2 件事**（最小集，CI 单独 job 跑这一个文件）：

- `test_pg_concurrent_reserve_cap_one_only_one_wins` — cap=1 时 N（如 10）个并发 reserve **同一 user 不同 (job,speaker)**（绕过幂等，测真 cap 竞态）→ 断言**恰好 1 个** 200 reserved，其余 9 个 409 cap_exceeded
- `test_pg_concurrent_reserve_same_key_idempotent` — N 个并发 reserve **同一 (user,job,speaker)** → 断言全部返回**同一** reservation_id（幂等 + partial unique 在并发下不重复建）

**CI 接入**（PR2-G 落地）：
- 现有 CI 普通 pytest job **不变**（不依赖 PG，跑全仓单测含 §10.1-§10.6）
- 新增独立 CI job（`backend-pg-integration`）：起 `postgres` service container，按现有 CI 风格装依赖（**不是** `.[dev]`——`pyproject [dev]` 只有 pytest/pytest-asyncio/aiosqlite，asyncpg 在 `gateway/requirements.txt`）：
  ```bash
  python -m pip install -r requirements-dev.txt
  python -m pip install -r gateway/requirements.txt   # 含 asyncpg==0.31.0
  pytest -q tests/test_phase43a_pr2_reservation_pg_atomic.py
  ```
- 该文件用 `pytest.mark.skipif(no PG env)`，本地无 PG 时跳过（不阻塞本地全仓 pytest）；CI PG job 里有 `DATABASE_URL` 指向 service postgres 才真跑
- 连接：复用 gateway 的 async engine 构造（asyncpg），建临时 schema / 表，测完 drop

---

## §11 不做事项

| 项 | 原因 |
|---|---|
| ❌ 前端 consent checkbox / availability wiring | PR3 |
| ❌ 开启 Express auto-clone（admin flag 翻 True） | PR3 之后灰度阶段 |
| ❌ 部署 / 跑 migration 到 prod | 用户单独授权 |
| ❌ 音色 temporary_expires_at 批量 sweeper | Phase 4.3b（与 reservation TTL sweeper 不同） |
| ❌ 多主说话人克隆 | 主 spec NG5；只主说话人 |
| ❌ 改 PR1 advisory budget GET | 主 spec §2.6a 明确它是 advisory；PR2 加独立 reserve |
| ❌ 在 app 容器装 boto3 / SQLAlchemy | D.7 / NG5；pipeline 全程 HTTP |

---

## §12 实施分阶段

| 阶段 | 内容 | 估时 |
|---|---|---|
| **PR2-A** | migration 032 reservation 表 + ORM model + admin_settings `reservation_ttl_minutes` 字段（validator 5-120 + full-body save 守卫）+ schema 守卫 | 4-5h |
| **PR2-B** | reservation service 函数（reserve 锁 users row + 幂等 + consume/release）+ count_active_reservations* | 5-7h |
| **PR2-C** | 3 个 reservation endpoints（reserve/consume/release）+ sqlite 行为测试（状态机/幂等/约束/计数，§10.1） | 5-7h |
| **PR2-C-pg** | PG 并发 integration 测试 `tests/test_phase43a_pr2_reservation_pg_atomic.py` + CI 独立 postgres job（§10.7） | 3-4h |
| **PR2-D** | reservation TTL sweeper（lifespan 任务）+ 守卫 | 3-4h |
| **PR2-E** | `src/services/express/{main_speaker,reservation_client,auto_clone,audit}.py` + 调用顺序守卫（§10.2，含 _safe_release / sample-before-reserve / reserve-before-worker） | 8-12h |
| **PR2-F** | process.py Express 分支接 `run_express_auto_clone` + routing 注入 + 跨 phase regression | 4-6h |
| **PR2-G** | 全套测试 + Codex review 迭代 | 6-10h |

**总估时**：~38-55 工时（约 6-8 工作日 + review）

**依赖顺序**：A → B → C →（C-pg 并行）→ D（并行 E）→ F → G

---

## §13 风险矩阵

| # | 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|---|
| R1 | advisory GET 与 atomic reserve 计数口径不一致导致困惑 | M | M | §5 明确：advisory 只查 voices（宽松短路），reserve 查 voices+reservations（严格闸）；文档 + 测试锁口径 |
| R2 | consume/register 之间双算 cap（临界窗）| L | L | §5：register 先写 voice，consume 再标 reservation；极短窗偏严不穿透，可接受 |
| R3 | 进程崩溃留 reserved 孤儿占额度 | M | M | TTL sweeper（§8）30 分钟回收；worker 已付费的 voice 孤儿留 Phase 4.3b 音色 sweeper |
| R4 | advisory→worker 直连绕过 reserve（实施漂移）| M | High | §10.2 调用顺序守卫 AST 扫 + mock 断言 |
| R5 | users row `FOR UPDATE` 在 sqlite 测试无阻塞语义 | H | L | v0.1：并发原子性单独放真 PG 测试（§10.7，CI 独立 job 起 postgres）；sqlite 单测（§10.1）覆盖状态机 / 幂等 / partial unique 约束 / 计数 / sweeper（不依赖行锁阻塞的部分）；partial unique index 是 sqlite 也支持的第二道幂等防线 |
| R6 | reservation 表与 user_voices count 去重错误导致穿透 | L | High | §5 去重正确性论证 + `test_count_includes_active_reservations` |

---

## §14 实施前自检

- [ ] 用户已审 §1 范围（PR2 = reservation + pipeline，不做前端）
- [ ] 用户已审 §2 五决策
- [ ] 用户已审 §3 reservation 表 schema（migration 032）
- [ ] 用户已审 §4 endpoints + §5 count 含 reservation
- [ ] 用户已审 §6 pipeline 调用顺序（reserve 在付费前）
- [ ] 用户已审 §7 失败降级 + 孤儿清理
- [ ] 用户已审 §10 测试守卫（调用顺序 + §10.7 PG 并发）
- [ ] **v0.1 闭合**（Codex 一轮）：reserve 顺序统一 sample-validate 后/upload-worker 前（§1.3 #4 + §2.2 + §6.2 + §10.2 一致）；TTL 30min + validator 5-120（§3）；lock 锁 users row 非 advisory hash（§4.1）；release 失败记 audit 不静默（§7 + §6.2 _safe_release）；PG 并发测试 §10.7 + CI 独立 job
- [ ] **v0.2 闭合**（Codex 二轮）：
  - reserve transaction 内 inline expire 当前 user 的 stale reserved（`expires_at < now`）再计数，不依赖 sweeper（§4.1 step 2 + 测试 `test_reserve_inline_expires_stale_before_count` / `test_reserve_stale_same_key_not_idempotent_reused`）
  - `users FOR UPDATE` 查不到 → 404 `user_not_found` fail-closed，不建 reservation（§4.1 step 1a + 测试 `test_reserve_unknown_user_no_insert`）
  - §10.7 CI 安装命令改为 `requirements-dev.txt` + `gateway/requirements.txt`（不写 `.[dev] 含 asyncpg`）
  - reservation 表归属 `gateway/alembic/versions/032_*.py`（down_revision='031'）+ ORM `gateway/models.py`
- [ ] @codex review 此 spec v0.2
- [ ] Codex 反馈纳入后再开 PR2 实施
