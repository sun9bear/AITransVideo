# Phase 4.3b — 临时音色清理 sweeper spec

**作者：** Claude (Opus 4.7 / 1M)
**版本：** v0.3（Codex 二轮 review 修订；待三轮，未实施）
**日期：** 2026-05-28
**变更摘要 v0.2 → v0.3（Codex 二轮 2 P1 + 2 P2，DoD/范围一致性）：**
- **P1-1**：§1.3 DoD #2 残留"已不存在视为成功"旧语义 → 与 §2.4 对齐：仅 A0 错误码映射落地且识别稳定 `voice_not_found`/`voice_already_deleted` code 才算成功；未知 `delete_voice_failed` 一律失败重试。
- **P1-2**：§1.3 DoD #1 selector 补 `cleanup_claim_until IS NULL OR cleanup_claim_until < now()`（claim lease 是验收口径，守卫必须覆盖，否则并发防线漏测）。
- **P2-1**：§1.1 G1 schema 范围 3 字段 → **5 字段**（含 `cleanup_claim_until`/`cleanup_run_id`），与 §3 同步。
- **P2-2**：§2.7 定义 `CLEANUP_CLAIM_LEASE_SECONDS=600` 默认 + 守卫（不得小于 `delete_voice` 最坏重试窗口）；**worker 不可用在 claim 前 fail-fast**（不先认领再 skip，避免认领行空等 lease 过期）。

**变更摘要 v0.1 → v0.2（Codex 一轮 2 P1 + 2 P2）：**
- **P1-1 并发 claim/lease（§2.7 新决策 + §3 + §4 重写）**：v0.1 是裸 "select→loop delete"，多 gateway 实例 / auto sweeper + manual cleanup 同跑会重复选中同一行 → 重复付费 `delete_voice`。v0.2 改 **claim-lease 两阶段**：短事务用 `SELECT … FOR UPDATE SKIP LOCKED` 原子认领一批（写 `cleanup_claim_until` + `cleanup_run_id` lease）→ COMMIT 释放行锁 → **事务外**逐行调 worker（不跨外部调用持锁）→ 完成更新用 `cleanup_run_id` 守卫。+ PG 并发 integration 测试（sqlite 测不了 SKIP LOCKED）。
- **P1-2 "已不存在视为成功"不能凭假设（§2.4 重写）**：真实代码 `RealCosyvoiceProvider.delete_voice`（`real_cosyvoice.py:257-278`）把**任意 SDK 异常**统一包成 `ProviderError(code="delete_voice_failed")`，client 抛 `WorkerError`——**没有** 稳定的 `voice_not_found` code。v0.2 默认：**所有 delete 失败一律重试**（无 already-gone 捷径）；already-gone 归一为成功是**可选优化**，前置依赖"先设计 + 测试 worker/provider 错误码映射（provider 返回稳定 `voice_not_found`）"（§11 列为前置子任务，默认关闭）。
- **P2-1 admin 手动清理边界（§2.6 / §4.3 收紧）**：后台按钮**不得**走 `/api/internal` + `X-Internal-Key`（浏览器不该持 internal key）。v0.2 默认 **4.3b 只做 CLI/manual script**；若要 UI，必须 admin-auth + CSRF 的 `/api/admin/...`（绝不 internal-key）。give-up retry 明确 `--include-give-up` / `--reset-attempts` 机制。
- **P2-2 索引名修正**：`idx_user_voices_temp_expires_pending` → 真实 `idx_user_voices_temp_expires_pending`（`gateway/models.py:704`）。
**前置：** Phase 4.3a（PR1+PR2+PR3）已 merge（origin/main `5704aaa3`）。Express auto-clone 全链路就位但 admin 主开关默认 OFF、未灰度。
**主 spec：** [`2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md`](2026-05-28-phase43a-express-cosyvoice-auto-clone-spec.md)（§NG6 把音色清理 sweeper 列为 4.3b）

---

## §0 背景：4.3a 留下的债

4.3a 的 Express auto-clone 把克隆音色写成**临时音色**：`is_temporary=TRUE` +
`temporary_expires_at=now()+7d`。但 **`temporary_expires_at` 目前只是元数据**——
没有任何代码读它去删 DashScope 上的临时 voice。后果：

- Express 灰度一旦开启，DashScope 临时 voice **只增不减**，累积占用账户 voice 配额 + 潜在费用。
- 现有 `mark_voice_expired()` / `delete_user_voice()` 只翻 DB `expired_at`（软删），
  **不调 worker 删 DashScope voice** → 直接调它们清理 cosyvoice 临时音色会**留 DashScope 孤儿**。
- PR3 文案已据此**不承诺删除时限**（"系统后续按清理策略处理"）——这条清理策略就是 4.3b。

