# 迁移债务修复实施方案（Critical 批次）

> **Status:** completed  
> **Last updated:** 2026-04-17  
> **Implemented-by:** T1-T8 commits `8e8b896` (T3) / `122b48d` (T6) / `60c8a44` (T4) / `a1484dc` (T1) / `f7a3053` (T2) / `bcb5e12` (T5) / `72afcef` (T8) / `d3039b0` (T7)，Codex P1/P3 fixes `f1b1be0` / `8fefee7`，已全量部署 US  
> **前置：** 2026-04-17 三审报告（模块耦合 / 错误处理 / 状态管理 / 安全 / 迁移残留）  
> **执行说明：** 本方案按步骤执行，每个 Task 独立提交。

---

## 1. 背景

项目从单机桌面迁移到多用户 Web SaaS，本次审核发现一批迁移期遗留的并发与安全问题。本方案聚焦 **Critical 档**：会造成**真金白银超扣、跨用户越权、凭据弱回退**的问题。成本优化类（S2 Pass1 fallback、TTS 自动切 provider）已确认推迟到后续批次——详见 [memory/feedback_paid_api_constraint_scope.md](../../memory/feedback_paid_api_constraint_scope.md)。

**本次修复范围（8 个 Task，按依赖序）：**

| Task | 主题 | 触及子系统 |
|------|------|----------|
| T1 | Shadow credits 并发锁（reserve + capture + release） | `gateway/credits_service.py` |
| T2 | `continue_job()` 防并发重入 | `gateway/job_intercept.py`（首选）|
| T3 | 删 `avt:avt` 硬编码 DB fallback | `gateway/config.py` |
| T4 | `/api/internal/voice-catalog` 强 localhost 访问 | `gateway/voice_catalog_api.py` + `gateway/main.py` |
| T5 | Job API 生产环境不泄漏异常详情 | `src/services/jobs/api.py` |
| T6 | 生产环境强制 `auth_required=True` 校验 | `gateway/main.py` |
| T7 | TTS fallback 可观测性（日志+metadata） | `src/services/tts/tts_generator.py` |
| T8 | Security hardening（密码 12 位 + cookie strict） | `gateway/auth.py` |

**非目标（本批次不做）：**
- 不动 S2 Pass 1 LLM fallback 链（`transcript_reviewer.py:1095-1111`）——属于成本优化
- 不动 TTS 失败段重试循环（`tts_generator.py:324-333`）——属于成本优化
- 不改 TTS provider 自动切换行为本身（只加观测），用户同意本批次仅加可观测性
- 不拆 `control_panel.py` / 删 `main.py` 桌面子命令——后续批次
- 不迁移 `voice_registry.json` 到 user-scoped / DB——后续批次（范围大，需独立方案）

---

## 2. 现状真实代码基线

| 组件 | 文件 | 行号 | 现状 |
|------|------|------|------|
| `shadow_reserve` | `gateway/credits_service.py` | 231-298 | 无锁，读 bucket → 修改 `bucket.reserved` → `db.add(entry)` |
| `shadow_capture` | 同上 | 301-462 | 同上，`SELECT bucket WHERE id=...` 后直接改，无锁 |
| `shadow_release` | 同上 | 465+ | 同样模式 |
| `shadow_rollback` | 同上 | 530-559 | **新发现**：`select(CreditsBucket).where(id=..., user_id=...)` + `bucket.remaining=0; bucket.reserved=0` 无锁 |
| `get_user_buckets` | 同上 | 134-149 | 普通 `SELECT`，无 FOR UPDATE |
| Caddyfile 反代 | `Caddyfile` | 44-52 | `@api_routes` 把 `/api/*` 全量反代到 Gateway，**内部端点被公网可达**（Gateway 收到的 `client.host` 是 Caddy 的 127.0.0.1，localhost 检查无效）|
| `get_db` | `gateway/database.py` | 19-21 | `async with session as s: yield s` —— **不显式 commit，默认回滚**。handler 必须显式 `await db.commit()` 才能让写落盘 |
| DB engine 初始化 | `gateway/database.py` | 9 | `create_async_engine(settings.database_url, ...)` —— import 时立即执行，若 url 空会崩 |
| `continue_job` Job API | `src/services/jobs/service.py` | 155-173 | 无锁；注释声明"Concurrency control is enforced at gateway layer" |
| Gateway continue 路径 | `gateway/job_intercept.py` | 555-570 | `intercept_job_subpath` 只做 ownership 检查 + proxy，无并发控制 |
| DB URL fallback | `gateway/config.py` | 65-66 | `elif not _raw.database_url: _raw.database_url = "postgresql+asyncpg://avt:avt@localhost:5432/aivideotrans"` |
| internal voice-catalog | `gateway/voice_catalog_api.py` | 86, 101-198 | `internal_router = APIRouter(prefix="/api/internal", ...)`，`/voice-catalog` 无任何访问控制 |
| `_verify_job_ownership` | `gateway/job_intercept.py` | 609-625 | `if not settings.auth_required or user is None: return`——auth_required=False 时**完全跳过** |
| Job API 异常响应 | `src/services/jobs/api.py` | 273-274 | `except Exception as exc: self._write_json(500, {"error": str(exc)})` |
| 邮箱密码最短 | `gateway/auth.py` | 159-160 | `if len(body.password) < 6:` |
| Cookie samesite | `gateway/auth.py` | 85+（`set_cookie`） | `samesite="lax"` |
| TTS fallback 路径 | `src/services/tts/tts_generator.py` | 800-814 | `fallback = get_fallback_provider(provider, ...)`，切 provider 只 `print`，不入 segment metadata |

---

## 3. ❓ 执行前必须确认的决策点

### ❓决策 1 — Shadow credits 并发锁策略（T1）

| 选项 | 优点 | 缺点 |
|------|------|------|
| **A. `SELECT ... FOR UPDATE` 悲观锁** | 改动最小，无 schema 变更；语义清晰 | 长事务有死锁风险（我们事务短，风险低） |
| B. 加 `CreditsBucket.version_id` 乐观锁 | 无锁，高并发吞吐好 | 需 migration 015；失败重试循环逻辑复杂 |
| C. Redis 分布式锁 | 跨进程 | 项目目前无 Redis 依赖，引入成本大 |

**默认推荐：A**。项目是单 Gateway 实例部署，同一 user 的并发 job 创建是低概率场景，悲观锁够用。

**✅ 你的选择：_______**

---

### ❓决策 2 — `continue_job()` 锁策略（T2）

| 选项 | 优点 | 缺点 |
|------|------|------|
| **A. Gateway 层加 `SELECT Job FOR UPDATE` + 状态校验（waiting_for_review）** | 符合现有注释"concurrency enforced at gateway layer"；一处修即全覆盖 | 需要 Gateway 层可访问 Job 状态（DB 有 mirror 记录，可行） |
| B. Job API 层加 `threading.Lock` 按 job_id 分片 | 更靠近真实 subprocess spawn 点 | 无法跨进程；未来扩展为多 Job API 实例会失效 |
| C. 仅靠 Job API 的 `JOB_STATUS_WAITING_FOR_REVIEW` 检查（line 157-158）做幂等 | 零改动 | 无锁，两请求都能过检查后 spawn |

**默认推荐：A**。与现有架构一致。

**✅ 你的选择：_______**

---

### ❓决策 3 — `avt:avt` 硬编码 fallback 处理（T3）

