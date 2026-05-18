# Pan Backup Implementation — Phase 5b Session Handoff

> **新会话读这个文件,5 分钟内能续上 Phase 5b 实施。**

**Last session ended:** 2026-05-18(Phase 1-4 + 5a 完成,Phase 3/4 各两轮 CodeX 修)
**Next phase:** Phase 5b — backup/restore/cleanup executors(T5.2 — T5.12,~10 task,2 工日)

---

## 1. 必读文档(按顺序)

1. **本文件** — Phase 5b 起点 + 测试 infra 设计要求
2. `docs/plans/2026-05-18-pan-backup-session-handoff.md` — Phase 3 起点(已 superseded,但保留作为 Phase 5 之前的完整背景)
3. `docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md` — Phase 5 规范 line 2384..3026
4. `docs/plans/2026-05-13-admin-pan-backup-design.md` — 设计 spec(已经过 4 轮 review + CodeX 修订)
5. `CLAUDE.md`(项目根)— 项目硬约束:付费 API / 容器部署 / 远端脚本

---

## 2. 当前 git 状态(验证用)

```bash
git log --oneline -6
# 期望(最近 6,Phase 5a + handoff + 2 CodeX 修):
#   84553c7 fix(pan-backup): set_archive_status raises on 0-row UPDATE (CodeX P1)
#   4cc8913 fix(pan-backup): safe_extract_tar strict type-byte allowlist (CodeX P1)
#   abf842e docs(pan-backup): Phase 5b session handoff doc
#   9b84807 feat(pan-backup): T5.1 set_archive_status (PG + JSON, no mirror)
#   fd6c84b feat(pan-backup): T5.11.5 safe_extract_tar (reject ../ /abs/ symlink/hardlink)
#   1b56b75 fix(pan-backup): deepcopy snapshot fields in build_manifest (CodeX P3)

git fetch origin
git rev-list --count HEAD..origin/main  # 应为 0
git rev-list --count origin/main..HEAD  # 应为 0
```

如果任一不通过,**先停手报告**,不要继续。

---

## 3. 已完成(不要重做)

### Phase 1 — Schema(`e120dc1..739bd55`,7 tasks)
### Phase 2 — Token Crypto + Config(`1e885d0..961882b`,5 tasks)
### Phase 3 — Baidu Pan API Client(`9997399..cee036a`,8 task)+ 3 CodeX 修(`f9145c7..182d4bc`)
### Phase 4 — Manifest helpers(`988c3ce..7479f98`,4 task)+ 3 CodeX 修(`427f0fe..1b56b75`)

详细 commit 表见 `docs/plans/2026-05-18-pan-backup-session-handoff.md` §3。

### Phase 5a — Isolated tasks(2 commits)

| Task | Commit | 内容 |
|---|---|---|
| T5.11.5 | `fd6c84b` | `safe_extract_tar` — reject ../ / /abs/ / symlink / hardlink(restore-time;Pass 1 validate-all 然后 Pass 2 extract)|
| T5.1 | `9b84807` | `set_archive_status(user_id, job_id, new_status, *, conn)` — PG UPDATE + best-effort JSON mirror,**source-text 契约 guard 锁死 "no mirror_job_terminal_state"** |

**测试统计:** Phase 1-4 + 5a 全部 pan 相关 test pass(`-k "pan or alembic_029"`: 156 passed)。

---

## 4. Phase 5b 任务清单(下一会话要做)

读 plan `# Phase 5` 段(line 2384..3026)。剩 10 个 task:

| Task | 主题 | 工日 | 复杂度 | 备注 |
|---|---|---|---|---|
| T5.2 | `execute_pan_backup` 骨架 — precondition + advisory lock + INSERT backup_records | 0.3 | **高** | 单连接长持模式 |
| T5.3 | tar.gz + manifest 构建(step d-f) | 0.2 | 中 | `asyncio.to_thread` wrap |
| T5.4 | pan client upload 调用(step g) | 0.1 | 低 | 复用 T3.6 |
| T5.5 | 三道闸门 verification(step h) | 0.2 | 中 | size / md5 / read-back |
| T5.6 | COMMIT POINT — UPDATE backup_records(step i)| 0.2 | **高** | 越过此点不 rollback |
| T5.7 | rmtree project_dir safety guard(step j) | 0.2 | 中 | dest 校验防 rm 错路径 |
| T5.8 | R2 artifacts deletion(step k) | 0.2 | 中 | idempotent |
| T5.9 | status='archived'(step l) | 0.1 | 低 | 复用 T5.1 |
| T5.10 | backup 完整 happy path 集成测试 | 0.2 | 中 | mock client + full pipeline |
| T5.11 | restore_executor 实现 | 0.5 | **高** | 镜像 backup 流程 + safe_extract_tar(T5.11.5 已 ready)|
| T5.11.6 | bg_task 表复用策略 + recover_stale 测试 | 0.1 | 低 | doc + 1 test |
| T5.12 | residue_cleanup executor | 0.3 | 中 | stale_reaper forward-resolve 用 |

---

## 5. ⚠️ 测试 infrastructure 设计要求(Phase 5b 起点)

**关键约束:** plan 5.2 起的 test code 引用了 `async_session_factory` / `sample_job` / `sample_job_succeeded` / `sample_job_running` / `mock_pan_client` 这些 fixture。**它们当前都不存在**(`tests/conftest.py` 只有 16 行 sys.path bootstrap)。

**已经验证的模式(借鉴 `tests/test_materials_pack_executor.py`):**

```python
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return "JSON"

@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):
    return "CHAR(36)"

async def _setup_engine_with_table(table_cls):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: table_cls.__table__.create(c))
    return engine
```

Phase 5a 的 `tests/test_pan_status_mutator.py` 也用了这个模式 — Job 表跑 SQLite,工作完美。

**Phase 5b 需要新建的 fixtures(建议在 `tests/conftest.py` 或新 `tests/pan_fixtures.py`):**

1. `make_engine_with_models(*model_classes)` — 多表 SQLite 启动 + @compiles 套用
2. `make_sample_job(engine, *, status='succeeded', edit_generation=0, project_dir='/tmp/x', ...)` — 插入 Job + 返回 Job
3. `make_sample_backup_record(engine, *, job, status='uploading', ...)` — 插入 BackupRecord
4. `make_sample_pan_credentials(engine, *, user_id, provider='baidu_pan', status='active', ...)` — 插入 PanCredentials(token 加密走 T2.3)
5. `FakeBaiduPanClient` — record calls without real HTTP;支持 inject failure modes(upload returns wrong md5,verify_remote_tail returns False)

**advisory lock 的 SQLite 兼容性问题:**

`pg_advisory_lock(:k)` 是 PG-only。SQLite 没有等效。两种处理:

- **方案 A(推荐)**: 在 executor 内部抽 `_acquire_session_lock(conn, key)` 助手,SQLite 实现成 no-op 或用 file_lock 替代。Unit test 跑 SQLite 不验真 lock 行为,只验 happy path。Live PG smoke test 单独跑(可推到 Phase 10 integration)
- **方案 B**: 把 advisory lock 测试标 `@pytest.mark.postgres`,需要真 PG fixture(testcontainers-postgres 或 docker compose up postgres)

C2 修订要求"单连接长持" — 这跟 SQLite 兼容。`async with engine.connect() as conn:` 工作正常。

**asyncio.to_thread:**

测试时 mock 阻塞 I/O 必须用 **同步函数**(`def` 不是 `async def`)。
`asyncio.to_thread(callable, *args)` 调用 `callable(*args)` 在线程池里跑;
如果 `callable` 是 `async def`,调用它只产生 coroutine object 不执行它,
然后 `await asyncio.to_thread(async_fn, ...)` 拿到的是 coroutine 而**不是 result**,
测试会拿到怪状态或 hang(CodeX P2 找到的错误 pattern)。

```python
def fake_upload(*args, **kw):
    # 同步函数,被 asyncio.to_thread 包后正常返回值
    return {'size': 100, 'md5': 'fakemd5', 'fs_id': 'fakefs'}

monkeypatch.setattr(client, 'upload', fake_upload)
# production code: result = await asyncio.to_thread(client.upload, tar_path, ...)
# 测试时 to_thread 真的跑 fake_upload 同步,返回 dict
```

或者完全绕过 `to_thread`,直接 monkeypatch `asyncio.to_thread` 自己:

```python
async def passthrough_to_thread(fn, *args, **kw):
    return fn(*args, **kw)
monkeypatch.setattr('asyncio.to_thread', passthrough_to_thread)
```

但 production code 必须用 `await asyncio.to_thread(client.upload, ...)`,
否则 sync requests 冻 event loop 10+ 小时。

---

## 6. 执行节奏(从 Phase 3 总结的)

### 6.1 启动 subagent-driven-development(可选)

```
/superpowers:subagent-driven-development
```

把 plan + 本 handoff 路径传进去,告 implementer 从 T5.2 起。但 T5.2-T5.6 涉及多步状态机,verbatim copy 价值低,implementer 需要做设计判断 — **建议直接做,不 dispatch**。

### 6.2 每个 task 的标准流程

```
1. Pre-flight
   - git fetch origin
   - git rev-list --count HEAD..origin/main  # 应为 0
   - 测试 baseline:python -m pytest tests/test_pan_*.py 全 pass

2. 实现该 task
   - 写测试(基于 fixture pattern)
   - 跑 fail
   - 实现该 step
   - 跑通过

3. Pre-commit staged-diff guard
   - git add <explicit files>
   - git diff --cached --name-only  → 只应有目标文件
   - git diff --cached <main file> | grep "^\+" | head -30
     → 应只见你的添加,无 source_*/smart_*/disk_resize_* 漂移

4. Commit + 进下个 task
```

### 6.3 Batch push

每 phase 末 push,不要单 task push(减少跟 smart MVP rebase)。Phase 5 完成时一次推送 T5.2-T5.12 全部 10 commit。

### 6.4 模型选择

- **Sonnet**: T5.4 / T5.5 / T5.8 / T5.9 / T5.10 / T5.11.6 / T5.12(单一职责,完整 spec)
- **Opus**: T5.2 / T5.3 / T5.6 / T5.7 / T5.11(状态机 / commit point / 数据安全敏感)

---

## 7. ⚠️ 不要做的事

- ❌ 单 task push
- ❌ 创建 worktree / 新分支(项目硬约束)
- ❌ `git add -A` / `git add .`
- ❌ 改 plan / design 文件(已 4 轮 review 锁定)
- ❌ Windows 本地跑真 PG migration(没 docker;静态验证用 alembic ScriptDirectory)
- ❌ 自动调付费 API(MiniMax clone 等)
- ❌ **commit point 后 rollback**:T5.6 越过 `backup_records.status='uploaded'` 后,任何失败 → `log + 继续 j/k/l`,不 rollback。`stale_reaper` 之后兜底
- ❌ 不在 `set_archive_status` 调用 `mirror_job_terminal_state`(T5.1 的 source-text guard 会 fail build)
- ❌ 不让阻塞 I/O 直接跑(必须 `asyncio.to_thread`)

---

## 8. Phase 5 特殊注意点

### 8.1 单连接长持模式(CodeX C2)

```python
async with engine.connect() as conn:
    # precondition 短 txn
    async with conn.begin():
        # SELECT job + cred
        ...

    # advisory lock(本 conn 持有,session-level lock 才有效)
    await conn.execute(text("SELECT pg_advisory_lock(:k)"), {'k': lock_key})
    try:
        # 一堆短 txn(均 begin() ... commit())
        async with conn.begin():
            await set_archive_status(..., conn=conn)

        async with conn.begin():
            # INSERT backup_records
            ...

        # 阻塞 I/O via to_thread,不动 conn
        await asyncio.to_thread(...)

        # COMMIT POINT
        async with conn.begin():
            # UPDATE backup_records.status='uploaded'
            ...
        # ↑ 越过此点失败 → log,不 rollback
    finally:
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {'k': lock_key})
# connection 还池
```

### 8.2 三道闸门(plan §7 step h)

```python
assert upload_result['size'] == tar_path.stat().st_size   # h1
assert upload_result['md5'] == md5                         # h2
read_back_ok = await asyncio.to_thread(
    client.verify_remote_tail, tar_path, remote_path,
    size=upload_result['size'], access_token=access_token,
)
if not read_back_ok:                                       # h3
    raise RuntimeError("Read-back probe mismatch — refuse to delete local")
```