**4.3b = 把 `temporary_expires_at` 从元数据变成真实清理动作**：到期 → 删 DashScope voice（付费/外部）→ 软删 DB 行。

> ⚠️ **付费 API 边界（CLAUDE.md）**：`delete_voice` 是对**系统自己创建**的 worker voice 的 rollback/lifecycle 清理，**不是** fallback 路径调付费 API——属允许范畴（与 4.3a §7.3 孤儿清理同性质）。但仍必须有 dry-run / batch limit / audit / fail-safe（§6）。

---

## §1 范围 / 非范围

### 1.1 范围（4.3b in-scope）

- **G1 schema（migration 033）**：UserVoice 加 **5** 个清理追踪字段——`cleanup_attempts` / `cleanup_retry_after` / `cleanup_last_error`（backoff + give-up）+ `cleanup_claim_until` / `cleanup_run_id`（并发 claim-lease，§2.7），**不**新表。
- **G2 清理函数**：`cleanup_expired_temporary_voices(db, *, dry_run, limit)`——选到期临时 cosyvoice 音色 → 删 worker voice → 软删 DB 行；失败保留可重试状态。
- **G3 sweeper**：gateway lifespan 后台任务（参照 PR2 `express_reservation_sweeper`），周期跑 G2，fail-safe，默认保守。
- **G4 admin 手动清理入口**：admin 触发的一次性清理 + dry-run 预览（与自动 sweeper 共用 G2 核心，边界清晰）。
- **G5 audit**：每次删除（成功/失败/dry-run）落 audit，含 voice_id / worker_request_id / 结果。
- **G6 守卫测试**：mock worker（**绝不真调** DashScope）+ 状态机 + 失败不硬删 + dry-run + 与 reservation sweeper 区分。

### 1.2 非范围（4.3b out-of-scope）

- **NG1 不动 reservation TTL sweeper**（PR2）：那个只翻 reservation 表、不调 worker；本 sweeper 调 `MainlandWorkerClient.delete_voice`（付费）。两者独立（§8）。
- **NG2 不清非临时音色**：`is_temporary=FALSE`（用户永久库 / Studio 手动 clone）**绝不**碰。
- **NG3 不清非 cosyvoice 音色**：只清 `provider='cosyvoice_voice_clone' AND requires_worker=TRUE`（有 DashScope worker voice 可删的）；MiniMax 等其它 provider 不在本 sweeper 范围。
- **NG4 不部署、不跑 alembic**：migration 033 随分支落，本阶段不 apply。
- **NG5 不开启 Express auto-clone 主开关**：4.3b 与灰度解耦——sweeper 即便上线，无临时音色时就是 no-op。
- **NG6 不改 4.3a 的 `temporary_expires_at=now+7d` 默认**：清理周期由 sweeper 读 admin/常量，不改写入端。

### 1.3 验收 DoD

1. ✅ sweeper 只选 `provider='cosyvoice_voice_clone' AND is_temporary=TRUE AND requires_worker=TRUE AND temporary_expires_at < now() AND expired_at IS NULL AND cleanup_attempts < MAX AND (cleanup_retry_after IS NULL OR < now()) AND (cleanup_claim_until IS NULL OR cleanup_claim_until < now())`（**claim lease 条件是验收口径**，守卫必覆盖，否则并发防线漏测）
2. ✅ 每个到期音色：先 `worker.delete_voice` **成功** → 才 `mark_voice_expired`（写 `expired_at`）→ 行从 select 集剔除。**"已不存在视为成功"仅在 A0 错误码映射落地、且识别到稳定 `voice_not_found`/`voice_already_deleted` code 时成立**；未知 `delete_voice_failed` 一律**失败重试**（§2.4），绝不软删
3. ✅ worker delete **失败** → **不**写 `expired_at`（不硬删）、`cleanup_attempts += 1` + `cleanup_retry_after = now + backoff` + `cleanup_last_error`；达 MAX → give-up（停止自动重试，留 manual）
4. ✅ **dry-run**：选行 + 报告"将删哪些"，**不**调 worker、**不**改 DB
5. ✅ batch limit（单批 cap）防长事务 + 防一次性打爆 worker
6. ✅ sweeper 异常不崩 gateway / 不崩 loop（fail-safe，参照 PR2 sweeper）
7. ✅ 测试全程 mock worker，**0** 真实 DashScope 调用
8. ✅ 与 reservation TTL sweeper 模块 / 职责清晰区分（守卫）

---

## §2 关键决策

### 2.1 决策 1：两步终态清理——先删 worker，再软删 DB（单一权威路径）✅

cosyvoice 临时音色的终态清理**必须**两步且**按序**：

```
worker.delete_voice(voice_id)  成功/幂等已删
        ↓
mark_voice_expired(expired_at=now)  软删 DB 行
```