| 选项 | 语义 |
|------|------|
| **A. 启动时 raise RuntimeError（`AVT_PG_PASSWORD` 与 `AVT_DATABASE_URL` 都未设置时）** | fail-closed |
| B. warning log + 继续使用 fallback | fail-soft |

**默认推荐：A**。数据库凭据必须明示配置，避免生产误用 `avt:avt` 这种开发凭据。

**✅ 你的选择：_______**

---

### ❓决策 4 — `/api/internal/voice-catalog` 访问控制（T4）

| 选项 | 防御深度 |
|------|---------|
| A. 强制 `request.client.host == "127.0.0.1"` | 单层 |
| B. 加 `X-Internal-Token` header（`AVT_INTERNAL_API_KEY`，与 Job API 已有 key 一致） | 单层 |
| **C. A+B 双层** | 双层防御，互补 |

**默认推荐：C**。两层都改动极小，且 `AVT_INTERNAL_API_KEY` 已在 `jobs/api.py:444` 存在，可复用。

**✅ 你的选择：_______**

---

### ❓决策 5 — Job API 错误响应过滤（T5）

| 选项 | 用户体验 |
|------|---------|
| **A. 生产环境统一返回 `{"error": "internal_error"}`，详细写日志** | 简单，安全 |
| B. 加 `AVT_ERROR_DETAIL=false` 开关，生产默认关 | 更灵活 |

**默认推荐：A**。已有的 `JobNotFoundError` / `JobConflictError` / `ValueError` 分支已经返回特定错误消息，**只有最后一档 `except Exception` 需要改**（line 273-274）。

**✅ 你的选择：_______**

---

### ❓决策 6 — 生产强制 `auth_required=True`（T6）

| 选项 | 风险 |
|------|------|
| **A. Gateway 启动时校验：若 `AVT_ENV=production` 且 `AVT_AUTH_REQUIRED=false` → raise** | 禁止运维误配 |
| B. 仅加日志警告 | 低阻 |

**默认推荐：A**。若 `_verify_job_ownership` 在 auth_required=False 时完全跳过（已知问题 #10），生产必须堵。

**✅ 你的选择：_______**

---

### ❓决策 7 — TTS fallback 可观测性位置（T7）

| 选项 | 用户可见性 |
|------|----------|
| A. 仅日志 | 内部排查用 |
| B. 写入 segment metadata（`fallback_used_provider` 字段） | 产物 manifest 可查 |
| **C. A+B** | 日志 + 产物都能追溯 |

**默认推荐：C**。用户下载 artifacts 时能看到哪些段落用了 fallback，便于客诉排查。

**✅ 你的选择：_______**

---

### ❓决策 8 — Security hardening（T8）

| 项 | 改动 |
|----|------|
| 密码最短 | 6 → 12 |
| Cookie samesite | lax → strict |

**确认采纳？** 改动极小（各 1 行），邮箱注册生产已默认关闭（`email_registration_enabled=False`），影响范围小。

**✅ 你的选择（是/否）：_______**

---

## 4. 文件触及总览

| 文件 | Task | 改动类型 |
|------|------|---------|
| `gateway/credits_service.py` | T1 | `get_user_buckets` 加 `for_update` 参数；`shadow_reserve`/`capture`/`release`/`rollback` 全加锁 |
| `gateway/job_intercept.py` | T2 | `intercept_job_subpath` 对 continue 子路径调用 `_reserve_continue_transition`（锁 + 过渡态 + commit） |
| `gateway/config.py` | T3, T4, T6 | 加 `internal_api_key` 和 `env` 字段；`resolve_database_url` 纯函数（不自调） |
| `gateway/database.py` | T3 | engine + session_maker 改懒初始化，加 `init_db()` |
| `gateway/main.py` | T3, T4, T6 | 统一 startup block：`validate_production_safety` + `validate_internal_api_key` + `init_db` |
| `gateway/voice_catalog_api.py` | T4 | `internal_voice_catalog` 加 `Depends(_require_internal_access)`（无条件 token + loopback 校验）|
| `gateway/auth.py` | T8 | `len < 6` → `len < 12`；`samesite="lax"` → `"strict"` |
| `Caddyfile` | T4 | `@internal_block` 在 `@api_routes` 之前拦截 `/api/internal/*` 返 404 |
| `docker-compose.yml` | T3, T4 | 确保 `AVT_PG_PASSWORD` + `AVT_INTERNAL_API_KEY` 在 gateway + app 两个 service 都有 |
| `.env.example` | T3, T4 | 加 `AVT_INTERNAL_API_KEY=` 占位 + 生成命令注释 |
| `src/services/jobs/api.py` | T5 | handler class 加 `_send_sanitized_error` helper；所有 `except Exception` 兜底改调 helper |
| `src/services/tts/tts_generator.py` | T7 | `TTSResult` 加 `fallback_used_provider` 字段；fallback 触发处 `result.fallback_used_provider = fallback` + 结构化日志 |
| `src/pipeline/process.py` | T7 | segment 字段拷贝 + manifest 构造（line ~3920）加 `fallback_used_provider` |
| DubbingSegment 定义文件 | T7 | 加 `fallback_used_provider: str \| None` 字段（T7.2 grep 定位）|
| **测试文件（新建/修改）** | 全部 | 见各 Task 的 Test 部分 |

---

## 5. 任务分解

### Task 1: Shadow credits 并发锁（`SELECT FOR UPDATE`）

**目标：** 所有写 `CreditsBucket.reserved` / `CreditsBucket.remaining` 的路径（`shadow_reserve` / `shadow_capture` / `shadow_release` / `shadow_rollback`）在读 bucket 时加行锁。

**完整 writer 盘点（已 grep 验证）：**

| 函数 | 行号 | 写入字段 |
|------|------|---------|
| `shadow_reserve` | 263 | `bucket.reserved +=` |
| `shadow_capture` | 373, 375 | `bucket.reserved -=`, `bucket.remaining -=` |
| `shadow_capture`（overflow path）| 413, 414 | 同上 |
| `shadow_capture`（additional debit）| 440 | `bucket.remaining -=` |
| `shadow_release` | ~496 | `bucket.reserved -=` |
| `shadow_rollback` | 546, 547 | `bucket.remaining = 0`, `bucket.reserved = 0` |

所有 bucket SELECT 必须加 `.with_for_update()`，对应 4 个函数。

**Files:**
- Modify: `gateway/credits_service.py:134-149`（`get_user_buckets` 加 `for_update` 参数）
- Modify: `gateway/credits_service.py:248-264`（`shadow_reserve` 调用 locked 版本）
- Modify: `gateway/credits_service.py:361-362, 405-406, 431-432`（`shadow_capture` SELECT bucket 加 `.with_for_update()`）
- Modify: `gateway/credits_service.py:480+`（`shadow_release` 同理）
- Modify: `gateway/credits_service.py:534-539`（`shadow_rollback` SELECT 加 `.with_for_update()`）
- Test: `tests/test_credits_service.py`（新增 `test_concurrent_reserve_no_double_claim` + `test_rollback_locks_bucket`）

---

- [ ] **T1.1: 改 `get_user_buckets` 签名加 `for_update` 参数**

修改 `gateway/credits_service.py:134-149`：