三道任一 fail → rollback backup_records=failed + status 回 succeeded,**不删 local**。

### 8.3 COMMIT POINT 边界(T5.6)

```python
async with conn.begin():
    await conn.execute(
        update(BackupRecord)
        .where(BackupRecord.id == br_id)
        .values(
            status='uploaded',
            remote_path=remote_path,
            sha256=sha256, md5=md5,
            size_bytes=upload_result['size'],
            manifest_json=manifest,
            completed_at=datetime.now(timezone.utc),
        )
    )
# ↑ 越过此点,backup 数据已 commit。失败 → log,不 rollback
# T5.7 rmtree / T5.8 R2 delete / T5.9 status='archived' 都是 post-commit cleanup
```

### 8.4 rmtree 安全(T5.7)

```python
project_dir = Path(job.project_dir).resolve()
# 双重校验:
# - 必须在 settings.projects_dir 下
# - 必须 != settings.projects_dir 本身
projects_root = settings.projects_dir.resolve()
if not project_dir.is_relative_to(projects_root):
    raise RuntimeError(f"project_dir {project_dir} escape from {projects_root}")
if project_dir == projects_root:
    raise RuntimeError(f"project_dir cannot be projects_root itself")
await asyncio.to_thread(shutil.rmtree, project_dir, ignore_errors=False)
```

### 8.5 复用现有 background_tasks 表(CodeX Q-A,T5.11.6)

**Source of truth 是 `backup_records.status` + `heartbeat_at`,不是 `background_tasks.status`。**

- Gateway 启动期 `recover_stale` 把 running/pending bg_task 标 failed(line 408)
- pan_stale_reaper(Phase 8)看 backup_records.heartbeat_at:< now - 4h + status='uploading' + job.status='archiving' → 触发 residue_cleanup
- UI `GET /api/admin/pan/backups` 读 backup_records,**不**读 background_tasks

---

## 9. 文件位置速查

```
gateway/pan/
├── __init__.py             # ✅ T2.3
├── token_crypto.py          # ✅ T2.3
├── provider_protocol.py     # ✅ T3.1
├── baidu_pan_client.py      # ✅ T3.1-T3.8
├── manifest.py              # ✅ T4.1-T4.4 + T5.11.5
├── status_mutator.py        # ✅ T5.1
├── backup_executor.py?      # ⏳ T5.2-T5.10 — 加到 gateway/background_task_executors.py 末尾
├── restore_executor.py?     # ⏳ T5.11 — 同上
└── residue_cleanup.py?      # ⏳ T5.12 — 同上

tests/
├── test_baidu_pan_client.py  # ✅ 37 tests
├── test_pan_manifest.py      # ✅ 22 tests
├── test_pan_safe_tar.py      # ✅ 9 tests (T5.11.5)
├── test_pan_status_mutator.py  # ✅ 8 tests (T5.1)
├── test_backup_executor.py   # ⏳ T5.2-T5.10
├── test_restore_executor.py  # ⏳ T5.11
└── test_residue_cleanup.py   # ⏳ T5.12
```

Plan §3.1 还规划了 Phase 5 之后的 `gateway/pan/auth.py`(Phase 6)/ `archive_scanner.py` / `orphan_cleanup.py` / `stale_reaper.py`(Phase 8)— 那是后续阶段的事。

---

## 10. 开场 prompt 模板(粘到新会话)

```
继续 admin pan backup implementation,从 Phase 5b 开始。

读 docs/plans/2026-05-18-pan-backup-phase5b-handoff.md 全文,验证
git state(§2),然后:

1. 先扩展 tests 测试 infrastructure — 在 tests/conftest.py(或新建
   tests/pan_fixtures.py)写出 §5 列出的 5 个 fixtures:
   make_engine_with_models / make_sample_job / make_sample_backup_record
   / make_sample_pan_credentials / FakeBaiduPanClient

2. 跑一个 smoke test 验证 fixtures 自身工作(就建一个 Job + BackupRecord
   往返,不调用 executor),commit。

3. 然后按 §4 顺序做 T5.2-T5.12,每 task 标准流程(§6.2)。

4. Phase 5 完成,batch push 10+ 个 commit。

工作目录已经在 main 分支,不要开 worktree。
```

End of handoff.