**不能颠倒**：若先写 `expired_at` 再删 worker，worker 删失败 → DB 说已过期、select 集剔除 → DashScope 永久孤儿（再也扫不到）。先删 worker、删成功才软删，保证"DB 标过期" ⟺ "DashScope 已删"。

**`mark_voice_expired` 单独使用的风险（与 terminal-state-single-entry 教训同源）**：现有 `mark_voice_expired` / `delete_user_voice` 只翻 `expired_at`、不删 worker。**任何路径**对 cosyvoice 临时音色调它们而不先删 worker → 留孤儿。4.3b 把"cosyvoice 临时音色清理"收敛到 G2 单一入口（删 worker + 软删原子配对）；其它直接调 `mark_voice_expired` 的旧路径**不得**用于 cosyvoice 临时音色（守卫 + 文档锁）。

### 2.2 决策 2：失败永不硬删 / 永不写 expired_at；backoff + give-up ✅

worker delete 失败（网络 / worker 5xx / DashScope 拒绝）→：

- **不**写 `expired_at`（行保持 active 可重试，Codex 重点）
- `cleanup_attempts += 1`，`cleanup_retry_after = now + backoff(attempts)`，`cleanup_last_error = <code>[:200]`
- 下次 sweep 过了 `cleanup_retry_after` 才重试（避免对永久失败行每周期狂调 worker）
- `cleanup_attempts >= MAX_CLEANUP_ATTEMPTS`（如 5）→ **give-up**：select 集排除（`cleanup_attempts < MAX`），落 audit `cleanup_give_up`，转入 §4.3 manual 处理

### 2.3 决策 3：dry-run 默认优先（首次灰度先观察）✅

`cleanup_expired_temporary_voices(dry_run=True)` 只选行 + 报告，不调 worker、不改 DB。
- sweeper 首次上线 / admin 首次手动清理建议先 dry-run，确认选中集合合理再实跑。
- env / admin setting 可让自动 sweeper 起步先跑 dry-run 模式（只 log "would delete N"），观察期后再放开实删。

### 2.4 决策 4：失败默认全部重试；already-gone 归一为成功是**可选**前置优化（Codex 一轮 P1-2）✅

**默认（v0.2）：所有 `delete_voice` 失败一律视为失败 → backoff 重试（§2.2）。不假设任何"已不存在=成功"捷径。**

理由（查真实代码）：`RealCosyvoiceProvider.delete_voice`（`src/services/mainland_worker/worker/providers/real_cosyvoice.py:257-278`）把**任意 SDK 异常**统一包成 `ProviderError(code="delete_voice_failed", retryable=_retryable_keywords(...))`；`MainlandWorkerClient.delete_voice` 在非 ok 时抛 `WorkerError`（`client.py:348/592`）。**当前没有** 稳定的 `voice_not_found` / `voice_already_deleted` code 可识别。凭"404=成功"假设会把真实失败误判成功 → 软删 DB 但 DashScope voice 还在 → 反向孤儿。

**already-gone 优化（可选，前置依赖）**：若要支持"voice 已被 out-of-band 删除 → 归一成功避免无谓重试到 give-up"，**必须先**做 worker/provider 错误码映射子任务（§11 前置）：
- `real_cosyvoice.py` 识别 SDK "not found" 类异常 → emit 稳定 `code="voice_not_found"`（retryable=False）
- client 把该 code 透到 `WorkerError.code`
- G2 仅当识别到该**稳定 code** 才归一成功；未知 `delete_voice_failed` **一律失败重试**
- 该映射须有独立单测（mock SDK 抛 not-found → 断言 code）

在该映射落地前，4.3b 行为 = "已不存在的 voice 也重试到 give-up → 转 manual"（保守、无双删、只是多几次无谓重试，可接受）。

### 2.7 决策 7：并发认领 claim-lease（FOR UPDATE SKIP LOCKED）防重复付费删除（Codex 一轮 P1-1）✅

裸 "select → loop delete" 在 **多 gateway 实例 / auto sweeper + manual cleanup 同跑** 时会重复选中同一行 → 重复调付费 `delete_voice`。v0.2 用 **claim-lease 两阶段**：

1. **认领（短事务）**：`SELECT … FOR UPDATE SKIP LOCKED LIMIT batch` 原子选一批未被锁/未被 lease 的行 → `UPDATE` 写 `cleanup_claim_until = now()+LEASE` + `cleanup_run_id = <本次 run uuid>` → **COMMIT**（立即释放行锁）。`SKIP LOCKED` 保证两个 runner 不会认领同一行。
2. **处理（事务外，逐行）**：对认领到的行调 `worker.delete_voice`（付费/外部）——**绝不**跨外部调用持有 DB 事务/行锁（避免长事务 + 连接占用）。
3. **完成 / 失败更新**：成功 → `UPDATE … SET expired_at=now(), cleanup_claim_until=NULL WHERE id=:id AND cleanup_run_id=:run_id AND expired_at IS NULL`（`run_id` 守卫：若本 runner lease 已过期被别的 runner 重认领，本次更新 no-op、不 clobber）；失败 → 释放 lease + backoff（§2.2）。