```python
async def get_user_buckets(
    db: AsyncSession,
    user_id,
    *,
    include_expired: bool = False,
    for_update: bool = False,   # NEW
) -> list[CreditsBucket]:
    """Return all active buckets for a user, ordered by created_at.

    for_update=True: issue SELECT ... FOR UPDATE to acquire row locks on buckets.
    Caller must be inside a transaction (which is always true for handler DB sessions).
    """
    now = datetime.now(timezone.utc)
    stmt = select(CreditsBucket).where(
        CreditsBucket.user_id == user_id,
    ).order_by(CreditsBucket.created_at)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    ...
```

- [ ] **T1.2: 改 `shadow_reserve` 用 `for_update=True`**

`gateway/credits_service.py:249`：

```python
buckets = await get_user_buckets(db, user_id, for_update=True)
```

- [ ] **T1.3: 改 `shadow_capture` SELECT bucket 时加锁**

`gateway/credits_service.py:361-362`：

```python
bucket_result = await db.execute(
    select(CreditsBucket)
    .where(CreditsBucket.id == re.bucket_id)
    .with_for_update()   # NEW
)
```

同样改 line 405-406 的 `select(CreditsBucket)`，以及 line 431 的 `get_user_buckets(db, user_id)` 改 `for_update=True`。

- [ ] **T1.4: 改 `shadow_release`**

找到 `shadow_release` 中所有 `select(CreditsBucket)` 和 `get_user_buckets` 调用，加锁。

- [ ] **T1.4b: 改 `shadow_rollback`**

`gateway/credits_service.py:534-539`：

```python
result = await db.execute(
    select(CreditsBucket)
    .where(
        CreditsBucket.id == bucket_id,
        CreditsBucket.user_id == user_id,
    )
    .with_for_update()   # NEW
)
```

**执行时完整盘点验证：** 改完跑一次 `grep -n "\.remaining\s*[=+-]\|\.reserved\s*[=+-]" gateway/credits_service.py`，确认每个 writer 上方的最近 SELECT 都带 `with_for_update()`。若未来有新 writer 加入，此检查能防漂移。

- [ ] **T1.5: 写并发测试**

新建测试：`tests/test_credits_service.py::test_concurrent_reserve_no_double_claim`

```python
@pytest.mark.asyncio
async def test_concurrent_reserve_no_double_claim(db_session_factory):
    """Two concurrent reserve() calls for same user must NOT both succeed
    when combined claim exceeds available credits."""
    # Setup: user with 100 credits in single bucket
    # Launch 2 concurrent shadow_reserve(estimated_credits=60) tasks
    # Assert: sum of reserved credits across both <= 100
    # Assert: no bucket has reserved > remaining
```

测试使用两个独立 `AsyncSession`（两个事务）模拟并发。PostgreSQL 下 FOR UPDATE 会阻塞第二个事务直到第一个 commit。

- [ ] **T1.6: 跑测试**

```bash
python -m pytest tests/test_credits_service.py -v
```

期望：新测试 PASS，所有既有测试 PASS。

- [ ] **T1.7: 提交**

```bash
git add gateway/credits_service.py tests/test_credits_service.py
git commit -m "fix(credits): add SELECT FOR UPDATE to prevent concurrent double-claim"
```

---

### Task 2: `continue_job()` Gateway 层防重入（锁 + 过渡态 + 显式 commit）

**目标：** `/job-api/jobs/{id}/continue` 在 Gateway 层彻底堵住并发重入窗口。**两次审核都发现此 Task 的原方案有漏洞**，现修订版需同时解决两个问题。

**两个原漏洞：**

1. **[Codex 发现] 脏读窗口：** Gateway DB 的 `Job.status` 只在 `list_jobs()` 里同步上游状态（[job_intercept.py:215-227](../../gateway/job_intercept.py)）。若只做 `SELECT FOR UPDATE` 读 status 检查，Req1 释放锁后 Req2 读到的依然是旧 `waiting_for_review`——锁只能串行化请求，**不去重入**。
2. **[Claude Code 发现] 不 commit：** `get_db` ([database.py:19-21](../../gateway/database.py)) 没显式 commit，handler 内的写默认回滚。即使我们在同事务内写过渡态，若不 `await db.commit()`，该写会被回滚，Req2 读的还是旧值。

**合并修法：** 同事务 `SELECT FOR UPDATE` → 校验 status → **立即 UPDATE Job.status='running' 过渡态** → **显式 `await db.commit()`** → 再 proxy 到上游。Req2 拿锁时会读到 `running` 状态，直接 409。

**关于"立即写 running 是否正确"：** 调 `/continue` 的语义就是把 job 从 review 推入运行态。即使上游 Job API 还没真正 spawn subprocess，Gateway 层把 status 预先置为 `running` 是**偏乐观但合理的**——这也是 `list_jobs` 后续同步会写的值。上游若失败（极少），后续同步会覆盖为 `failed`；这个偏差窗口可接受，换取并发安全。

**Gateway Job 模型字段确认（已验证）：**
- [gateway/models.py:120-123](../../gateway/models.py) —— `Job.id` 是 UUID PK；`Job.job_id` 是 String(64) unique —— 本 Task 用 `Job.job_id`
- [gateway/models.py:131](../../gateway/models.py) —— `Job.status: Mapped[str]` 存在
- [gateway/job_intercept.py:227 + 267](../../gateway/job_intercept.py) —— `db_job.status = x` + `await db.commit()` 的成熟模式可复用

**Files:**
- Modify: `gateway/job_intercept.py:553-570`（`intercept_job_subpath` 加 continue 分支）
- Modify: `gateway/job_intercept.py`（新增 `_reserve_continue_transition` 函数）
- Test: `tests/test_gateway_continue_concurrency.py`（新建）

---

- [ ] **T2.1: 在 `intercept_job_subpath` 中识别 continue 子路径**

`gateway/job_intercept.py:553-570`：

```python
async def intercept_job_subpath(
    request: Request,
    job_id: str,
    subpath: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    await _verify_job_ownership(job_id, db, user)

    # Concurrency control for state-transition endpoints.
    # Must complete (lock + write + commit) BEFORE proxying, so that a
    # concurrent second request observes the transitional state.
    if subpath == "continue" and request.method == "POST":
        await _reserve_continue_transition(job_id, db)

    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )
```

- [ ] **T2.2: 新增 `_reserve_continue_transition` 函数（锁 + 过渡态 + commit）**

在 `gateway/job_intercept.py` 加：

```python
async def _reserve_continue_transition(job_id: str, db: AsyncSession) -> None:
    """Atomically lock the Job row, verify it's continuable, flip status to 'running',
    and COMMIT the transition so concurrent continue requests cannot double-spawn.

    Order matters:
      1. SELECT ... FOR UPDATE  (serialize concurrent continues at DB layer)
      2. Assert status == 'waiting_for_review' (idempotent check)
      3. UPDATE status = 'running' (pre-mirror, reconciled later by list_jobs sync)
      4. COMMIT (so a concurrent transaction reading with FOR UPDATE sees 'running')
      5. Proxy to upstream (outside this function; lock/txn already released)

    A second concurrent request blocks at step 1 until the first commits. After the
    first commits, the second reads status='running' and raises 409 at step 2.

    Legacy jobs with no DB row: fall through (no lock to take, upstream handles).
    """
    result = await db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        # Legacy job not mirrored in Gateway DB — let upstream Job API handle validation.
        return
    if job.status != "waiting_for_review":
        # Either a concurrent continue already committed (status='running')
        # or the job isn't actually waiting (done / failed / etc.)
        raise HTTPException(
            status_code=409,
            detail=f"Job is not continuable (current status: {job.status})",
        )
    # Pre-mirror: flip to 'running' so the next concurrent reader sees the transition.
    job.status = "running"
    await db.commit()
```