- **LEASE 默认 = `CLEANUP_CLAIM_LEASE_SECONDS = 600`（10 min）**（常量，env 可调）。**守卫**：LEASE 必须 **>** `MainlandWorkerClient.delete_voice` 的最坏重试窗口（`max_network_retries × (per-attempt timeout + backoff)`）——单测断言 `CLEANUP_CLAIM_LEASE_SECONDS >= <worst-case delete window 的安全下界常量>`，防有人把 LEASE 调到比单行处理还短 → 处理中被重认领 → 双删。
- **lease 过期自愈**：runner 崩溃 / 卡住 > LEASE → 行的 `cleanup_claim_until` 过期 → 下个 runner 重新认领（select 过滤 `cleanup_claim_until IS NULL OR < now()`）。LEASE > 单行最坏处理时间 → 把"卡住 runner 仍在调用时被重认领 → 双删"概率压到极低。
- **worker 不可用在 claim 之前 fail-fast**：每轮**先**确认 `build_client_from_env()` 非 None（worker 已配置/启用），**否则整轮 skip、不认领任何行**。绝不"先认领再发现 worker 不可用 → skip"——那会把认领行 lease 占住、白等 LEASE 过期才能被别处理。dry-run 不受此限（dry-run 不调 worker）。
- **PG-only**：`FOR UPDATE SKIP LOCKED` 是 PG 语义，sqlite 忽略 → 并发原子性放真 PG integration 测试（§7，参照 PR2 §10.7）；sqlite 单测覆盖 claim→process→complete 状态机 + run_id 守卫逻辑。

### 2.5 决策 5：与 reservation TTL sweeper 严格区分 ✅

| | reservation TTL sweeper（PR2） | temporary voice sweeper（4.3b） |
|---|---|---|
| 模块 | `gateway/express_reservation_sweeper.py` | `gateway/express_voice_cleanup_sweeper.py`（新） |
| 操作 | 翻 `express_clone_reservations.status` → expired（纯 DB，免费） | 删 DashScope voice（**付费/外部**）+ 软删 `user_voices` |
| 调 worker？ | **绝不** | **是**（`MainlandWorkerClient.delete_voice`） |
| 表 | `express_clone_reservations` | `user_voices` |

守卫：reservation sweeper 不 import worker（PR2 已锁）；4.3b sweeper 调 worker 但有 dry-run/batch/backoff。两模块互不复用、职责不混。

### 2.6 决策 6：admin 手动清理 = CLI/manual script（不走 /api/internal）；give-up 显式重试（Codex 一轮 P2-1）✅

- **自动 sweeper**（G3）：周期跑、保守、可 dry-run 起步。运维零干预的常态回收。
- **admin 手动清理**（G4）：**4.3b 默认只做 CLI / manual script**（如 `python -m scripts.cleanup_temp_voices --dry-run`），用于灰度观察 / give-up 行兜底 / 紧急回收。CLI 在服务器进程内、天然持有 internal/DB 凭据，无浏览器暴露问题。
- **不做后台按钮走 `/api/internal`**：浏览器**不该**持 `X-Internal-Key`。若未来要 UI 按钮，**必须** admin-auth + CSRF 的 `/api/admin/...` endpoint（与现有 admin 路由同款鉴权），**绝不**复用 internal-key 路径。4.3b 先不引入该 UI。
- **give-up 行重试机制（明确）**：give-up 行（`cleanup_attempts >= MAX`）默认被 select 排除（`cleanup_attempts < MAX`）。CLI 提供两个显式开关让"admin 可重试"落地：
  - `--include-give-up`：select 时**不**加 `cleanup_attempts < MAX` 过滤（把 give-up 行也纳入本次清理）
  - `--reset-attempts`：清理前把目标行 `cleanup_attempts=0, cleanup_retry_after=NULL, cleanup_last_error=NULL`（重置重试计数）
  - 两者都默认 False（自动 sweeper 永不带）；只有 admin CLI 显式传才生效。
- 自动 + 手动**共用 G2 核心**（同 claim-lease，§2.7）→ 并发安全一致、行为一致、不重复实现。

---

## §3 schema（migration 033）

UserVoice 加 5 个清理追踪列（**不新表**；复用现有 `expired_at` 软删 + `idx_user_voices_temp_expires_pending` 部分索引）：