- [ ] **T2.3: 写并发 integration 测试（必须对 PG 真实并发跑）**

新建 `tests/test_gateway_continue_concurrency.py`：

```python
import asyncio
import pytest

@pytest.mark.asyncio
@pytest.mark.postgres  # SQLite FOR UPDATE is no-op, test only meaningful on PG
async def test_concurrent_continue_rejects_second(async_client, seeded_job_waiting_review):
    """Two simultaneous POST /jobs/{id}/continue — only one should succeed, other 409.

    Verifies both the lock AND the commit timing: if the write isn't committed
    before lock release, the second request reads stale status and proceeds.
    """
    job_id = seeded_job_waiting_review.job_id
    r1_task = async_client.post(f"/job-api/jobs/{job_id}/continue")
    r2_task = async_client.post(f"/job-api/jobs/{job_id}/continue")
    r1, r2 = await asyncio.gather(r1_task, r2_task)

    statuses = sorted([r1.status_code, r2.status_code])
    # Exactly one success (202), one rejection (409)
    assert statuses == [202, 409], f"expected [202, 409], got {statuses}"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_continue_leaves_status_running(async_client, seeded_job_waiting_review, db_session):
    """After successful continue, Gateway DB status must be 'running' (pre-mirrored)."""
    job_id = seeded_job_waiting_review.job_id
    r = await async_client.post(f"/job-api/jobs/{job_id}/continue")
    assert r.status_code == 202
    # Read fresh from DB to verify commit happened
    from gateway.models import Job
    from sqlalchemy import select
    result = await db_session.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one()
    assert job.status == "running"
```

- [ ] **T2.4: 跑测试**

```bash
python -m pytest tests/test_gateway_continue_concurrency.py -v
```

期望：PG 环境下两个测试都 PASS。SQLite 环境下跳过（`@pytest.mark.postgres` filter）。

- [ ] **T2.5: 提交**

```bash
git add gateway/job_intercept.py tests/test_gateway_continue_concurrency.py
git commit -m "fix(gateway): atomic lock+transition+commit on continue to block double-spawn"
```

---

### Task 3: 删 `avt:avt` 硬编码 DB fallback（lazy validation）

**目标：** `AVT_PG_PASSWORD` 和 `AVT_DATABASE_URL` 都未设置时，启动 raise；**import 时不 raise**（保持 pytest 在干净环境可收集测试）。

**设计约束（Codex 指出）：** 原方案把 `_resolve_database_url(_raw)` 放模块 scope 等于没改——import `gateway.config` 就 raise，`pytest` 进不去。必须改为 **lazy pattern**：纯函数不自调，engine 延迟初始化。

**连带处理（Claude Code 指出的 database.py 问题）：** [database.py:9](../../gateway/database.py) 在 import 时就 `create_async_engine(settings.database_url, ...)`。即使 config 改 lazy，database.py 一 import 还是会拿到空 url 崩。所以 **T3 必须同时改 database.py 做 lazy engine**。

**Files:**
- Modify: `gateway/config.py:58-67`（`_resolve_database_url` 纯函数，不自调）
- Modify: `gateway/database.py:1-21`（engine + session_maker 改懒初始化）
- Modify: `gateway/main.py`（startup hook 调 `init_db()`）
- Test: `tests/test_config.py`（新建）

---

- [ ] **T3.1: 抽出 `_resolve_database_url` 纯函数（不自调）**

`gateway/config.py`（替换原 line 58-67）：

```python
def resolve_database_url(raw: GatewaySettings) -> str:
    """Resolve final database URL or raise if no credentials provided.

    Pure function — does NOT mutate raw or trigger at import time.
    Caller must invoke this explicitly (typically at app startup).

    Precedence: explicit raw.database_url → pg_password → refuse fallback.
    """
    if raw.database_url:
        return raw.database_url
    if raw.pg_password:
        encoded = quote_plus(raw.pg_password)
        return f"postgresql+asyncpg://avt:{encoded}@127.0.0.1:5432/aivideotrans"
    raise RuntimeError(
        "Gateway startup refused: neither AVT_PG_PASSWORD nor AVT_DATABASE_URL is set. "
        "Refusing to fall back to default 'avt:avt' credentials. "
        "Set AVT_PG_PASSWORD (preferred) or AVT_DATABASE_URL explicitly."
    )


settings = GatewaySettings()
# NOTE: database_url is NOT populated here. gateway.main.startup() calls
# resolve_database_url(settings) explicitly. Import of this module must
# not raise on missing creds, so tests can import config in a clean env.
```

- [ ] **T3.2: 改 `gateway/database.py` engine 懒初始化**

替换整个文件：

```python
"""Database setup and session management — lazy initialization.

Engine and session_maker are created on first access (or via explicit init_db()
at startup), not at import time. This keeps `import gateway.database` side-
effect-free so tests can collect without valid DB credentials.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import resolve_database_url, settings

_engine = None
_async_session: async_sessionmaker | None = None


def init_db(url: str | None = None) -> None:
    """Explicitly initialize engine + session maker. Called from app startup.

    url: optional override (tests may pass a local TEST_DATABASE_URL).
    If None, calls resolve_database_url(settings) — which raises if creds missing.
    """
    global _engine, _async_session
    resolved = url if url is not None else resolve_database_url(settings)
    _engine = create_async_engine(resolved, echo=False, pool_size=5, max_overflow=10)
    _async_session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def _require_session_maker() -> async_sessionmaker:
    if _async_session is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() at app startup before handling requests."
        )
    return _async_session


async def get_db() -> AsyncSession:
    maker = _require_session_maker()
    async with maker() as session:
        yield session
```

- [ ] **T3.3: 在 `gateway/main.py` 启动时调 `init_db()`**

在 FastAPI app 实例化后、路由挂载前：

```python
from database import init_db
init_db()  # raises RuntimeError if no DB creds configured
```

（此步骤与 T6 的 `validate_production_safety()` 放在同一 startup 区块。）

- [ ] **T3.4: 本地/CI 验证 env 完整**

```bash
grep -n "AVT_PG_PASSWORD\|AVT_DATABASE_URL" docker-compose.yml .env.example 2>/dev/null
```

确认 `docker-compose.yml` 的 `gateway` 和 `app` service 都从 env 读凭据。CI/本地 pytest 如果需要真实 DB，`conftest.py` 可显式 `init_db(url=os.environ["TEST_DATABASE_URL"])`。

- [ ] **T3.5: 单元测试（纯函数直接测）**

新建 `tests/test_config.py`：