```sql
ALTER TABLE user_voices
  ADD COLUMN cleanup_attempts    INTEGER      NOT NULL DEFAULT 0,
  ADD COLUMN cleanup_retry_after TIMESTAMPTZ  NULL,
  ADD COLUMN cleanup_last_error  VARCHAR(200) NULL,
  -- 决策 7 并发 claim-lease：
  ADD COLUMN cleanup_claim_until TIMESTAMPTZ  NULL,   -- 认领租约到期时刻
  ADD COLUMN cleanup_run_id      VARCHAR(36)  NULL;   -- 认领者 run uuid（完成更新守卫）
```

down_revision = `032_express_clone_reservations`（4.3b 是 4.3a PR2 之后的 migration head）。

**认领 SQL（短事务，FOR UPDATE SKIP LOCKED；决策 7 phase 1）**：

```sql
BEGIN;
SELECT id, voice_id, user_id, source_job_id
  FROM user_voices
 WHERE provider = 'cosyvoice_voice_clone'
   AND requires_worker = TRUE
   AND is_temporary = TRUE
   AND temporary_expires_at < now()
   AND expired_at IS NULL
   AND cleanup_attempts < :MAX_CLEANUP_ATTEMPTS
   AND (cleanup_retry_after IS NULL OR cleanup_retry_after < now())
   AND (cleanup_claim_until IS NULL OR cleanup_claim_until < now())   -- 未被他人 lease
 ORDER BY temporary_expires_at ASC
 LIMIT :batch_limit
 FOR UPDATE SKIP LOCKED;          -- 原子跳过他人正持有的行（PG-only）
UPDATE user_voices
   SET cleanup_claim_until = now() + :LEASE, cleanup_run_id = :run_id
 WHERE id IN (:selected_ids);
COMMIT;                            -- 立即释放行锁；这批行 lease 给本 run_id
```

dry-run：只跑上面的 `SELECT`（不带 `FOR UPDATE`、不带 `UPDATE` 认领），报告 selected，不改 DB。
现有部分索引 `idx_user_voices_temp_expires_pending`（`WHERE is_temporary=TRUE AND expired_at IS NULL`）覆盖主筛选；其余为 filter。

**状态机（per voice row）**：

| 字段组合 | 含义 |
|---|---|
| `expired_at IS NULL` + `cleanup_attempts=0` + `temporary_expires_at < now` + `claim_until` 空/过期 | 待认领（pending） |
| `expired_at IS NULL` + `cleanup_claim_until > now` + `cleanup_run_id` set | 已认领、处理中（in-flight，他 runner 跳过）|
| `expired_at IS NULL` + `0 < cleanup_attempts < MAX` + `cleanup_retry_after` set | 清理失败、待重试（backoff 中）|
| `expired_at IS NULL` + `cleanup_attempts >= MAX` | give-up（停止自动，留 manual）|
| `expired_at IS NOT NULL` | 已清理（worker 已删 + 软删完成）—— 终态 |

---

## §4 清理流程

### 4.1 核心函数 `cleanup_expired_temporary_voices`（claim-lease 两阶段）

```python
def cleanup_expired_temporary_voices(db_factory, *, dry_run: bool, limit: int,
                                     worker_delete, now=None,
                                     include_give_up=False) -> CleanupReport:
    run_id = uuid4().hex
    # dry-run：只 SELECT 报告，绝不认领 / 不调 worker / 不改 DB
    if dry_run:
        rows = select_eligible(db, limit=limit, now=now, include_give_up=include_give_up)
        return CleanupReport(dry_run=True, selected=[r.voice_id for r in rows])

    # === phase 1：认领（短事务，FOR UPDATE SKIP LOCKED；决策 7）===
    async with db_factory() as db:
        claimed = await claim_batch(db, run_id=run_id, limit=limit, now=now,
                                    lease=LEASE, include_give_up=include_give_up)
        # claim_batch 内：SELECT … FOR UPDATE SKIP LOCKED → UPDATE claim_until/run_id → COMMIT
    if not claimed:
        return CleanupReport(dry_run=False, deleted=0)

    # === phase 2：处理（事务外，逐行；绝不跨外部调用持锁）===
    deleted = failed = gave_up = 0
    for c in claimed:
        try:
            worker_delete(c.voice_id, user_id=c.user_id,
                          job_id=c.source_job_id or "cleanup",
                          reason="temporary_voice_ttl_cleanup")        # 付费/外部
        except WorkerError as exc:
            if _is_recognized_already_gone(exc):   # 决策 4：仅稳定 voice_not_found code
                pass                               # 归一成功（前置映射落地后才会命中）
            else:
                async with db_factory() as db:     # 失败：释放 lease + backoff（不写 expired_at）
                    await release_with_backoff(db, c.id, run_id=run_id,
                                               error=getattr(exc, "code", "") or type(exc).__name__)
                # release_with_backoff：attempts+1；attempts>=MAX → audit give_up（不再 retry_after）
                #                        否则 retry_after=now+backoff(attempts)；都清 claim_until/run_id
                failed += 1   # （give-up 计入 gave_up，实现里分流）
                continue
        # 成功 / 已识别 already-gone → 软删（决策 1 顺序：先 worker 后 DB；run_id 守卫）
        async with db_factory() as db:
            ok = await complete_soft_delete(db, c.id, run_id=run_id, now=now)
            # UPDATE … SET expired_at=now, claim_until=NULL
            #  WHERE id=:id AND cleanup_run_id=:run_id AND expired_at IS NULL
            #  → 0 行表示 lease 已被重认领，本次不 clobber（no double-complete）
        if ok:
            emit_cleanup_audit(decision="cleaned", voice_id=c.voice_id, ...)
            deleted += 1
    return CleanupReport(dry_run=False, deleted=deleted, failed=failed, gave_up=gave_up)
```