```python
import pytest
from gateway.config import GatewaySettings, resolve_database_url

def test_resolve_uses_explicit_url_first():
    s = GatewaySettings(database_url="postgresql+asyncpg://u:p@h/d", pg_password="")
    assert resolve_database_url(s) == "postgresql+asyncpg://u:p@h/d"

def test_resolve_uses_pg_password_when_no_explicit_url():
    s = GatewaySettings(database_url="", pg_password="secret!@#")
    out = resolve_database_url(s)
    assert out.startswith("postgresql+asyncpg://avt:")
    # `quote_plus` encodes "!" → "%21", "@" → "%40" etc. — "secret" literal
    # survives since those chars come after it
    assert "secret" in out

def test_resolve_refuses_fallback():
    s = GatewaySettings(database_url="", pg_password="")
    with pytest.raises(RuntimeError, match="avt:avt"):
        resolve_database_url(s)


def test_config_module_imports_without_creds(monkeypatch):
    """Regression: importing gateway.config in a clean env must NOT raise.

    The whole point of T3's lazy design. If this test fails, someone put
    resolve_database_url call back at module scope.
    """
    monkeypatch.delenv("AVT_PG_PASSWORD", raising=False)
    monkeypatch.delenv("AVT_DATABASE_URL", raising=False)
    # Fresh import should succeed even without creds
    import importlib, gateway.config as cfg
    importlib.reload(cfg)  # reload is safe here — no side effects expected
    assert cfg.settings.database_url == ""  # unset, not populated
```

- [ ] **T3.6: 跑测试**

```bash
python -m pytest tests/test_config.py -v
```

- [ ] **T3.7: 提交**

```bash
git add gateway/config.py gateway/database.py gateway/main.py tests/test_config.py
git commit -m "fix(config): lazy DB init, refuse fallback to hardcoded avt:avt credentials"
```

---

### Task 4: `/api/internal/*` 公网堵死 + token 强制校验

**目标：真正的双层防御** —— Caddy 层对公网返 403；Gateway 层 token 无条件强制（fail-closed）。

**两份审核都发现原方案的严重漏洞，修订要点：**

1. **[Claude Code critical] Caddy 反代绕过 localhost 检查**：Caddyfile 把 `/api/*` 反代到 `127.0.0.1:8880`，Gateway 看到的 `request.client.host` 永远是 127.0.0.1（来自 Caddy 本地反代）。原方案的"localhost 限制"对公网攻击无效。**必须在 Caddy 层堵公网**。
2. **[Claude Code critical] Token 软判断 fail-open**：原写法 `if _INTERNAL_API_KEY: check` 在 env 未设时**直接放行**，方向反了。必须改成 key 空就 raise（启动 fail-closed），或至少请求时 403。
3. **[Codex critical] Header 名错**：现有代码 [voice_catalog_api.py:691](../../gateway/voice_catalog_api.py) + [jobs/api.py:446](../../src/services/jobs/api.py) 全用 `X-Internal-Key`。我原方案写 `X-Internal-Token` 会把所有现有内部调用打成 403。
4. **[Codex high] 模块级缓存 `_INTERNAL_API_KEY = os.environ.get(...)` 让测试 monkeypatch 失效**：key 必须请求时动态读（或从 `settings` 取）。

**Files:**
- Modify: `Caddyfile`（加 `@internal_block` 放在 `@api_routes` 之前）
- Modify: `gateway/config.py`（加 `internal_api_key: str = ""` 字段）
- Modify: `gateway/voice_catalog_api.py:86+`（加 `_require_internal_access` 依赖）
- Modify: `gateway/main.py`（startup 校验 `internal_api_key` 非空）
- Modify: 所有调用方 header 名 `X-Internal-Token` → **不改（从没存在过）**；新增调用方一律用 `X-Internal-Key`
- Modify: `docker-compose.yml` + `.env.example`（确认 `AVT_INTERNAL_API_KEY` 在 `gateway` + `app` 都有）
- Test: `tests/test_internal_voice_catalog_access.py`（新建）
- Test: `tests/test_caddy_internal_block.py`（可选：集成测试，若测试环境没 Caddy 可跳）

---

- [ ] **T4.1: Caddy 层堵死公网（最高优先）**

`Caddyfile` 第 44-52 行之前插入：

```
    # Deny public access to internal-only endpoints (app↔gateway on localhost only).
    # Must come BEFORE @api_routes to short-circuit reverse_proxy.
    @internal_block {
        path /api/internal/*
    }
    handle @internal_block {
        respond "Not Found" 404
    }

    # Other API traffic → Gateway
    @api_routes {
        path /api/*
        ...
    }
```

**设计考量：** 返 404 而非 403，**避免暴露端点存在**（enumeration 防御）。Caddy 命中早于 @api_routes。

- [ ] **T4.2: config 加 `internal_api_key` 字段**

`gateway/config.py` `GatewaySettings` 内：

```python
internal_api_key: str = ""
```

- [ ] **T4.3: 加 `_require_internal_access` 依赖（fail-closed + 动态读 key + 正确 header 名）**

`gateway/voice_catalog_api.py`（`internal_router` 定义附近）：

```python
from fastapi import Depends, HTTPException, Request
from config import settings

async def _require_internal_access(request: Request) -> None:
    """Unconditional internal-endpoint guard.

    Token is read from settings at request time (not module-import time),
    so monkeypatch works in tests. Header name matches existing convention
    (see voice_catalog_api.py:691 and jobs/api.py:446): X-Internal-Key.

    localhost check is a secondary safety net — the primary defense is
    Caddy-level block at Caddyfile @internal_block. In production, Caddy
    ensures public requests never reach here. If Caddy is bypassed (direct
    gateway port access), this IP check still rejects non-loopback clients.
    """
    # Primary: token must be configured AND match. No soft judgment.
    key = settings.internal_api_key
    if not key:
        # Defense in depth: if startup check (T4.5) somehow didn't run, refuse.
        raise HTTPException(status_code=503, detail="Internal endpoint misconfigured")
    provided = request.headers.get("X-Internal-Key", "")
    if provided != key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Key")
    # Secondary: reject non-loopback source (belt-and-suspenders vs. Caddy block).
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Non-loopback client not allowed")
```

- [ ] **T4.4: 挂依赖**

`gateway/voice_catalog_api.py:101-107`：

```python
@internal_router.get("/voice-catalog", dependencies=[Depends(_require_internal_access)])
async def internal_voice_catalog(
    ...
```

- [ ] **T4.5: 启动时强制 `internal_api_key` 非空（fail-closed）**

在 `gateway/main.py` 的 startup 区块（与 T6 的 `validate_production_safety` 放一起）：

```python
def validate_internal_api_key(key: str) -> None:
    """Refuse to start if AVT_INTERNAL_API_KEY is unset.

    Without this key, internal endpoints would fail open (T4's dependency
    returns 503 which is correct but noisy). Force operators to set it.
    """
    if not key or len(key) < 16:
        raise RuntimeError(
            "Gateway startup refused: AVT_INTERNAL_API_KEY must be set "
            "(minimum 16 chars, recommended: 32+ random chars). "
            "Generate: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
        )

validate_internal_api_key(settings.internal_api_key)
```

- [ ] **T4.6: 更新现有内部调用方使用 `X-Internal-Key`（验证现状）**

grep 所有调用 `/api/internal/voice-catalog` 的地方：

```bash
grep -rn "api/internal/voice-catalog\|internal/voice-catalog" src/ gateway/
```

对每处调用确认：① 已传 `X-Internal-Key` header；② key 值从 env 读取（不是 hardcode）。
预期大部分调用点已经有这个 header（因为 [voice_catalog_api.py:687-692](../../gateway/voice_catalog_api.py) 的 `_internal_headers()` helper 已这么做，只是被用在其他内部端点）。若 `internal_voice_catalog` 的调用方还没传，补上。

- [ ] **T4.7: 确认 `AVT_INTERNAL_API_KEY` 在两个容器都可用**

```bash
grep -n "AVT_INTERNAL_API_KEY" docker-compose.yml .env.example 2>/dev/null
```

若 `app` 或 `gateway` service 缺，补：
```yaml
# docker-compose.yml 的 gateway 和 app service 都加
environment:
  - AVT_INTERNAL_API_KEY=${AVT_INTERNAL_API_KEY}
```

`.env.example` 加一行占位（空值 + 注释）：
```
# 32+ char random string. Generate: python -c 'import secrets; print(secrets.token_urlsafe(32))'
AVT_INTERNAL_API_KEY=
```

**产线部署：** 远程 `/opt/aivideotrans/config/.env` 也必须设置该 key（首次部署者生成一次，之后不换）。Via-154.cmd 不需改（它读本地 .env）。

- [ ] **T4.8: 测试**

新建 `tests/test_internal_voice_catalog_access.py`：

```python
import pytest
from fastapi.testclient import TestClient

def test_missing_token_returns_403(app_client_localhost, monkeypatch):
    """No X-Internal-Key header → 403."""
    monkeypatch.setattr("gateway.config.settings.internal_api_key", "test-secret-32-chars-xxxxxxxxxxxxxx")
    r = app_client_localhost.get("/api/internal/voice-catalog?provider=cosyvoice")
    assert r.status_code == 403

def test_wrong_token_returns_403(app_client_localhost, monkeypatch):
    monkeypatch.setattr("gateway.config.settings.internal_api_key", "correct-key-32-chars-xxxxxxxxxxxxx")
    r = app_client_localhost.get(
        "/api/internal/voice-catalog?provider=cosyvoice",
        headers={"X-Internal-Key": "wrong-key"},
    )
    assert r.status_code == 403

def test_correct_token_ok(app_client_localhost, monkeypatch):
    key = "correct-key-32-chars-xxxxxxxxxxxxx"
    monkeypatch.setattr("gateway.config.settings.internal_api_key", key)
    r = app_client_localhost.get(
        "/api/internal/voice-catalog?provider=cosyvoice",
        headers={"X-Internal-Key": key},
    )
    assert r.status_code == 200  # or 404 if no voices seeded, but not auth error

def test_startup_refuses_empty_key():
    """validate_internal_api_key must raise on empty/short key."""
    from gateway.main import validate_internal_api_key
    with pytest.raises(RuntimeError, match="AVT_INTERNAL_API_KEY"):
        validate_internal_api_key("")
    with pytest.raises(RuntimeError, match="AVT_INTERNAL_API_KEY"):
        validate_internal_api_key("short")
    validate_internal_api_key("a" * 32)  # long enough: no raise
```

- [ ] **T4.9: 跑测试 + 提交**

```bash
python -m pytest tests/test_internal_voice_catalog_access.py -v
git add Caddyfile gateway/config.py gateway/voice_catalog_api.py gateway/main.py docker-compose.yml .env.example tests/test_internal_voice_catalog_access.py
git commit -m "fix(gateway): block /api/internal/* at Caddy; enforce X-Internal-Key unconditionally"
```

---

### Task 5: Job API 生产环境不泄漏异常详情（DRY helper）

**目标：** `except Exception` 兜底分支不把 `str(exc)` 回给客户端，改统一 `internal_error`，详情只进日志。**[Claude Code 建议] 抽 helper 方法避免 N 处散落漂移。**

**Files:**
- Modify: `src/services/jobs/api.py`（handler class 加 `_send_sanitized_error`；所有 `except Exception` 兜底调它）
- Test: `tests/test_job_api_error_handling.py`（新建）

---

- [ ] **T5.1: 定位所有兜底位置**

```bash
grep -n "except Exception" src/services/jobs/api.py
```

预期 do_GET / do_POST / do_PUT 等多处。每处都会改为调 helper。

- [ ] **T5.2: 在 handler class 里加 `_send_sanitized_error` helper**

`src/services/jobs/api.py`（BaseHTTPRequestHandler 子类内）：

```python
import logging
logger = logging.getLogger(__name__)

def _send_sanitized_error(self, exc: Exception) -> None:
    """Log full exception context; return generic 500 to client.

    Prevents leaking internal paths/stack/DSN/password from str(exc).
    Centralized here so all `except Exception` fallbacks stay consistent
    — if we want to change format later, one place to edit.
    """
    logger.exception(
        "Unhandled exception in Job API handler path=%s method=%s",
        self.path, self.command,
    )
    self._write_json(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        {"error": "internal_error", "message": "服务器内部错误，请重试或联系管理员"},
    )
```

- [ ] **T5.3: 替换所有 `except Exception` 兜底**

原代码（line 273-274）：
```python
except Exception as exc:  # pragma: no cover
    self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
```

改为：
```python
except Exception as exc:  # pragma: no cover
    self._send_sanitized_error(exc)
```

对 `do_GET` / `do_POST` / `do_PUT` / 任何 `except Exception as exc` + `{"error": str(exc)}` 的地方全改成调 helper。

- [ ] **T5.4: 测试**

新建 `tests/test_job_api_error_handling.py`：

```python
def test_unexpected_exception_returns_generic_error(job_api_test_client, monkeypatch):
    """Generic exception with sensitive substring must not leak to response body."""
    from src.services.jobs import service
    monkeypatch.setattr(
        service, "require_job",
        lambda jid: (_ for _ in ()).throw(RuntimeError("DB password=hunter2 path=/secret")),
    )
    response = job_api_test_client.get("/jobs/fake-id")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    # Sensitive substrings must not leak
    assert "hunter2" not in response.text
    assert "password" not in response.text.lower()
    assert "/secret" not in response.text


def test_known_error_types_still_show_message(job_api_test_client):
    """JobNotFoundError / ValueError etc. have specific user-facing messages —
    helper should NOT intercept them (they're raised/caught separately)."""
    response = job_api_test_client.get("/jobs/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.text  # job_id echoed in NotFoundError is OK
```

- [ ] **T5.5: 跑测试 + 提交**

```bash
python -m pytest tests/test_job_api_error_handling.py -v
git add src/services/jobs/api.py tests/test_job_api_error_handling.py
git commit -m "fix(jobs): sanitize generic exception responses via centralized helper"
```

---

### Task 6: 生产强制 `auth_required=True`（整合进统一 startup）

**目标：** Gateway 启动时若 `AVT_ENV=production` 且 `auth_required=False`，raise 禁止启动。

**整合说明：** 与 T3（`init_db`）和 T4（`validate_internal_api_key`）放在同一 startup 区块，共用一个顺序：

```
1. validate_production_safety(env, auth_required)     # T6
2. validate_internal_api_key(internal_api_key)        # T4
3. init_db()                                          # T3
```

**执行先后依赖：** T3 和 T4 都要在 T6 之后合并，最终 `gateway/main.py` 只有一个 startup block 调 3 个纯函数 + init。

**Files:**
- Modify: `gateway/config.py`（加 `env` 字段）
- Modify: `gateway/main.py`（startup 统一区块）
- Test: `tests/test_gateway_startup_checks.py`（新建）

---

- [ ] **T6.1: config 加 `env` 字段**

`gateway/config.py` `GatewaySettings` 内：

```python
env: str = "dev"   # dev | staging | production
```

- [ ] **T6.2: main.py 加 startup 检查（纯函数）**