**关键**：
- `worker_delete` **注入式**（DI，参照 PR2-E）——真实现包 `MainlandWorkerClient.delete_voice`，测试注入 mock。核心不直接 import worker。
- phase 1 认领事务 **不跨** worker 调用；phase 2 每行用独立短事务更新（成功/失败），`cleanup_run_id` 守卫防 lease 重认领后 clobber。
- `_is_recognized_already_gone` 默认恒 False（无稳定 code，决策 4）；前置错误码映射落地后才识别 `voice_not_found`。

### 4.2 sweeper（`gateway/express_voice_cleanup_sweeper.py`，新）

参照 PR2 `express_reservation_sweeper`：

- gateway lifespan `create_task` + `app.state.express_voice_cleanup_sweeper_task` + 启动 try/except fail-safe + shutdown cancel
- 周期保守（建议 1h；env `AVT_EXPRESS_VOICE_CLEANUP_INTERVAL_S` 默认 3600）+ batch limit（默认 50）
- **起步可 dry-run**：env `AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN`（默认 `true`，观察期先只 log "would delete N"，确认后翻 `false` 实删）
- 单次异常只 log、loop 续命（fail-safe）
- 装配真实 `worker_delete`（`build_client_from_env` → `MainlandWorkerClient.delete_voice`）；**worker 不可用 → 在 claim 之前整轮 skip（log），不认领、不崩**（§2.7 fail-fast-before-claim）

### 4.3 admin 手动清理（G4）= CLI / manual script

- 形态：**CLI / manual script**（如 `python -m scripts.cleanup_temp_voices`），复用 G2 核心 + claim-lease（与自动 sweeper 并发安全）。**不**引入走 `/api/internal` 的后台按钮（§2.6）。
- flags：`--dry-run`（默认建议先跑）/ `--limit N` / `--include-give-up` / `--reset-attempts`。
- 用途：灰度观察（dry-run 预览选中集）/ give-up 行人工兜底（`--include-give-up [--reset-attempts]`）/ 紧急回收。
- 若未来确需 UI：`/api/admin/...` + admin-auth + CSRF（绝不 internal-key + 浏览器）——属后续阶段，4.3b 不做。

---

## §5 付费 API 安全（§6 DoD 汇总）

| 约束 | 落点 |
|---|---|
| dry-run | §2.3 + §4.1 `dry_run` 参数 + sweeper 起步默认 dry-run |
| batch limit | §3 `LIMIT :batch_limit` + sweeper 默认 50/轮 |
| audit | §6（每次成功/失败/give-up/dry-run 落 audit） |
| retry / backoff | §2.2 `cleanup_retry_after` + `cleanup_attempts` + MAX give-up |
| fail-safe | §4.2 sweeper try/except 不崩；worker 不可用本轮 skip |
| 失败不硬删 | §2.2 失败**不**写 `expired_at`，保留可重试 |
| 只删自建资源 | NG2/NG3 只删 `is_temporary=TRUE AND provider=cosyvoice_voice_clone`（系统自建临时 clone） |

---

## §6 audit

`<runtime_logs>/express_voice_cleanup.jsonl`（或复用现有 runtime audit 通道；实施时定）：

```json
{
  "kind": "express_temp_voice_cleanup",
  "ts": "...",
  "decision": "cleaned" | "cleanup_failed" | "cleanup_give_up" | "dry_run",
  "voice_id": "cosyvoice-v3.5-flash-...",
  "user_id": "...",
  "worker_request_id": "...",            // delete_voice 返回
  "cleanup_attempts": 1,
  "error": null | "<error code>",
  "temporary_expires_at": "...",
}
```

dry-run 也落 audit（`decision="dry_run"` + selected voice_ids），便于灰度观察。

---

## §7 测试守卫（mock worker，0 真实调用）

**新文件**：`tests/test_phase43b_voice_cleanup_sweeper.py`（真 in-memory aiosqlite + mock worker_delete）

- `test_selects_only_expired_temp_cosyvoice` — 选行只含 `is_temporary=TRUE + cosyvoice + requires_worker + temporary_expires_at<now + expired_at IS NULL`；不选永久 / 非 cosyvoice / 未到期 / 已 expired
- `test_delete_worker_then_mark_expired_order` — 成功路径：先调 worker_delete、后写 expired_at（mock 记录调用序）
- `test_worker_fail_does_not_set_expired_at` — worker_delete 抛 → expired_at 仍 NULL + cleanup_attempts+1 + retry_after set（**不硬删**，Codex 重点）
- `test_give_up_after_max_attempts` — attempts≥MAX → 不再 select + audit cleanup_give_up
- `test_retry_after_backoff_excludes_row` — retry_after > now 的行本轮不选
- `test_dry_run_no_worker_call_no_db_change` — dry_run=True → worker_delete **0 次调用** + expired_at 不变 + 不写 claim_until + 报告 selected
- `test_unknown_delete_error_retries_not_success` — **决策 4（P1-2）**：worker_delete 抛 `delete_voice_failed`（未知 code）→ **失败重试**（attempts+1 / retry_after set），**绝不**软删（expired_at 仍 NULL）
- `test_recognized_already_gone_marks_expired` — 仅当 `_is_recognized_already_gone` 命中稳定 `voice_not_found` code → 软删成功（默认无映射时该用例 mock 注入识别函数验证分支；前置映射落地前 skip/xfail）
- **claim-lease（决策 7）**：
  - `test_claim_writes_lease_and_run_id` — 认领后 `cleanup_claim_until`≈now+LEASE + `cleanup_run_id` set
  - `test_claimed_row_excluded_from_select` — `cleanup_claim_until > now` 的行本轮不被再选
  - `test_expired_lease_reclaimable` — `cleanup_claim_until < now` → 可被重新认领
  - `test_complete_guarded_by_run_id` — 用过期 run_id 调 complete → 0 行更新（不 clobber 重认领行）
- `test_lease_constant_exceeds_delete_window` — 守卫：`CLEANUP_CLAIM_LEASE_SECONDS` ≥ delete 最坏重试窗口安全下界（防 LEASE 调太短 → 双删）
- `test_worker_unavailable_skips_before_claim` — `build_client_from_env()` 返 None → 整轮 0 认领（claim 前 fail-fast，不留 lease 占用）
- `test_sweeper_loop_fail_safe` — 单次异常不崩 loop（参照 PR2 sweeper 测试）
- `test_sweeper_does_not_touch_non_temporary` — is_temporary=FALSE 永不被删（NG2）
- **`test_cleanup_module_isolation`** — AST：4.3b sweeper 与 reservation sweeper 不互相 import；4.3b 核心函数 worker 走注入（不在纯 DB 测试里真 import worker client）
- `test_lifespan_wires_voice_cleanup_sweeper` — 静态确认 lifespan create_task + app.state + try/except + shutdown cancel（不真起无限循环）
- `test_no_real_dashscope_call` — 守卫：测试全程 worker_delete 是 mock（断言注入路径，0 真实 HTTP）

**PG 并发 integration（决策 7，sqlite 测不了 FOR UPDATE SKIP LOCKED；参照 PR2 §10.7 + `tests/test_phase43a_pr2_reservation_pg_atomic.py` 的 DSN 安全 guard）**：

**新文件** `tests/test_phase43b_voice_cleanup_pg_concurrent.py`（本地无 PG skip；CI `backend-pg-integration` job 真跑）：

- `test_concurrent_claim_no_double_select` — 2 个并发 `claim_batch`（同一批到期音色）→ 断言**无**同一 voice 被两边同时认领（`FOR UPDATE SKIP LOCKED` 生效），两边认领集合不相交
- `test_concurrent_claim_total_covers_all` — 两并发认领的并集 = 全部 eligible（无遗漏、无重复）

---

## §8 与 reservation TTL sweeper 的区分（再强调）

见 §2.5 表。一句话：**reservation sweeper 翻状态（免费），voice cleanup sweeper 删音色（付费）**。两个独立模块、独立 lifespan task、独立 admin setting、独立 audit。混用会出 4.3a PR2-D 守卫挡过的"sweeper 调付费 API"风险。

---

## §9 边界与约束（Codex 4.3b 红线）

| 项 | 约束 |
|---|---|
| alembic / migration 033 | ❌ 不 apply（随分支落，部署阶段单独授权才跑） |
| 部署 | ❌ 4.3b merge 后不部署 |
| Express 主开关 | ❌ 不翻（4.3b 与灰度解耦） |
| 真实 worker 调用 | ❌ 测试 0 真调；sweeper 起步建议 dry-run |
| 删非临时 / 非 cosyvoice 音色 | ❌ 永不（NG2/NG3） |
| 失败硬删 DB 行 | ❌ 永不（决策 2，保留可重试） |