`gateway/main.py` 里加纯函数：

```python
def validate_production_safety(env: str, auth_required: bool) -> None:
    """Pure check: refuse to start if production mode has auth disabled.

    Standalone function so tests can call directly without reloading
    gateway.main (which triggers FastAPI app re-construction side effects).
    """
    if env == "production" and not auth_required:
        raise RuntimeError(
            "Refusing to start: AVT_ENV=production requires AVT_AUTH_REQUIRED=true. "
            "Disabling auth in production would expose all jobs to anonymous access."
        )
```

然后在 app 实例化后、路由挂载前统一调用：

```python
# Startup-time validations (all pure functions, testable independently).
validate_production_safety(settings.env, settings.auth_required)
validate_internal_api_key(settings.internal_api_key)  # T4
init_db()                                              # T3

app = FastAPI(...)
# ... routes mount ...
```

- [ ] **T6.3: 更新 `.env.example` / docker-compose**

确认生产 docker-compose 设置 `AVT_ENV=production`、`AVT_AUTH_REQUIRED=true`。

- [ ] **T6.4: 测试（直接测纯函数，不 reload 模块）**

```python
import pytest
from gateway.main import validate_production_safety

def test_production_with_auth_disabled_raises():
    with pytest.raises(RuntimeError, match="production requires"):
        validate_production_safety(env="production", auth_required=False)

def test_production_with_auth_enabled_ok():
    validate_production_safety(env="production", auth_required=True)  # no raise

def test_dev_with_auth_disabled_ok():
    validate_production_safety(env="dev", auth_required=False)  # no raise
```

- [ ] **T6.5: 跑测试 + 提交**

---

### Task 7: TTS fallback 可观测性（日志 + metadata）

**目标：** TTS provider 自动切换（MiniMax → CosyVoice）时，日志结构化输出 + 在 segment 产物 metadata 里写 `fallback_used_provider` 字段，用户可查哪段音色被悄悄换了。

**持久化路径已定位（grep 验证）：**
- `TTSResult` 定义在 [tts_generator.py:139-146](../../src/services/tts/tts_generator.py)，字段 `selected_voice` / `match_confidence` / `billed_chars`
- TTS 结果字段**不是 asdict 序列化**，而是在 `process.py` 里**手工拷贝到 DubbingSegment 后再手工构造 dict** 写 manifest
- 拷贝 point：[process.py:2380](../../src/pipeline/process.py) 有 `"match_confidence": result.match_confidence`（result → dict，匹配报表用）
- 最终 manifest 写入 point：[process.py:3905-3920](../../src/pipeline/process.py) 手工构造 segment dict，含 `selected_voice` / `match_confidence` / `tts_provider` 等字段

**改动链（已规划好，执行时无需盲猜）：**
1. `TTSResult` 加字段（tts_generator.py:139-146）
2. `DubbingSegment` 也加同名字段（具体位置 T7.2 grep 找）—— 因为 manifest 从 segment 读
3. TTS 生成侧：fallback 触发处把 `result.fallback_used_provider = fallback`
4. 拷贝到 segment：找 `segment.selected_voice = result.selected_voice` 的位置，同处加一行 `segment.fallback_used_provider = result.fallback_used_provider`
5. manifest 构造处（process.py:3920）：加 `"fallback_used_provider": segment.fallback_used_provider`

**Files:**
- Modify: `src/services/tts/tts_generator.py:139-146`（`TTSResult` 加字段）
- Modify: `src/services/tts/tts_generator.py:800-814`（fallback 触发处）
- Modify: `DubbingSegment` 定义（T7.2 grep 确认位置）
- Modify: `src/pipeline/process.py`（segment 字段拷贝点 + manifest 构造 line ~3920）
- Test: `tests/test_tts_fallback_observability.py`（新建）

---

- [ ] **T7.1: `TTSResult` 加 `fallback_used_provider` 字段**

`src/services/tts/tts_generator.py:139-146`：

```python
@dataclass
class TTSResult:
    segment_id: int
    audio_path: str
    duration_ms: int
    voice_id: str
    selected_voice: str = ""
    match_confidence: str = ""
    billed_chars: int = 0
    fallback_used_provider: str | None = None   # NEW: provider name if fallback used, else None
```

- [ ] **T7.2: `DubbingSegment` 加同名字段**

```bash
grep -rn "class DubbingSegment\|DubbingSegment.*dataclass\|selected_voice\s*:" src/ --include="*.py" | head -10
```

找到 `DubbingSegment` 定义（预期在 `src/services/` 或 `src/pipeline/` 下的 models 文件），加：

```python
fallback_used_provider: str | None = None   # NEW: mirrors TTSResult for manifest
```

- [ ] **T7.3: 改 fallback 触发处记录（TTS 生成侧）**

`src/services/tts/tts_generator.py:800-814`：

```python
fallback = get_fallback_provider(provider, voice_clone_enabled)
if fallback:
    logger.warning(
        "tts_fallback_triggered segment=%s primary=%s fallback=%s reason=%s",
        segment.segment_id, provider, fallback, last_error,
    )
    try:
        result = self._generate_one(segment, output_dir, provider=fallback)
        result.fallback_used_provider = fallback   # NEW: mark on result
        return result
    except TTSGenerationError as fb_exc:
        logger.error(
            "tts_fallback_failed segment=%s fallback=%s error=%s",
            segment.segment_id, fallback, fb_exc,
        )
        # Continue to pause-and-retry below
```

- [ ] **T7.4: 拷贝到 segment + 写入 manifest（pipeline 侧）**

```bash
# 找到 result → segment 字段拷贝点
grep -n "segment\.selected_voice\s*=\|segment\.match_confidence\s*=" src/pipeline/process.py
```

在每处拷贝点附加：
```python
segment.fallback_used_provider = result.fallback_used_provider
```

然后在 [process.py:3920](../../src/pipeline/process.py) 的 manifest 构造处（紧跟 `"tts_provider": segment.tts_provider` 之后）加：
```python
"fallback_used_provider": segment.fallback_used_provider,
```

- [ ] **T7.5: 测试**

```python
def test_fallback_writes_field_to_result(monkeypatch, tmp_path):
    """When primary provider fails but fallback succeeds, TTSResult.fallback_used_provider is set."""
    # Mock _generate_one: raise TTSGenerationError on primary, succeed on fallback
    # Assert: returned result.fallback_used_provider == <fallback_name>
    # Assert: logger.warning was called with 'tts_fallback_triggered'


def test_primary_success_leaves_field_none():
    """Normal path: primary succeeds, fallback_used_provider stays None."""
    # ...
```

- [ ] **T7.6: 跑测试 + 提交**

```bash
python -m pytest tests/test_tts_fallback_observability.py -v
git add src/services/tts/tts_generator.py src/pipeline/process.py <DubbingSegment文件> tests/test_tts_fallback_observability.py
git commit -m "feat(tts): record fallback provider in TTSResult and segment manifest"
```

**验收：** 跑一个故意让 MiniMax 失败的集成场景（或手工 inject fallback），检查最终 segments 产物 JSON 里能看到 `fallback_used_provider: "cosyvoice"` 字段。

- [ ] **T7.5: 测试**

```python
def test_fallback_writes_metadata(monkeypatch, tmp_path):
    # Mock _generate_one: first call (primary) raises, second call (fallback) returns
    # Assert: returned TTSResult.fallback_used_provider == <fallback_name>
    # Assert: logger.warning was called with 'tts_fallback_triggered'
```

- [ ] **T7.6: 跑测试 + 提交**

```bash
python -m pytest tests/test_tts_fallback_observability.py -v
git add src/services/tts/tts_generator.py tests/test_tts_fallback_observability.py
git commit -m "feat(tts): record fallback provider in segment metadata for traceability"
```

---

### Task 8: Security hardening（密码 + cookie）

**目标：** 密码最短 6 → 12；cookie samesite lax → strict。

**邮箱验证流交互确认（[Claude Code 提出的关注点]）：**
- 项目当前 `email_registration_enabled=False` 默认关闭（[config.py:53](../../gateway/config.py)）
- 唯一活跃的认证流是**手机验证码**（不发邮件链接，无"点邮箱链接跳回"场景）
- Cookie `samesite=strict` 的典型破坏场景是"从外部站点导航进来 cookie 不带" —— 本项目**无此流程**
- 仅剩风险：未来若重启邮箱注册 + 加"点邮件确认链接"流程，strict 可能让点链接后 session 丢。届时需单独讨论
- **结论：本批次改 strict 无业务影响**

**Files:**
- Modify: `gateway/auth.py:85+`（set_cookie samesite）
- Modify: `gateway/auth.py:159`（密码长度）
- Test: `tests/test_auth_phone.py` 或 `tests/test_auth_registration.py`（若存在）

---

- [ ] **T8.1: 改 cookie samesite**

`gateway/auth.py:85`（`response.set_cookie(...)` 附近）：

```python
samesite="strict",
```

- [ ] **T8.2: 改密码最短**

`gateway/auth.py:159`：

```python
if len(body.password) < 12:
    raise HTTPException(status_code=400, detail="密码至少 12 位")
```

- [ ] **T8.3: 更新测试（如已有邮箱注册测试）**

若 `test_auth_phone.py` 或类似测试用 6 位密码，批量改成 12 位。

- [ ] **T8.4: 跑测试 + 提交**

```bash
python -m pytest tests/test_auth_phone.py -v
git add gateway/auth.py tests/test_auth_*.py
git commit -m "fix(auth): enforce 12-char password minimum and samesite=strict cookie"
```

---

## 6. 执行顺序 & 依赖

**依赖链（修订后）：**

```
T3 (config lazy + database.py lazy engine + main.py stub startup block)
  └─→ T6 (piggyback on main.py startup: validate_production_safety)
       └─→ T4 (piggyback on main.py startup: validate_internal_api_key;
                + Caddyfile block + docker-compose env wiring)

T1 (credits FOR UPDATE)          ─ 独立（但 tests 需 T3 的 init_db 可用）
T2 (continue lock + transition)  ─ 独立（同上）
T5 (error handler DRY helper)    ─ 独立
T7 (TTS fallback observability)  ─ 独立
T8 (auth hardening)              ─ 独立
```

**推荐执行顺序：T3 → T6 → T4 → T1 → T2 → T5 → T7 → T8**

**理由：**
- T3 必须先做 —— 其他所有涉及 DB 的测试都依赖 `init_db()` 可用。T3 做完后 `import gateway.config` / `import gateway.database` 都是 side-effect-free
- T6 紧跟 T3 —— 复用同一个 main.py startup block
- T4 紧跟 T6 —— 再复用 startup block 加第三个校验（`validate_internal_api_key`），同时改 Caddyfile 和 docker-compose
- T1 / T2 —— 核心并发修复，中段做，需要 DB 可用
- T5 / T7 / T8 —— 边界清理，收尾，互不依赖可并行做

---

## 7. 测试策略

**每个 Task 自己的单元测试必须跑通**，此外在全部合并后跑：

```bash
# Python 全量测试
python -m pytest tests/ -v --tb=short

# 前端类型检查（本批次不改前端，但确保未破坏）
cd frontend-next && npm run lint && cd ..

# 如项目有集成测试，跑相关段落
python -m pytest tests/test_credits_*.py tests/test_gateway_*.py tests/test_auth_*.py -v
```

**回归风险热区：**
- `test_credits_service.py` — T1 可能影响既有 reserve/capture 测试断言（特别是事务语义）
- `test_gateway_create_job.py` + `test_async_tts.py` — T2 可能让测试 fixture 需要更仔细的并发控制
- 所有 Job API 集成测试 — T5 的错误响应格式变了

**⚠️ 数据库要求：**
- **T1.5 的并发测试必须在 PostgreSQL 下跑**。SQLite `SELECT FOR UPDATE` 是 no-op，测试会假绿（两个事务都能过锁直接继续）。若本地 conftest 默认用 in-memory SQLite，给 T1.5 测试加 `@pytest.mark.postgres` 或用 `sqlalchemy.url` fixture 强制切到 `TEST_DATABASE_URL` 环境变量指向的 PG 实例。
- T2.3 的并发测试同理——gateway 的 `Job` 表 FOR UPDATE 必须对着 PG 跑。
- 若 CI 没有 PG，在 CI 里起一个 docker Postgres 服务，或将这两条测试标记 integration 级只在部署前手工跑。

---

## 8. 回滚方案

每个 Task 独立 commit，单独 revert 安全。若生产出问题：

- T1 回滚：revert credits_service.py commit，行锁移除，回到原始并发漏洞状态
- T2 回滚：revert gateway/job_intercept.py 的 `_lock_and_check_continue`
- T3 回滚：极不建议——回退即恢复 avt:avt 弱密码 fallback，仅在紧急情况且已确认生产 env 正确时做
- T6 回滚：临时设 `AVT_ENV=dev` 绕过 startup 检查（不要 revert 代码）

---

## 9. 上线检查单

合并到 main 前确认：

- [ ] 所有 Task 的单元测试 PASS
- [ ] `python -m pytest tests/ -v` 全量 PASS（或只有已知无关 skip）
- [ ] 前端 `npm run lint` 通过
- [ ] 生产 `.env` / docker-compose 已设置 `AVT_ENV=production`, `AVT_AUTH_REQUIRED=true`, `AVT_PG_PASSWORD`, `AVT_INTERNAL_API_KEY`
- [ ] 生产 docker-compose 的 gateway 服务重启能通过 `_startup_checks()`
- [ ] 回滚脚本准备好（见 §8）
- [ ] 部署后手工验证：① 登录流程 ② 创建一个测试 job 能扣 credits ③ 一个 waiting_for_review 的 job 能 continue

---

## 10. 后续批次（本方案之外）

以下问题已识别但推迟：

1. **全局 `voice_registry.json` 改 user-scoped / 迁 DB** — 改动大，独立方案
2. **拆 `main.py` 桌面子命令（control-panel / web-ui 等）** — 架构清理
3. **S2 Pass 1 LLM fallback 链改"抛给用户决定"** — 成本优化 + UX 决策，需要产品讨论
4. **TTS 失败段无上限重试改有界** — 成本优化
5. **`VoiceRegistry.save()` 读改写竞态** — 若未来高并发注册音色才关键
6. **`_cache` 模块级单例 TTL + mutex** — 低风险，顺手优化
7. **死代码清理**：`frontend/`、`build/`、`tmp_local_video_repro/`、`scripts/start_remote_workbench.ps1`、根目录 `projects/`

建议每完成一批 Critical + 生产观察稳定后，再挑 1-2 条推进。