---

## §10 风险矩阵

| # | 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|---|
| R1 | 先软删后删 worker → DashScope 孤儿永久扫不到 | M | High | §2.1 强制顺序：先 worker 后 DB |
| R2 | 永久失败行每周期狂调 worker（费用/限流）| M | M | §2.2 backoff + give-up |
| R3 | sweeper 误删用户永久音色 | L | High | NG2 + `test_sweeper_does_not_touch_non_temporary` |
| R4 | 与 reservation sweeper 混淆 → sweeper 调付费 API 漂移 | L | High | §2.5 独立模块 + AST 隔离守卫 |
| R5 | 首次灰度一次性删大量音色 | M | M | §2.3 dry-run 起步 + batch limit |
| R6 | 旧 `mark_voice_expired` 路径绕过 worker 删 → 孤儿 | M | M | §2.1 收敛单一入口 + 文档/守卫锁 |
| R7 | 误把 `delete_voice_failed` 当 already-gone → 软删但 DashScope voice 还在（反向孤儿）| M | High | §2.4（P1-2）：默认全部失败重试；already-gone 仅认稳定 `voice_not_found` code，且前置错误码映射落地 + 测试后才启用 |
| R8 | auto sweeper + manual / 多 gateway 并发选中同一行 → 重复付费 `delete_voice` | M | High | §2.7（P1-1）claim-lease：`FOR UPDATE SKIP LOCKED` 认领 + lease 过滤 + run_id 完成守卫 + PG 并发测试 |

---

## §11 实施分阶段

| 阶段 | 内容 | 估时 |
|---|---|---|
| **4.3b-A** | migration 033（**5 列**：attempts/retry_after/last_error + claim_until/run_id）+ ORM + schema 守卫 | 3-4h |
| **4.3b-B** | `cleanup_expired_temporary_voices` 核心（claim-lease 两阶段 + 两步删序 + 失败 backoff + give-up + dry-run）+ sqlite 单测（mock worker，含 run_id 守卫 / claim 排除 / lease 过期） | 8-11h |
| **4.3b-B-pg** | PG 并发 integration 测试（`FOR UPDATE SKIP LOCKED` 无双认领）+ CI 复用 `backend-pg-integration` job | 3-4h |
| **4.3b-C** | sweeper 模块 + lifespan wiring + fail-safe 守卫 + dry-run 起步开关 | 3-4h |
| **4.3b-D** | CLI/manual cleanup script（`--dry-run` / `--include-give-up` / `--reset-attempts`） | 3-5h |
| **4.3b-E** | 全套测试 + 与 reservation sweeper 隔离守卫 + 开 PR + Codex review | 4-6h |

**可选前置（仅当要 already-gone 优化，决策 4）**：
| **4.3b-A0** | worker/provider 错误码映射：`real_cosyvoice.delete_voice` 识别 SDK not-found → 稳定 `code="voice_not_found"`；client 透传；独立单测 | 3-5h |

> A0 **可选**——不做则 already-gone voice 重试到 give-up 转 manual（保守安全，无双删）。做则需先于 B 的 already-gone 分支落地 + 测试。

**总估时**：~24-34 工时（含 PG 测试；A0 可选另计）

**依赖顺序**：A →（A0 可选）→ B → B-pg →（C 并行 D）→ E

---

## §12 实施前自检

- [ ] 用户/Codex 已审 §1 范围（4.3b = 清理 sweeper，不部署不灰度）
- [ ] 已审 §2 **七**决策（两步顺序 / 失败不硬删 / dry-run / **已不存在仅认稳定 code** / 与 reservation sweeper 区分 / admin=CLI / **claim-lease 并发**）
- [ ] 已审 §3 schema（migration 033 **五列**：含 claim_until/run_id，复用 expired_at 软删 + 正确索引名 `idx_user_voices_temp_expires_pending`）
- [ ] 已审 §4 清理流程（claim-lease 两阶段：认领短事务 FOR UPDATE SKIP LOCKED → 事务外删 worker → run_id 守卫完成；先 worker 后 DB；注入式 worker_delete）
- [ ] 已审 §5/§9 付费 API 安全 + 红线
- [ ] 已审 §7 测试（mock worker，0 真调；含 PG 并发 SKIP LOCKED 测试）
- [ ] **v0.2 闭合（Codex 一轮）**：P1-1 claim-lease 并发（§2.7/§3/§4/§7 PG 测试）；P1-2 already-gone 不凭假设（§2.4 默认全部重试 + 错误码映射前置）；P2-1 admin=CLI 非 internal-key + give-up retry 开关（§2.6/§4.3）；P2-2 索引名修正
- [ ] @codex review 此 spec v0.2
- [ ] Codex 反馈纳入后再开 4.3b 实施
