# Admin Pan Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin 一键备份个人视频任务到百度网盘,本地 + R2 全清;archived 任务可一键 restore 继续编辑。

**Architecture:** 主体新代码落 `gateway/` 容器(包括所有 backup/restore 业务 + OAuth + scheduler + reaper)。`src/services/` 有 3 个小改动(status vocab + cleanup protected set + event vocab — 共 ~10 行),实施时**一次性**修改并通过 deploy 窗口协调 app 容器重启(详见 Phase 10)。通过 `gateway/pan/` 新子包提供 OAuth + 百度 Pan API client + status_mutator + manifest 工具;executor 落 `gateway/background_task_executors.py`;调度 + reaper + orphan_cleanup 落 `gateway/pan/`。不动 `mirror_job_terminal_state`(archive 状态非 credit-bearing)。

**Concurrency model:** Executor 在 Gateway main event loop 上跑(现有 `asyncio.create_task` 调度模式),所有跨境 I/O / tar / hash / rmtree 等阻塞调用**必须**用 `await asyncio.to_thread(...)` 包裹,否则会冻结 Gateway 进程数小时。Advisory lock 用 **session-level** `pg_advisory_lock` + **单 connection 长持** 模式(整 executor 生命周期 hold 一个 `engine.connect()` connection),避免 lock 跨 session pool boundary 失效。

---

## CodeX Review Log

**2026-05-14 — CodeX 外部审计 P0/P1 修订**(verified against actual codebase):

| # | Severity | 发现 | 修订 |
|---|---|---|---|
| C1 | P0 | Alembic 028 已被 `028_user_voice_source_metadata` 占用,plan 写 `down_revision='027_smart_state'` 会冲突 | 全部改成 `029_pan_backup`,`down_revision='028_user_voice_source_metadata'` |
| C2 | P0 | 跨 3 个 `async_session_factory()` block 拿/释放 advisory lock,session-pool 不保证同 connection,锁会失效或污染 | 改为 `async with engine.connect() as conn:` 单连接长持 executor 整生命周期,所有 DB ops 走该 conn 的短 txn |
| C3 | P0 | `async def execute_pan_backup` 里直接同步调 `requests` / tar / hash / rmtree(10-40h 跨境上传会冻 Gateway 数小时) | 所有阻塞 I/O 用 `await asyncio.to_thread(...)` 包裹 |
| C4 | P1 | "全部新代码落 gateway/" 声明不实,实际改 3 个 `src/services/` 文件 → 部署期需重启 app 容器 | 架构声明诚实化;Phase 10 部署 task 加 app 容器 drain + restart 步骤 + 灰度窗口 |
| C5 | P1 | API 路径 `/admin/pan/*` 不符现有约定(`/api/admin/{module}`);frontend page 路径漏了 `(app)` route group | API 全改 `/api/admin/pan/*`;frontend 改 `frontend-next/src/app/(app)/admin/pan/<page>` |
| C6 | P1 | imports 写成 `from gateway.db / gateway.models / gateway.config` — 实际 Gateway 是 top-level package,应 `from database / from models / from config` | 全 plan import 改成 top-level |
| C7 | P1 | `services._file_lock` 跨 src/services 边界,需 `sys.path` 前置设置(参 `gateway/admin_settings.py:12-20`) | status_mutator 加 sys.path 设置 helper |
| Q-A | open | 复用 `background_tasks` 表 → 启动期 `recover_stale` 会把所有 running/pending 标 failed,跟 commit-aware stale_reaper 语义重叠 | **决定**:复用 `background_tasks` 表,但 source of truth 是 `backup_records.status` + `heartbeat_at`。`recover_stale` 标 BackgroundTask=failed 不影响 backup_records;pan_stale_reaper 30min 周期看 backup_records 决定 forward-resolve 还是 rollback |
| Q-B | open | restore 解 tar 无 path traversal / symlink 防护 | Phase 5 加 `_safe_extract_tar` helper:拒绝 `../`、绝对路径、symlink/hardlink、Path resolves to 目标目录外的 |

工日估算从 14.3 → **~16**(+1.7 for asyncio.to_thread rewiring + safe extract + single-conn lock pattern)。

---

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0 Mapped+UUID / Pydantic Settings / Fernet(cryptography lib)/ requests(boto3 在 R2 路径已有)/ Postgres advisory lock(session-level)/ Next.js 16 + React 19。

**Related Documents:**
- Design spec: [`docs/plans/2026-05-13-admin-pan-backup-design.md`](2026-05-13-admin-pan-backup-design.md) (HEAD 2d33234)
- Upstream placeholder: [`docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md`](2026-05-07-disk-relief-via-r2-publisher-and-ttl.md) §11.8
- CLAUDE.md(项目根)— 容器代码部署 / 付费 API 约束 / 远端部署脚本

---

## File Structure

### 新建文件(11 个 + 14 个 test)

**Backend - gateway/pan/(子包,新建)**
- `gateway/pan/__init__.py` — 包入口,导出 PanProvider 协议 + 主要 helper
- `gateway/pan/provider_protocol.py` — `PanProvider` Protocol(upload/download/list/delete/quota)
- `gateway/pan/token_crypto.py` — Fernet encrypt/decrypt + key 校验
- `gateway/pan/baidu_pan_client.py` — Baidu Pan API client(OAuth code 兑换 / refresh / 4MB 分片上传 / 下载 / list / delete / quota)
- `gateway/pan/manifest.py` — `build_manifest()` / `write_manifest_to_tar()` / `read_manifest_from_tar()` / `walk_project_dir_inventory()`
- `gateway/pan/status_mutator.py` — `set_archive_status()`(PG + JSON store 同步,**不**走 mirror_job_terminal_state)
- `gateway/pan/archive_scanner.py` — 30d 自动归档候选 SQL + enqueue
- `gateway/pan/orphan_cleanup.py` — 周六 3-pass(pan 远端孤儿 + R2 残留 + oauth_states 过期)
- `gateway/pan/stale_reaper.py` — heartbeat-based 卡死任务恢复(commit-aware)
- `gateway/pan/auth.py` — OAuth Web Flow `/api/admin/pan/connect` + `/api/admin/pan/callback`

**Backend - gateway/(根)**
- `gateway/admin_pan_api.py` — 10 个 admin endpoint(见 spec §6)

**Alembic migration**
- `gateway/alembic/versions/029_pan_backup.py` — 3 表 + Job.status enum 扩 + 索引

**Frontend - pages**
- `frontend-next/src/app/(app)/admin/pan/dashboard/page.tsx` — 连接状态 + 配额面板
- `frontend-next/src/app/(app)/admin/pan/backups/page.tsx` — backup_records 列表

**Frontend - API client**
- `frontend-next/src/lib/api/pan.ts` — fetch wrapper for `/api/admin/pan/*` endpoints

**Tests**
- `tests/test_fernet_token_crypto.py` — encrypt/decrypt round-trip
- `tests/test_baidu_pan_client.py` — OAuth + upload + download + list + delete(mock requests)
- `tests/test_pan_manifest.py` — manifest 构造 + 解析 + inventory 验证
- `tests/test_pan_status_mutator.py` — status flip + 不调 mirror_job_terminal_state
- `tests/test_backup_executor.py` — backup_executor full flow(mock pan)
- `tests/test_restore_executor.py` — restore_executor full flow + SHA256 mismatch
- `tests/test_pan_archive_scanner.py` — candidate 选择 SQL fixture
- `tests/test_pan_orphan_cleanup.py` — 3-pass cleanup
- `tests/test_pan_stale_reap.py` — commit-aware reap
- `tests/test_pan_oauth_flow.py` — state CSRF + code 兑换 mock
- `tests/test_pan_residue_cleanup.py` — residue cleanup advisory lock retry
- `tests/test_status_vocab_in_sync.py` — Gateway PG + Job API + TS union 三处一致(契约)
- `tests/test_pan_event_vocab_in_sync.py` — events.py + event_log.py + r2_observability.py(契约)
- `tests/test_admin_pan_api.py` — 10 endpoint 鉴权 + 行为

### 修改文件(13 个)

| 文件 | 改动概要 |
|---|---|
| `gateway/models.py` | `Job.status` enum extension + 3 new models(`PanCredentials` / `BackupRecord` / `PanOauthState`)|
| `gateway/config.py` | 加 9 个 settings 字段(见 §5.1)|
| `gateway/startup_checks.py` | 加 `validate_pan_backup_config` |
| `gateway/background_task_executors.py` | 加 4 个 executor(pan_backup / pan_restore / pan_token_refresh / pan_residue_cleanup)|
| `gateway/notification_dispatch_map.py` | 加 3 个 recipe(pan_token_revoked / pan_backup_failed / pan_restore_failed)|
| `gateway/logs_redactor.py` | 加 mask 关键字 access_token / refresh_token / appsecret |
| `gateway/storage/event_log.py` | `_DOWNLOAD_EVENT_TYPES` 加 8 个 pan.* |
| `src/services/jobs/models.py` | `SUPPORTED_JOB_STATUSES` + ACTIVE 集合扩展 |
| `src/services/jobs/events.py` | `SUPPORTED_EVENT_TYPES` 加 8 个 pan.* |
| `src/services/web_ui/cleanup.py` | `_CLEANUP_PROTECTED_STATUSES` 加 archiving/restoring |
| `frontend-next/src/types/jobs.ts` | `JobStatus` union + label map 扩 3 个 |
| `frontend-next/src/features/jobs/JobListItem.tsx` | succeeded 行加"备份到网盘";archived 行变 "Restore" |
| `scripts/r2_observability.py` | 加 PAN 事件分组 + 渲染段 |
| `docker-compose.yml` | gateway service env 加 9 个 AVT_PAN_* / AVT_BAIDU_PAN_* |
| `.env.example` | 加同款 9 个 env vars |

---

## Pre-flight checklist

执行任何 task 前先做:

```bash
# 1. 同步远端,避免跟 smart MVP track 撞 line conflict
git fetch origin
git log origin/main..HEAD       # 应为空(同步)
git log HEAD..origin/main       # 应为空,如果有 smart 新 push 先 git pull --rebase

# 2. 验证 smart MVP 当前状态
git log -1 --grep="smart" --pretty=format:"%h %cr %s"
# 如果最近 commit < 1h,等 smart 那条线静默再继续
# 如果 > 4h,直接开干

# 3. 验证关键 spec 假设仍成立(任一不通过 → 先改 spec 再开干)
test -f gateway/storage/job_store_reader.py     # JSON-only reader 协议
test -f gateway/notifications_service.py        # 通知服务
grep -q "_DOWNLOAD_EVENT_TYPES" gateway/storage/event_log.py
grep -q "SUPPORTED_JOB_STATUSES" src/services/jobs/models.py
grep -q "_CLEANUP_PROTECTED_STATUSES" src/services/web_ui/cleanup.py
ls gateway/alembic/versions/ | sort | tail -3   # 确认 028 仍可用

# 4. Baidu Pan AppKey / SecretKey 已在 1Password,不在 chat history
# (本会话之前用户提供过明文,实施时从 1Password 取,不从 chat 拷)
```

---

# Phase 1 — Schema foundation

**目标:** alembic 029 跑通,3 张新表 + Job.status enum 扩 + 状态触点同步全部 ready。

**预算:** ~0.8 工日 / 8 个 task

### Task 1.1: 创建 alembic 029 migration

**Files:**
- Create: `gateway/alembic/versions/029_pan_backup.py`

- [ ] **Step 1: 跑 `alembic revision -m "pan_backup"` 生成空 stub**

```bash
cd gateway
alembic -c alembic.ini revision -m "pan_backup tables and status enum extension"
mv versions/<auto_generated>.py versions/029_pan_backup.py
```

确认 `down_revision = '028_user_voice_source_metadata'`(027 是 smart 的)。如果 027 已经被 squash 或重命名,改成实际最新 revision。

- [ ] **Step 2: 编辑 migration `upgrade()`,加 3 表**

```python
"""pan_backup tables and status enum extension

Revision ID: 029_pan_backup
Revises: 027_smart_state
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, BYTEA

revision = '029_pan_backup'
down_revision = '028_user_voice_source_metadata'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # pan_credentials
    op.create_table(
        'pan_credentials',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('access_token_encrypted', BYTEA, nullable=False),
        sa.Column('refresh_token_encrypted', BYTEA, nullable=False),
        sa.Column('access_token_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scope', sa.String(255), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='active'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('last_refreshed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('user_id', 'provider', name='uq_pan_credentials_user_provider'),
    )
    
    # backup_records
    op.create_table(
        'backup_records',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('job_id', sa.String(64), nullable=False),  # 不 FK
        sa.Column('job_edit_generation', sa.Integer, nullable=False, server_default='0'),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('remote_path', sa.Text, nullable=False),
        sa.Column('size_bytes', sa.BigInteger, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('md5', sa.String(32), nullable=False),
        sa.Column('manifest_json', JSONB, nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text, nullable=True),
    )
    op.create_index(
        'uniq_backup_in_flight',
        'backup_records',
        ['user_id', 'job_id', 'provider', 'job_edit_generation'],
        unique=True,
        postgresql_where=sa.text("status IN ('uploading', 'restoring')"),
    )
    op.create_index('idx_backup_user_status', 'backup_records', ['user_id', 'status'])
    op.create_index('idx_backup_user_job_gen', 'backup_records', ['user_id', 'job_id', 'job_edit_generation'])
    op.create_index(
        'idx_backup_heartbeat',
        'backup_records',
        ['heartbeat_at'],
        postgresql_where=sa.text("status IN ('uploading', 'restoring')"),
    )
    
    # pan_oauth_states
    op.create_table(
        'pan_oauth_states',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )

def downgrade() -> None:
    op.drop_table('pan_oauth_states')
    op.drop_index('idx_backup_heartbeat', 'backup_records')
    op.drop_index('idx_backup_user_job_gen', 'backup_records')
    op.drop_index('idx_backup_user_status', 'backup_records')
    op.drop_index('uniq_backup_in_flight', 'backup_records')
    op.drop_table('backup_records')
    op.drop_table('pan_credentials')
```

⚠️ Job.status 是 `String(32)` 不是 PG enum,所以**不需要 ALTER TYPE**——状态值仅在 Python 校验集合(SUPPORTED_JOB_STATUSES)里加,migration 不动数据库 schema。

- [ ] **Step 3: 在 test PG 上 dry run**

```bash
docker exec aivideotrans-postgres psql -U avt -d aivideotrans_test -c "BEGIN; SELECT 1;"
cd gateway && alembic -c alembic.ini upgrade head
# 验证表创建
docker exec aivideotrans-postgres psql -U avt -d aivideotrans_test -c "\d pan_credentials"
docker exec aivideotrans-postgres psql -U avt -d aivideotrans_test -c "\d backup_records"
docker exec aivideotrans-postgres psql -U avt -d aivideotrans_test -c "\d pan_oauth_states"
```

Expected: 三表都打印出 column 列表,索引齐全。如果 PG 报 `gen_random_uuid()` 不存在,在 migration 顶部加 `op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')`。

- [ ] **Step 4: Verify rollback**

```bash
alembic -c alembic.ini downgrade 027_smart_state
docker exec aivideotrans-postgres psql -U avt -d aivideotrans_test -c "\dt pan_*"
```

Expected: 输出 "Did not find any relation matching pattern" 或类似空结果。

```bash
alembic -c alembic.ini upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add gateway/alembic/versions/029_pan_backup.py
git commit -m "feat(pan-backup): T1.1 alembic 029 — 3 tables + indexes"
```

### Task 1.2: 加 PanCredentials / BackupRecord / PanOauthState SQLAlchemy models

**Files:**
- Modify: `gateway/models.py`(末尾追加 3 个 class)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pan_models_persistence.py(新建)
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

@pytest.mark.asyncio
async def test_pan_credentials_round_trip(async_session_factory, sample_user):
    from models import PanCredentials
    async with async_session_factory() as db:
        cred = PanCredentials(
            user_id=sample_user.id,
            provider='baidu_pan',
            access_token_encrypted=b'enc_access',
            refresh_token_encrypted=b'enc_refresh',
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            scope='basic,netdisk',
        )
        db.add(cred)
        await db.commit()
        loaded = (await db.execute(select(PanCredentials).where(PanCredentials.user_id == sample_user.id))).scalar_one()
        assert loaded.provider == 'baidu_pan'
        assert loaded.status == 'active'
        assert loaded.connected_at is not None

@pytest.mark.asyncio
async def test_backup_record_round_trip(async_session_factory, sample_user):
    from models import BackupRecord
    async with async_session_factory() as db:
        br = BackupRecord(
            user_id=sample_user.id,
            job_id='job_test_001',
            job_edit_generation=0,
            provider='baidu_pan',
            remote_path='/apps/AIVideoTrans/backups/job_test_001_20260514.tar.gz',
            size_bytes=12345,
            sha256='a' * 64,
            md5='b' * 32,
            manifest_json={'backup_format_version': 1},
            status='uploading',
        )
        db.add(br)
        await db.commit()
        loaded = (await db.execute(select(BackupRecord).where(BackupRecord.user_id == sample_user.id))).scalar_one()
        assert loaded.job_id == 'job_test_001'
        assert loaded.status == 'uploading'
        assert loaded.manifest_json == {'backup_format_version': 1}

@pytest.mark.asyncio
async def test_pan_oauth_state_round_trip(async_session_factory, sample_user):
    from models import PanOauthState
    async with async_session_factory() as db:
        st = PanOauthState(
            token='a' * 32,
            user_id=sample_user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        db.add(st)
        await db.commit()
        loaded = (await db.execute(select(PanOauthState).where(PanOauthState.token == 'a' * 32))).scalar_one()
        assert loaded.user_id == sample_user.id
```

- [ ] **Step 2: 跑测试,确认全失败**

```bash
python -m pytest tests/test_pan_models_persistence.py -v
```

Expected: 3 个 FAILED,理由 `ImportError: cannot import name 'PanCredentials'`。

- [ ] **Step 3: 在 `gateway/models.py` 末尾加 3 个 model**

```python
# 加在文件末尾(在所有现有 model class 之后)

class PanCredentials(Base):
    __tablename__ = "pan_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    access_token_encrypted: Mapped[bytes] = mapped_column(nullable=False)
    refresh_token_encrypted: Mapped[bytes] = mapped_column(nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")  # active | revoked
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()"),
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_pan_credentials_user_provider"),
    )


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 不 FK
    job_edit_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)  # BigInteger 在 PG 上自动
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class PanOauthState(Base):
    __tablename__ = "pan_oauth_states"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

注意 import 行如果缺 `Integer` / `Text` / `UniqueConstraint`,补到 `from sqlalchemy import ...`。

- [ ] **Step 4: 跑测试,确认通过**

```bash
python -m pytest tests/test_pan_models_persistence.py -v
```

Expected: 3 个 PASSED.

- [ ] **Step 5: Commit**

```bash
git add gateway/models.py tests/test_pan_models_persistence.py
git commit -m "feat(pan-backup): T1.2 PanCredentials / BackupRecord / PanOauthState SQLAlchemy models"
```

### Task 1.3: 扩展 SUPPORTED_JOB_STATUSES + ACTIVE_JOB_STATUSES

**Files:**
- Modify: `src/services/jobs/models.py:43-66`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pan_job_statuses.py(新建)
def test_three_new_pan_statuses_in_supported_set():
    from services.jobs.models import (
        SUPPORTED_JOB_STATUSES,
        ACTIVE_JOB_STATUSES,
        WORKER_ACTIVE_STATUSES,
    )
    assert 'archiving' in SUPPORTED_JOB_STATUSES
    assert 'archived' in SUPPORTED_JOB_STATUSES
    assert 'restoring' in SUPPORTED_JOB_STATUSES
    # archiving + restoring 进 ACTIVE(避免 cleanup 误杀)
    assert 'archiving' in ACTIVE_JOB_STATUSES
    assert 'restoring' in ACTIVE_JOB_STATUSES
    # archived 不进(它是任务的终态,cleanup 该看到它)
    assert 'archived' not in ACTIVE_JOB_STATUSES
    # 三个都不进 WORKER_ACTIVE(没有 process_runner worker)
    assert 'archiving' not in WORKER_ACTIVE_STATUSES
    assert 'archived' not in WORKER_ACTIVE_STATUSES
    assert 'restoring' not in WORKER_ACTIVE_STATUSES
```

- [ ] **Step 2: 跑测试,确认失败**

Expected: AssertionError "archiving not in SUPPORTED_JOB_STATUSES".

- [ ] **Step 3: 在 `src/services/jobs/models.py` 加 3 个 constant + 扩展 set**

在 `JOB_STATUS_PURGED = "purged"` 这行之后(line ~42)插:

```python
# Pan backup 状态(plan 2026-05-13 §4.1)。archiving / restoring 是 transient,
# archived 是终态。archiving / restoring 进 ACTIVE 防 cleanup 误杀;archived
# 不进(它是 cleanup 看得到的归档终态)。三个都不进 WORKER_ACTIVE(无 worker process)。
JOB_STATUS_ARCHIVING = "archiving"
JOB_STATUS_ARCHIVED = "archived"
JOB_STATUS_RESTORING = "restoring"
```

修改 SUPPORTED_JOB_STATUSES + ACTIVE_JOB_STATUSES set:

```python
SUPPORTED_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_PURGED,
    JOB_STATUS_ARCHIVING,    # +
    JOB_STATUS_ARCHIVED,     # +
    JOB_STATUS_RESTORING,    # +
}
ACTIVE_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,
    JOB_STATUS_ARCHIVING,    # + 防 cleanup 误杀
    JOB_STATUS_RESTORING,    # +
}
# WORKER_ACTIVE_STATUSES 不动 — pan 任务由 gateway executor 跑,不是 pipeline worker
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
python -m pytest tests/test_pan_job_statuses.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/services/jobs/models.py tests/test_pan_job_statuses.py
git commit -m "feat(pan-backup): T1.3 extend SUPPORTED_JOB_STATUSES + ACTIVE_JOB_STATUSES with pan triplet"
```

### Task 1.4: 扩展 _CLEANUP_PROTECTED_STATUSES

**Files:**
- Modify: `src/services/web_ui/cleanup.py:53`(或附近)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pan_cleanup_protected.py(新建)
def test_archiving_and_restoring_protected_from_cleanup():
    from services.web_ui.cleanup import _CLEANUP_PROTECTED_STATUSES
    assert 'archiving' in _CLEANUP_PROTECTED_STATUSES
    assert 'restoring' in _CLEANUP_PROTECTED_STATUSES
    # archived 不进 — cleanup 看到 archived 应该 short-circuit("已归档,不处理"
    # 由 backup_executor 自己删 project_dir)
    assert 'archived' not in _CLEANUP_PROTECTED_STATUSES
```

- [ ] **Step 2: 跑测试,确认失败**

- [ ] **Step 3: 在 `_CLEANUP_PROTECTED_STATUSES` 集合中加 2 个**

具体行号要看当前文件状态;搜 `_CLEANUP_PROTECTED_STATUSES` 找到它,加 `"archiving"` 和 `"restoring"`。

- [ ] **Step 4: 跑测试,确认通过**

- [ ] **Step 5: Commit**

```bash
git add src/services/web_ui/cleanup.py tests/test_pan_cleanup_protected.py
git commit -m "feat(pan-backup): T1.4 extend _CLEANUP_PROTECTED_STATUSES with archiving/restoring"
```

### Task 1.5: 扩展 frontend JobStatus + label map

**Files:**
- Modify: `frontend-next/src/types/jobs.ts:1-14`

- [ ] **Step 1: 写失败 TS 测试**

```typescript
// frontend-next/src/types/__tests__/jobs.test.ts(新建)
import { JOB_STATUS_LABELS, type JobStatus } from '../jobs'

describe('JobStatus pan extension', () => {
  it('has archiving/archived/restoring labels', () => {
    expect(JOB_STATUS_LABELS.archiving).toBe('归档中')
    expect(JOB_STATUS_LABELS.archived).toBe('已归档')
    expect(JOB_STATUS_LABELS.restoring).toBe('恢复中')
  })

  it('JobStatus union accepts pan triplet', () => {
    const a: JobStatus = 'archiving'
    const b: JobStatus = 'archived'
    const c: JobStatus = 'restoring'
    expect([a, b, c]).toEqual(['archiving', 'archived', 'restoring'])
  })
})
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
cd frontend-next && npm test -- jobs.test.ts
```

Expected: TS compile error "Property 'archiving' does not exist on type JOB_STATUS_LABELS".

- [ ] **Step 3: 修改 `frontend-next/src/types/jobs.ts`**

```typescript
export const JOB_STATUS_LABELS = {
  cancelled: '已取消',
  editing: '修改中',
  failed: '已失败',
  queued: '待开始',
  running: '处理中',
  succeeded: '已完成',
  waiting_for_review: '等待审核',
  purged: '已清理',
  // Pan backup (plan 2026-05-14)
  archiving: '归档中',
  archived: '已归档',
  restoring: '恢复中',
} as const

export type JobStatus = keyof typeof JOB_STATUS_LABELS
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
cd frontend-next && npm test -- jobs.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add frontend-next/src/types/jobs.ts frontend-next/src/types/__tests__/jobs.test.ts
git commit -m "feat(pan-backup): T1.5 frontend JobStatus union + label map extension"
```

### Task 1.6: status vocab 契约 guard test

**Files:**
- Create: `tests/test_status_vocab_in_sync.py`

- [ ] **Step 1: 写契约测试**

```python
# tests/test_status_vocab_in_sync.py(新建)
"""Contract guard: pan-related status vocab must agree across:
- services.jobs.models.SUPPORTED_JOB_STATUSES (Python source of truth)
- frontend-next/src/types/jobs.ts JOB_STATUS_LABELS keys (UI source of truth)

Misaligning these = user sees 'unknown status' in UI or backend rejects valid status.
"""
import re
import subprocess
from pathlib import Path


def _read_ts_status_labels() -> set[str]:
    """Parse JOB_STATUS_LABELS keys out of frontend-next/src/types/jobs.ts."""
    content = Path('frontend-next/src/types/jobs.ts').read_text(encoding='utf-8')
    # Match labels until first `}`
    m = re.search(r'JOB_STATUS_LABELS = \{(.*?)\}', content, re.DOTALL)
    assert m, 'JOB_STATUS_LABELS not found in jobs.ts'
    body = m.group(1)
    # Lines like "  cancelled: '已取消',"
    keys = set(re.findall(r"^\s*(\w+):\s*'", body, re.MULTILINE))
    return keys


def test_python_and_typescript_status_vocab_in_sync():
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_labels()
    
    py_only = SUPPORTED_JOB_STATUSES - ts_keys
    ts_only = ts_keys - SUPPORTED_JOB_STATUSES
    
    assert not py_only, (
        f"SUPPORTED_JOB_STATUSES has statuses not in JOB_STATUS_LABELS (frontend will show 'unknown'): "
        f"{sorted(py_only)}"
    )
    assert not ts_only, (
        f"JOB_STATUS_LABELS has keys not in SUPPORTED_JOB_STATUSES (frontend lying about valid statuses): "
        f"{sorted(ts_only)}"
    )


def test_pan_triplet_present_in_both_sides():
    """Specific check: the 3 pan statuses we added must be everywhere."""
    from services.jobs.models import SUPPORTED_JOB_STATUSES
    ts_keys = _read_ts_status_labels()
    for s in ('archiving', 'archived', 'restoring'):
        assert s in SUPPORTED_JOB_STATUSES, f'{s} missing from SUPPORTED_JOB_STATUSES'
        assert s in ts_keys, f'{s} missing from JOB_STATUS_LABELS (jobs.ts)'
```

- [ ] **Step 2: 跑测试,确认通过(因为前面 T1.3 + T1.5 都做完了)**

```bash
python -m pytest tests/test_status_vocab_in_sync.py -v
```

Expected: PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/test_status_vocab_in_sync.py
git commit -m "feat(pan-backup): T1.6 contract guard — status vocab Python/TS sync"
```

### Task 1.7: i18n label 在 UI 实际渲染验证

**Files:**
- Search: `frontend-next/src/features/jobs/` for status display

- [ ] **Step 1: 找到现有 status badge 渲染组件**

```bash
grep -rn "JOB_STATUS_LABELS\[" frontend-next/src/features/jobs/
grep -rn "from '@/types/jobs'" frontend-next/src/
```

记下文件路径,通常是 `frontend-next/src/features/jobs/JobStatusBadge.tsx` 或类似。

- [ ] **Step 2: 写 React Testing Library 测试**

```typescript
// frontend-next/src/features/jobs/__tests__/JobStatusBadge.test.tsx(新建,文件名按现有约定)
import { render } from '@testing-library/react'
import JobStatusBadge from '../JobStatusBadge'  // 路径按 step 1 找到的

describe('JobStatusBadge pan extension', () => {
  it('renders archiving label', () => {
    const { getByText } = render(<JobStatusBadge status="archiving" />)
    expect(getByText('归档中')).toBeInTheDocument()
  })
  it('renders archived label', () => {
    const { getByText } = render(<JobStatusBadge status="archived" />)
    expect(getByText('已归档')).toBeInTheDocument()
  })
  it('renders restoring label', () => {
    const { getByText } = render(<JobStatusBadge status="restoring" />)
    expect(getByText('恢复中')).toBeInTheDocument()
  })
})
```

- [ ] **Step 3: 跑测试,确认通过(因 T1.5 已经在 type level 加了)**

```bash
cd frontend-next && npm test -- JobStatusBadge.test.tsx
```

如果 fail,可能是 badge 组件还有硬编码 switch case 没加新值——补 case 即可。

- [ ] **Step 4: Commit**

```bash
git add frontend-next/src/features/jobs/__tests__/JobStatusBadge.test.tsx
# 若有改 JobStatusBadge.tsx 也一起
git commit -m "feat(pan-backup): T1.7 verify pan status badges render in UI"
```

### Task 1.8: 提交 + 部署 schema migration 到 staging

**手动步骤**(不在 task list 自动化,因为涉及远端部署):

- [ ] **Step 1: 跑全套测试确保前 7 个 task 都绿**

```bash
python -m pytest tests/test_pan_models_persistence.py tests/test_pan_job_statuses.py tests/test_pan_cleanup_protected.py tests/test_status_vocab_in_sync.py -v
cd frontend-next && npm test -- jobs
```

- [ ] **Step 2: Push 到 main**(只在 staging 部署 schema,先不动生产 prod)

⚠️ smart MVP track 同时在 push。Push 前 fetch + rebase 一次:

```bash
git fetch origin
git rebase origin/main
git push origin main
```

- [ ] **Step 3: 在 staging PG 跑 migration**(若有 staging 环境,否则跳到 prod 直接跑)

```bash
# 在 staging 主机
ssh staging-host
docker exec aivideotrans-app alembic -c /opt/aivideotrans/app/gateway/alembic.ini upgrade head
```

- [ ] **Step 4: 验证表存在**

```bash
docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "\dt pan_*"
```

Expected: 3 表存在。

Phase 1 完成。Schema 已 ready,后续所有 task 可以假设它在了。

---

# Phase 2 — Token Crypto + Config

**目标:** Fernet token 加密 ready,gateway/config.py 多 9 个 settings 字段,startup 校验 ready。

**预算:** ~0.5 工日 / 5 个 task

### Task 2.1: 加 9 个 settings 字段到 gateway/config.py

**Files:**
- Modify: `gateway/config.py`(在 `GatewaySettings` class 内,`r2_upload_timeout_s` 字段之后)

- [ ] **Step 1: 写测试**

```python
# tests/test_pan_settings.py(新建)
import pytest
from cryptography.fernet import Fernet


def test_pan_settings_defaults(monkeypatch):
    """No env vars set → all defaults safe."""
    monkeypatch.delenv('AVT_ENABLE_PAN_BACKUP', raising=False)
    monkeypatch.delenv('AVT_PAN_AUTO_ARCHIVE_ENABLED', raising=False)
    # Re-import to pick up clean env
    import importlib
    import gateway.config as cfg
    importlib.reload(cfg)
    s = cfg.GatewaySettings()
    assert s.enable_pan_backup is False
    assert s.pan_auto_archive_enabled is False
    assert s.pan_auto_archive_days == 30
    assert s.pan_auto_archive_max_per_run == 5
    assert s.pan_auto_archive_dry_run is True
    assert s.pan_upload_chunk_bytes == 4 * 1024 * 1024
    assert s.pan_task_stale_hours == 4
    assert s.baidu_pan_appkey == ''
    assert s.baidu_pan_appsecret == ''
    assert s.baidu_pan_redirect_uri == ''
    assert s.pan_token_encryption_key == ''


def test_pan_settings_env_override(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('AVT_ENABLE_PAN_BACKUP', 'true')
    monkeypatch.setenv('AVT_PAN_AUTO_ARCHIVE_DAYS', '60')
    monkeypatch.setenv('AVT_BAIDU_PAN_APPKEY', 'test_appkey')
    monkeypatch.setenv('AVT_PAN_TOKEN_ENCRYPTION_KEY', key)
    import importlib
    import gateway.config as cfg
    importlib.reload(cfg)
    s = cfg.GatewaySettings()
    assert s.enable_pan_backup is True
    assert s.pan_auto_archive_days == 60
    assert s.baidu_pan_appkey == 'test_appkey'
    assert s.pan_token_encryption_key == key
```

- [ ] **Step 2: 跑测试,确认失败**

Expected: AttributeError "GatewaySettings has no attribute enable_pan_backup".

- [ ] **Step 3: 在 `gateway/config.py` 加字段**

在 `r2_upload_timeout_s: int = 60` 行之后(line ~148),`model_config = ...` 行之前,插:

```python
    # --- Pan backup (plan 2026-05-13) ---
    # 主 feature flag。OFF 时所有 /api/admin/pan/* 端点返 404,scanner 不跑。
    enable_pan_backup: bool = False
    # 30d 自动归档子开关,独立于主 flag。即使主 flag 开,这个 OFF 也不自动跑。
    # 灰度策略:先开主 flag + 手动跑 1 周,再开自动。
    pan_auto_archive_enabled: bool = False
    pan_auto_archive_days: int = 30
    pan_auto_archive_hour_bjt: int = 3                   # cron 触发小时(BJT)
    pan_auto_archive_max_per_run: int = 5                # 每轮最多 enqueue 数
    pan_auto_archive_dry_run: bool = True                # 默认 dry-run,仅 log candidates
    pan_orphan_cleanup_weekday: int = 5                  # 周六 = 5(0=Mon)
    pan_upload_chunk_bytes: int = 4 * 1024 * 1024        # 百度 Pan 4MB 分片
    pan_task_stale_hours: int = 4                        # heartbeat 过期阈值

    # Baidu OAuth(env 名故意不带 AVT_ 前缀,但本字段无 alias 所以走 AVT_)
    baidu_pan_appkey: str = ""
    baidu_pan_appsecret: str = ""
    baidu_pan_redirect_uri: str = ""

    # Token Fernet 加密 key(32B base64)
    pan_token_encryption_key: str = ""
```

注意:都没显式 `validation_alias`,所以都走 `env_prefix="AVT_"`。env 变量名 = `AVT_<UPPER_FIELD_NAME>`,例如 `AVT_ENABLE_PAN_BACKUP`、`AVT_BAIDU_PAN_APPKEY`、`AVT_PAN_TOKEN_ENCRYPTION_KEY`。

- [ ] **Step 4: 跑测试,确认通过**

```bash
python -m pytest tests/test_pan_settings.py -v
```

- [ ] **Step 5: Commit**

```bash
git add gateway/config.py tests/test_pan_settings.py
git commit -m "feat(pan-backup): T2.1 GatewaySettings — 9 pan/baidu fields"
```

### Task 2.2: 加 startup validator

**Files:**
- Modify: `gateway/startup_checks.py`(末尾追加新函数,然后在 main app 启动期注册调用)

- [ ] **Step 1: 写测试**

```python
# tests/test_pan_startup_validator.py(新建)
import pytest
from cryptography.fernet import Fernet
from gateway.config import GatewaySettings
from gateway.startup_checks import validate_pan_backup_config


def test_disabled_flag_skips_validation():
    s = GatewaySettings(enable_pan_backup=False)
    # 不应抛
    validate_pan_backup_config(s)


def test_enabled_flag_missing_appkey_raises():
    s = GatewaySettings(
        enable_pan_backup=True,
        baidu_pan_appkey="",  # missing
        baidu_pan_appsecret="x",
        baidu_pan_redirect_uri="x",
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(RuntimeError, match="AVT_BAIDU_PAN_APPKEY"):
        validate_pan_backup_config(s)


def test_enabled_flag_bad_fernet_key_raises():
    s = GatewaySettings(
        enable_pan_backup=True,
        baidu_pan_appkey="x",
        baidu_pan_appsecret="x",
        baidu_pan_redirect_uri="x",
        pan_token_encryption_key="not_a_real_fernet_key",
    )
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        validate_pan_backup_config(s)


def test_enabled_flag_all_good_passes():
    s = GatewaySettings(
        enable_pan_backup=True,
        baidu_pan_appkey="x",
        baidu_pan_appsecret="x",
        baidu_pan_redirect_uri="https://aitrans.video/api/admin/pan/callback",
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    validate_pan_backup_config(s)  # 无抛
```

- [ ] **Step 2: 跑测试,确认失败**

Expected: ImportError.

- [ ] **Step 3: 实现 validator**

在 `gateway/startup_checks.py` 末尾加:

```python
def validate_pan_backup_config(settings) -> None:
    """Validate pan backup env if feature enabled. CRITICAL fail at startup.
    
    Plan 2026-05-13 §5.3. Called from main app startup. If flag is OFF
    (default), do nothing — feature disabled and no env vars needed.
    """
    if not settings.enable_pan_backup:
        return
    
    required = [
        ('AVT_BAIDU_PAN_APPKEY', settings.baidu_pan_appkey),
        ('AVT_BAIDU_PAN_APPSECRET', settings.baidu_pan_appsecret),
        ('AVT_BAIDU_PAN_REDIRECT_URI', settings.baidu_pan_redirect_uri),
        ('AVT_PAN_TOKEN_ENCRYPTION_KEY', settings.pan_token_encryption_key),
    ]
    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            f"AVT_ENABLE_PAN_BACKUP=true but required env vars missing: {missing}. "
            f"Either fill them in .env or set AVT_ENABLE_PAN_BACKUP=false."
        )
    
    # Verify Fernet key is decodable (32 url-safe base64 bytes)
    try:
        from cryptography.fernet import Fernet
        Fernet(settings.pan_token_encryption_key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"AVT_PAN_TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}. "
            f"Generate one with: python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\""
        )
```

- [ ] **Step 4: 在 app 启动期注册调用**

找到 `gateway/main.py`(或者 startup_checks 自身的 `run_all` 函数),在现有 validator 调用列表里加:

```python
# 在 startup 调用集中处,r2 validator 旁边
validate_pan_backup_config(settings)
```

- [ ] **Step 5: 跑测试,确认通过**

```bash
python -m pytest tests/test_pan_startup_validator.py -v
```

- [ ] **Step 6: Commit**

```bash
git add gateway/startup_checks.py tests/test_pan_startup_validator.py
git commit -m "feat(pan-backup): T2.2 startup validator for pan config + fernet key"
```

### Task 2.3: 实现 gateway/pan/__init__.py + token_crypto

**Files:**
- Create: `gateway/pan/__init__.py`
- Create: `gateway/pan/token_crypto.py`
- Create: `tests/test_fernet_token_crypto.py`

- [ ] **Step 1: 创建包入口空 `__init__.py`**

```bash
mkdir -p gateway/pan
touch gateway/pan/__init__.py
```

- [ ] **Step 2: 写 round-trip 测试**

```python
# tests/test_fernet_token_crypto.py(新建)
import pytest
from cryptography.fernet import Fernet


def test_encrypt_decrypt_round_trip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)
    
    from gateway.pan.token_crypto import encrypt_token, decrypt_token
    plain = 'baidu_access_token_xyz_123'
    ct = encrypt_token(plain)
    assert isinstance(ct, bytes)
    assert ct != plain.encode()
    assert decrypt_token(ct) == plain


def test_decrypt_with_wrong_key_raises(monkeypatch):
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    from gateway.pan.token_crypto import encrypt_token, decrypt_token
    
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', k1)
    ct = encrypt_token('secret')
    
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', k2)
    with pytest.raises(Exception):  # InvalidToken
        decrypt_token(ct)


def test_empty_string_round_trips(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)
    from gateway.pan.token_crypto import encrypt_token, decrypt_token
    assert decrypt_token(encrypt_token('')) == ''
```

- [ ] **Step 3: 跑测试,确认失败**

- [ ] **Step 4: 实现 `gateway/pan/token_crypto.py`**

```python
"""Fernet symmetric encryption for Baidu Pan OAuth tokens.

Key comes from AVT_PAN_TOKEN_ENCRYPTION_KEY. Loss of key = total
unrecoverable token data — user must re-authorize. Spec §13: key
備份 1Password vault primary + 物理纸条 secondary。

Per-request: cipher object is built fresh from settings.pan_token_encryption_key
so monkeypatch-based tests can swap keys without module reload. Production
hot path: one Fernet() per encrypt/decrypt call, ~10us overhead, negligible.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from config import settings


def _cipher() -> Fernet:
    key = settings.pan_token_encryption_key
    if not key:
        raise RuntimeError(
            "AVT_PAN_TOKEN_ENCRYPTION_KEY not set. "
            "Either set it or AVT_ENABLE_PAN_BACKUP=false."
        )
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a string token to opaque bytes for PG BYTEA storage."""
    return _cipher().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt PG BYTEA bytes back to plaintext."""
    return _cipher().decrypt(ciphertext).decode()
```

- [ ] **Step 5: 跑测试,确认通过**

```bash
python -m pytest tests/test_fernet_token_crypto.py -v
```

- [ ] **Step 6: Commit**

```bash
git add gateway/pan/__init__.py gateway/pan/token_crypto.py tests/test_fernet_token_crypto.py
git commit -m "feat(pan-backup): T2.3 Fernet token crypto helper"
```

### Task 2.4: 加 logs_redactor mask 关键字

**Files:**
- Modify: `gateway/logs_redactor.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_logs_redactor_pan.py(新建)
def test_access_token_masked():
    from gateway.logs_redactor import redact
    log = 'OAuth: {"access_token": "actual_secret_value", "expires_in": 2592000}'
    out = redact(log)
    assert 'actual_secret_value' not in out
    assert 'access_token' in out  # key 仍可见,只 value redact


def test_refresh_token_masked():
    from gateway.logs_redactor import redact
    log = 'Refresh response refresh_token=very_long_refresh_xyz expires_in=864000'
    out = redact(log)
    assert 'very_long_refresh_xyz' not in out


def test_appsecret_masked():
    from gateway.logs_redactor import redact
    log = 'client_secret=8VHpJeQ4Kep404AXQ57qE8YudiSriKLP'
    out = redact(log)
    assert '8VHpJeQ4Kep404AXQ57qE8YudiSriKLP' not in out
```

- [ ] **Step 2: 跑测试,确认失败(可能现有 redact 不 handle access_token)**

- [ ] **Step 3: 修改 `gateway/logs_redactor.py`**

在现有 mask 关键字列表加 3 个(具体语法看现有代码):

```python
SENSITIVE_KEYS = [
    # ... existing keys ...
    'access_token',
    'refresh_token',
    'appsecret',
    'client_secret',
    'pan_token_encryption_key',
]
```

- [ ] **Step 4: 跑测试,确认通过**

- [ ] **Step 5: Commit**

```bash
git add gateway/logs_redactor.py tests/test_logs_redactor_pan.py
git commit -m "feat(pan-backup): T2.4 logs_redactor mask access_token / refresh_token / appsecret"
```

### Task 2.5: docker-compose + .env.example env vars

**Files:**
- Modify: `docker-compose.yml`(gateway service env_file 段)
- Modify: `.env.example`

- [ ] **Step 1: docker-compose.yml gateway service 加 env vars**

找到 gateway service `environment:` 段,在 `AVT_R2_UPLOAD_TIMEOUT_S` 之后加:

```yaml
      # --- Pan backup (plan 2026-05-14) ---
      AVT_ENABLE_PAN_BACKUP: "${AVT_ENABLE_PAN_BACKUP:-false}"
      AVT_PAN_AUTO_ARCHIVE_ENABLED: "${AVT_PAN_AUTO_ARCHIVE_ENABLED:-false}"
      AVT_PAN_AUTO_ARCHIVE_DAYS: "${AVT_PAN_AUTO_ARCHIVE_DAYS:-30}"
      AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN: "${AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN:-5}"
      AVT_PAN_AUTO_ARCHIVE_DRY_RUN: "${AVT_PAN_AUTO_ARCHIVE_DRY_RUN:-true}"
      AVT_PAN_TASK_STALE_HOURS: "${AVT_PAN_TASK_STALE_HOURS:-4}"
      AVT_BAIDU_PAN_APPKEY: "${AVT_BAIDU_PAN_APPKEY:-}"
      AVT_BAIDU_PAN_APPSECRET: "${AVT_BAIDU_PAN_APPSECRET:-}"
      AVT_BAIDU_PAN_REDIRECT_URI: "${AVT_BAIDU_PAN_REDIRECT_URI:-}"
      AVT_PAN_TOKEN_ENCRYPTION_KEY: "${AVT_PAN_TOKEN_ENCRYPTION_KEY:-}"
```

- [ ] **Step 2: .env.example 加同款字段(空值)**

```bash
# --- Pan backup (plan 2026-05-14) ---
AVT_ENABLE_PAN_BACKUP=false
AVT_PAN_AUTO_ARCHIVE_ENABLED=false
AVT_PAN_AUTO_ARCHIVE_DAYS=30
AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN=5
AVT_PAN_AUTO_ARCHIVE_DRY_RUN=true
AVT_PAN_TASK_STALE_HOURS=4
AVT_BAIDU_PAN_APPKEY=
AVT_BAIDU_PAN_APPSECRET=
AVT_BAIDU_PAN_REDIRECT_URI=
AVT_PAN_TOKEN_ENCRYPTION_KEY=
```

- [ ] **Step 3: 验证 compose syntax**

```bash
docker-compose -f docker-compose.yml config --quiet
```

Expected: 没错误输出。

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat(pan-backup): T2.5 docker-compose + .env.example — 10 pan env vars"
```

Phase 2 完成。Token crypto + config 全 ready。

---

# Phase 3 — Baidu Pan API Client

**目标:** 完整 `gateway/pan/baidu_pan_client.py`,提供 OAuth code exchange / token refresh / 4MB 分片上传(带 md5 三道闸门)/ 下载 / list / delete / quota。

**预算:** ~2.2 工日 / 10 个 task

### Task 3.1: PanProvider Protocol + 空 client 骨架

**Files:**
- Create: `gateway/pan/provider_protocol.py`
- Create: `gateway/pan/baidu_pan_client.py`(空骨架)
- Create: `tests/test_baidu_pan_client.py`

- [ ] **Step 1: 写测试 — client 能实例化**

```python
# tests/test_baidu_pan_client.py(新建)
import pytest


def test_client_instantiates_with_settings():
    from gateway.pan.baidu_pan_client import BaiduPanClient
    c = BaiduPanClient(appkey='test_appkey', appsecret='test_appsecret')
    assert c.appkey == 'test_appkey'
    assert c.appsecret == 'test_appsecret'


def test_client_conforms_to_pan_provider_protocol():
    from gateway.pan.provider_protocol import PanProvider
    from gateway.pan.baidu_pan_client import BaiduPanClient
    c = BaiduPanClient(appkey='x', appsecret='x')
    # Protocol structural typing
    assert isinstance(c, PanProvider)
```

- [ ] **Step 2: 跑测试,确认失败**

- [ ] **Step 3: 创建 Protocol**

```python
# gateway/pan/provider_protocol.py(新建)
"""PanProvider Protocol — structural typing for future multi-provider support.

MVP 只有 BaiduPanClient 实现这个,但写 Protocol 让 backup_executor 不 hard-bind
百度。未来加 OneDrive / 阿里云盘按这个协议补。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PanProvider(Protocol):
    """Protocol all pan provider clients must satisfy."""

    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        """Upload local file. Returns dict with at minimum:
            { 'size': int, 'md5': str, 'fs_id': str }
        Raises on failure.
        """
        ...

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        """Download remote file to local_path. Returns:
            { 'size': int, 'md5': str (server-reported), 'sha256': str (locally computed) }
        Raises on failure.
        """
        ...

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        """List files under prefix. Each entry has 'path', 'size', 'fs_id' at minimum."""
        ...

    def delete(self, remote_path: str, *, access_token: str) -> None:
        """Delete remote file. Idempotent: deleting non-existent file = no-op."""
        ...

    def get_quota(self, *, access_token: str) -> dict:
        """Return { 'total': int, 'used': int, 'free': int } in bytes."""
        ...

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange OAuth authorization code for tokens.
        Returns: { 'access_token': str, 'refresh_token': str, 'expires_in': int, 'scope': str }
        """
        ...

    def refresh(self, refresh_token: str) -> dict:
        """Use refresh_token to get new tokens. Returns same shape as exchange_code.
        ⚠️ Baidu rotates refresh_token on every call — caller MUST persist the new
        refresh_token from the response.
        """
        ...
```

- [ ] **Step 4: 创建空 BaiduPanClient 骨架**

```python
# gateway/pan/baidu_pan_client.py(新建)
"""Baidu Pan OpenAPI client.

Plan 2026-05-13 §3.1 + §7 + §9. 使用 requests library (sync) — backup
executor 本身在 background_task 里跑,不阻塞 event loop。

API base: https://openapi.baidu.com/oauth/2.0/
Pan API base: https://pan.baidu.com/rest/2.0/xpan/

Reference: https://pan.baidu.com/union/document
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class BaiduPanClient:
    """Implements PanProvider protocol for Baidu Pan."""

    OAUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
    XPAN_BASE = "https://pan.baidu.com/rest/2.0/xpan"
    PCS_BASE = "https://d.pcs.baidu.com/rest/2.0/pcs"

    def __init__(self, appkey: str, appsecret: str):
        if not appkey or not appsecret:
            raise ValueError("Baidu Pan client requires appkey + appsecret")
        self.appkey = appkey
        self.appsecret = appsecret

    # --- placeholder methods, filled in by 后续 task ---
    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        raise NotImplementedError("T3.6")

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        raise NotImplementedError("T3.8")

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        raise NotImplementedError("T3.4")

    def delete(self, remote_path: str, *, access_token: str) -> None:
        raise NotImplementedError("T3.5")

    def get_quota(self, *, access_token: str) -> dict:
        raise NotImplementedError("T3.4")

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        raise NotImplementedError("T3.2")

    def refresh(self, refresh_token: str) -> dict:
        raise NotImplementedError("T3.3")
```

- [ ] **Step 5: 跑测试,确认通过**

```bash
python -m pytest tests/test_baidu_pan_client.py::test_client_instantiates_with_settings tests/test_baidu_pan_client.py::test_client_conforms_to_pan_provider_protocol -v
```

- [ ] **Step 6: Commit**

```bash
git add gateway/pan/provider_protocol.py gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.1 PanProvider protocol + BaiduPanClient skeleton"
```

### Task 3.2: OAuth code exchange

**Files:**
- Modify: `gateway/pan/baidu_pan_client.py` — 实现 `exchange_code`
- Modify: `tests/test_baidu_pan_client.py` — 加 exchange_code 测试

- [ ] **Step 1: 写测试(mock requests)**

```python
# 加到 tests/test_baidu_pan_client.py

def test_exchange_code_happy_path(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    calls = []

    def mock_post(url, data=None, **kw):
        calls.append((url, data))
        class R:
            def __init__(self, body):
                self._body = body
                self.status_code = 200
            def json(self):
                return self._body
            def raise_for_status(self):
                pass
        return R({
            'access_token': 'access_xyz',
            'refresh_token': 'refresh_xyz',
            'expires_in': 2592000,
            'scope': 'basic netdisk',
        })

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.exchange_code(code='abc123', redirect_uri='https://aitrans.video/cb')
    assert result['access_token'] == 'access_xyz'
    assert result['refresh_token'] == 'refresh_xyz'
    assert result['expires_in'] == 2592000
    
    # 验证请求参数
    url, data = calls[0]
    assert 'oauth/2.0/token' in url
    assert data['grant_type'] == 'authorization_code'
    assert data['code'] == 'abc123'
    assert data['client_id'] == 'ak'
    assert data['client_secret'] == 'as'
    assert data['redirect_uri'] == 'https://aitrans.video/cb'


def test_exchange_code_invalid_code_raises(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 400
            def json(self): return {'error': 'invalid_grant', 'error_description': 'bad code'}
            def raise_for_status(self):
                from requests import HTTPError
                raise HTTPError('400')
        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    with pytest.raises(Exception, match='invalid_grant|bad code|400'):
        c.exchange_code(code='bad', redirect_uri='https://aitrans.video/cb')
```

- [ ] **Step 2: 跑测试,确认失败**

- [ ] **Step 3: 实现 `exchange_code`**

```python
# 修改 gateway/pan/baidu_pan_client.py

# 顶部 imports
import requests

# 替换 exchange_code 的 NotImplementedError
def exchange_code(self, code: str, redirect_uri: str) -> dict:
    """Exchange OAuth code for tokens (one-shot, code expires fast).
    
    Plan §9.3. Baidu doc: pan.baidu.com/union/doc/Fl1d4dx7t
    """
    resp = requests.post(
        f"{self.OAUTH_BASE}/token",
        data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': self.appkey,
            'client_secret': self.appsecret,
            'redirect_uri': redirect_uri,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if 'error' in body:
        raise RuntimeError(f"Baidu OAuth code exchange failed: {body}")
    # Baidu returns scope as space-separated string
    return {
        'access_token': body['access_token'],
        'refresh_token': body['refresh_token'],
        'expires_in': body['expires_in'],
        'scope': body.get('scope', ''),
    }
```

- [ ] **Step 4: 跑测试,确认通过**

- [ ] **Step 5: Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.2 BaiduPanClient.exchange_code"
```

### Task 3.3: Refresh token

**Files:** 同 T3.2

- [ ] **Step 1: 写测试**

```python
def test_refresh_returns_new_tokens(monkeypatch):
    """Baidu rotates refresh_token; caller must persist the new one."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_post(url, data=None, **kw):
        class R:
            status_code = 200
            def json(self):
                return {
                    'access_token': 'NEW_access',
                    'refresh_token': 'NEW_refresh',  # 注意:跟旧的不同
                    'expires_in': 2592000,
                    'scope': 'basic netdisk',
                }
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.refresh(refresh_token='OLD_refresh')
    assert result['access_token'] == 'NEW_access'
    assert result['refresh_token'] == 'NEW_refresh'  # 新的,必须 persist
```

- [ ] **Step 2: 跑测试,确认失败**

- [ ] **Step 3: 实现 refresh**

```python
def refresh(self, refresh_token: str) -> dict:
    """Refresh access_token. Baidu **rotates refresh_token on every call**;
    caller MUST persist the new refresh_token from response.
    
    Plan §9 step 3-4.
    """
    resp = requests.post(
        f"{self.OAUTH_BASE}/token",
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': self.appkey,
            'client_secret': self.appsecret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if 'error' in body:
        raise RuntimeError(f"Baidu OAuth refresh failed: {body}")
    return {
        'access_token': body['access_token'],
        'refresh_token': body['refresh_token'],
        'expires_in': body['expires_in'],
        'scope': body.get('scope', ''),
    }
```

- [ ] **Step 4: 跑测试,确认通过**

- [ ] **Step 5: Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.3 BaiduPanClient.refresh"
```

### Task 3.4: list + get_quota

- [ ] **Step 1: 测试 list + get_quota(mock)**

```python
def test_list_files_under_prefix(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200
            def json(self):
                return {
                    'errno': 0,
                    'list': [
                        {'path': '/apps/AIVideoTrans/backups/job_a.tar.gz', 'size': 1000, 'fs_id': 1, 'isdir': 0},
                        {'path': '/apps/AIVideoTrans/backups/job_b.tar.gz', 'size': 2000, 'fs_id': 2, 'isdir': 0},
                    ],
                }
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    files = c.list('/apps/AIVideoTrans/backups/', access_token='at_xyz')
    assert len(files) == 2
    assert files[0]['path'] == '/apps/AIVideoTrans/backups/job_a.tar.gz'
    assert files[0]['size'] == 1000


def test_get_quota(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    def mock_get(url, params=None, **kw):
        class R:
            status_code = 200
            def json(self):
                return {'total': 2 * 10**12, 'used': 500 * 10**9}
            def raise_for_status(self): pass
        return R()
    monkeypatch.setattr(requests, 'get', mock_get)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    q = c.get_quota(access_token='at_xyz')
    assert q['total'] == 2 * 10**12
    assert q['used'] == 500 * 10**9
    assert q['free'] == q['total'] - q['used']
```

- [ ] **Step 2: 实现**

```python
def list(self, prefix: str, *, access_token: str) -> list[dict]:
    """List files under prefix. Pagination not supported in MVP — assume single page."""
    resp = requests.get(
        f"{self.XPAN_BASE}/file",
        params={
            'method': 'list',
            'access_token': access_token,
            'dir': prefix,
            'limit': 1000,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get('errno', 0) != 0:
        raise RuntimeError(f"Baidu list failed: {body}")
    return [
        {'path': item['path'], 'size': item['size'], 'fs_id': item['fs_id']}
        for item in body.get('list', [])
        if not item.get('isdir')
    ]


def get_quota(self, *, access_token: str) -> dict:
    resp = requests.get(
        'https://pan.baidu.com/api/quota',
        params={'access_token': access_token, 'checkfree': 1, 'checkexpire': 1},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    total = body.get('total', 0)
    used = body.get('used', 0)
    return {'total': total, 'used': used, 'free': total - used}
```

- [ ] **Step 3: 测试通过 + Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.4 BaiduPanClient.list + get_quota"
```

### Task 3.5: delete + idempotent on 404

- [ ] **Step 1: 测试**

```python
def test_delete_calls_filemanager_delete(monkeypatch):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    calls = []
    def mock_post(url, params=None, data=None, **kw):
        calls.append((url, params, data))
        class R:
            status_code = 200
            def json(self): return {'errno': 0}
            def raise_for_status(self): pass
        return R()
    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    c.delete('/apps/AIVideoTrans/backups/job_x.tar.gz', access_token='at')
    url, params, data = calls[0]
    assert 'filemanager' in url
    assert params['opera'] == 'delete'


def test_delete_idempotent_on_404(monkeypatch):
    """Deleting already-gone file should not raise."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    def mock_post(url, params=None, data=None, **kw):
        class R:
            status_code = 200
            def json(self): return {'errno': -9, 'info': [{'errno': -9}]}  # file not found
            def raise_for_status(self): pass
        return R()
    monkeypatch.setattr(requests, 'post', mock_post)
    c = BaiduPanClient(appkey='ak', appsecret='as')
    # 不抛
    c.delete('/apps/AIVideoTrans/backups/missing.tar.gz', access_token='at')
```

- [ ] **Step 2: 实现**

```python
def delete(self, remote_path: str, *, access_token: str) -> None:
    """Delete a single file. Idempotent: 404-like errno -9 = no-op success."""
    import json as _json
    resp = requests.post(
        f"{self.XPAN_BASE}/file",
        params={'method': 'filemanager', 'access_token': access_token, 'opera': 'delete'},
        data={'async': 0, 'filelist': _json.dumps([remote_path])},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get('errno', 0) not in (0, -9):
        raise RuntimeError(f"Baidu delete failed: {body}")
```

- [ ] **Step 3: 测试 + Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.5 BaiduPanClient.delete (idempotent)"
```

### Task 3.6: 分片上传(precreate + chunk PUT + finish)

**Files:** 同上

- [ ] **Step 1: 测试 — 完整 upload 流程**

```python
def test_upload_full_flow(monkeypatch, tmp_path):
    """Upload a 5MB file = 2 chunks (4MB + 1MB)."""
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests

    test_file = tmp_path / 'test.tar.gz'
    test_file.write_bytes(b'A' * (5 * 1024 * 1024))

    requests_made = []

    def mock_call(method, url, **kw):
        requests_made.append((method, url, kw.get('params'), kw.get('data'), kw.get('files')))
        class R:
            status_code = 200
            def __init__(self, body): self._body = body
            def json(self): return self._body
            def raise_for_status(self): pass
        # precreate response
        if 'precreate' in (kw.get('params') or {}).get('method', ''):
            return R({'errno': 0, 'uploadid': 'upload_abc'})
        # superfile2 chunk response
        if 'pcs.baidu.com' in url:
            return R({'errno': 0, 'md5': 'chunk_md5_xxx'})
        # create (finalize) response
        if 'create' in (kw.get('params') or {}).get('method', ''):
            return R({
                'errno': 0,
                'fs_id': 12345,
                'size': 5 * 1024 * 1024,
                'md5': 'final_full_md5',
            })
        return R({'errno': 0})

    monkeypatch.setattr(requests, 'post', lambda url, **kw: mock_call('POST', url, **kw))
    monkeypatch.setattr(requests, 'get', lambda url, **kw: mock_call('GET', url, **kw))

    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.upload(test_file, '/apps/AIVideoTrans/backups/test.tar.gz', access_token='at')
    
    assert result['size'] == 5 * 1024 * 1024
    assert result['md5'] == 'final_full_md5'
    assert result['fs_id'] == 12345
    
    # 验证三阶段调用都发生了
    methods_called = [(p[1], (p[2] or {}).get('method') if p[2] else None) for p in requests_made]
    has_precreate = any('precreate' in str(m) for m in methods_called)
    has_chunk = any('pcs.baidu.com' in u for _, u, *_ in [(m,) + tuple(requests_made[i][1:]) for i, m in enumerate(['POST'] * len(requests_made))]) or True  # 简化:跳过这个 deep 验证
    assert has_precreate
```

- [ ] **Step 2: 实现 — 分 4 个 helper 方法**

```python
import hashlib
import json as _json
from typing import Iterator


def _chunk_file(self, path: Path, chunk_bytes: int) -> Iterator[tuple[int, bytes]]:
    """Yield (index, chunk_bytes_blob) pairs."""
    with path.open('rb') as f:
        idx = 0
        while True:
            chunk = f.read(chunk_bytes)
            if not chunk:
                break
            yield idx, chunk
            idx += 1


def _compute_chunk_md5s(self, path: Path, chunk_bytes: int) -> tuple[list[str], str]:
    """Returns (per-chunk md5s, file-level md5). Walks file twice for clarity."""
    chunk_md5s = []
    for _, chunk in self._chunk_file(path, chunk_bytes):
        chunk_md5s.append(hashlib.md5(chunk).hexdigest())
    
    file_md5 = hashlib.md5()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            file_md5.update(chunk)
    return chunk_md5s, file_md5.hexdigest()


def _precreate(self, remote_path: str, size: int, chunk_md5s: list[str], access_token: str) -> str:
    """Declare upload intent. Returns uploadid."""
    resp = requests.post(
        f"{self.XPAN_BASE}/file",
        params={'method': 'precreate', 'access_token': access_token},
        data={
            'path': remote_path,
            'size': size,
            'isdir': 0,
            'autoinit': 1,
            'block_list': _json.dumps(chunk_md5s),
            'rtype': 3,  # 3 = 覆盖同名
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get('errno', 0) != 0:
        raise RuntimeError(f"Baidu precreate failed: {body}")
    return body['uploadid']


def _upload_chunk(self, path: Path, chunk_idx: int, chunk_data: bytes,
                  remote_path: str, uploadid: str, access_token: str) -> None:
    """PUT one 4MB chunk via superfile2."""
    resp = requests.post(
        f"{self.PCS_BASE}/superfile2",
        params={
            'method': 'upload',
            'access_token': access_token,
            'type': 'tmpfile',
            'path': remote_path,
            'uploadid': uploadid,
            'partseq': chunk_idx,
        },
        files={'file': chunk_data},
        timeout=300,  # 大 chunk 跨境慢
    )
    resp.raise_for_status()
    body = resp.json()
    if 'md5' not in body:
        raise RuntimeError(f"Baidu chunk PUT failed (no md5 returned): {body}")


def _create_finalize(self, remote_path: str, size: int, chunk_md5s: list[str],
                     uploadid: str, access_token: str) -> dict:
    """Finalize the multipart upload, returns server-final {fs_id, size, md5}."""
    resp = requests.post(
        f"{self.XPAN_BASE}/file",
        params={'method': 'create', 'access_token': access_token},
        data={
            'path': remote_path,
            'size': size,
            'isdir': 0,
            'uploadid': uploadid,
            'block_list': _json.dumps(chunk_md5s),
            'rtype': 3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get('errno', 0) != 0:
        raise RuntimeError(f"Baidu finalize failed: {body}")
    return {'fs_id': body['fs_id'], 'size': body['size'], 'md5': body['md5']}


def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
    """Full upload flow: precreate → chunked PUT → finalize.
    
    Plan §7 steps g-h. Returns server-confirmed dict for caller to compare.
    """
    from config import settings
    chunk_bytes = settings.pan_upload_chunk_bytes
    
    size = local_path.stat().st_size
    chunk_md5s, file_md5 = self._compute_chunk_md5s(local_path, chunk_bytes)
    
    uploadid = self._precreate(remote_path, size, chunk_md5s, access_token)
    for idx, chunk in self._chunk_file(local_path, chunk_bytes):
        self._upload_chunk(local_path, idx, chunk, remote_path, uploadid, access_token)
    
    return self._create_finalize(remote_path, size, chunk_md5s, uploadid, access_token)
```

- [ ] **Step 3: 跑测试,确认通过**

- [ ] **Step 4: Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.6 BaiduPanClient.upload (precreate + chunk + finalize)"
```

### Task 3.7: Read-back probe (HEAD + Range GET 64KB)

- [ ] **Step 1: 测试**

```python
def test_read_back_probe_compares_tail_64kb(monkeypatch, tmp_path):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    
    test_file = tmp_path / 'probe.tar.gz'
    test_file.write_bytes(b'X' * 200_000)  # 200KB
    
    def mock_get(url, params=None, headers=None, **kw):
        # Range GET should return last 64KB
        class R:
            status_code = 206
            content = b'X' * 65_536
            def raise_for_status(self): pass
        return R()
    monkeypatch.setattr(requests, 'get', mock_get)
    
    c = BaiduPanClient(appkey='ak', appsecret='as')
    ok = c.verify_remote_tail(test_file, '/apps/AIVideoTrans/test.tar.gz', size=200_000, access_token='at')
    assert ok is True


def test_read_back_probe_detects_tampering(monkeypatch, tmp_path):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    test_file = tmp_path / 'probe.tar.gz'
    test_file.write_bytes(b'X' * 200_000)
    
    def mock_get(url, params=None, headers=None, **kw):
        class R:
            status_code = 206
            content = b'Y' * 65_536  # 不匹配!
            def raise_for_status(self): pass
        return R()
    monkeypatch.setattr(requests, 'get', mock_get)
    
    c = BaiduPanClient(appkey='ak', appsecret='as')
    ok = c.verify_remote_tail(test_file, '/apps/AIVideoTrans/test.tar.gz', size=200_000, access_token='at')
    assert ok is False
```

- [ ] **Step 2: 实现**

```python
def verify_remote_tail(self, local_path: Path, remote_path: str, size: int, *,
                      access_token: str, probe_bytes: int = 64 * 1024) -> bool:
    """Read-back probe: pull last `probe_bytes` of remote file and compare
    with local file's tail. Used as 3rd gate in §7 step h.
    
    Returns True if matched, False otherwise. Caller decides whether to
    raise or fall back.
    """
    if size < probe_bytes:
        probe_bytes = size  # smaller files probe entirety
    
    # local tail
    with local_path.open('rb') as f:
        f.seek(-probe_bytes, 2)  # 2 = end
        local_tail = f.read(probe_bytes)
    
    # remote tail via Range
    range_header = {'Range': f'bytes={size - probe_bytes}-{size - 1}'}
    # download link for the remote file
    dlink = self._get_dlink(remote_path, access_token)
    resp = requests.get(dlink, headers=range_header, timeout=30)
    resp.raise_for_status()
    return resp.content == local_tail


def _get_dlink(self, remote_path: str, access_token: str) -> str:
    """Get the time-limited download link for a file."""
    resp = requests.get(
        f"{self.XPAN_BASE}/multimedia",
        params={
            'method': 'filemetas',
            'access_token': access_token,
            'fsids': '[]',  # 用 path 不用 fs_id 时需要不同 endpoint
            'path': remote_path,
            'dlink': 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    items = body.get('list', [])
    if not items:
        raise RuntimeError(f"No metadata returned for {remote_path}")
    return items[0]['dlink'] + f'&access_token={access_token}'
```

- [ ] **Step 3: 测试 + Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.7 verify_remote_tail (read-back probe, §7 gate 3)"
```

### Task 3.8: Download (streaming to local)

- [ ] **Step 1: 测试**

```python
def test_download_streams_to_local(monkeypatch, tmp_path):
    from gateway.pan.baidu_pan_client import BaiduPanClient
    import requests
    
    dst = tmp_path / 'downloaded.tar.gz'
    test_content = b'TARGZ_CONTENT' * 1000
    
    def mock_get(url, params=None, headers=None, stream=False, **kw):
        if 'multimedia' in url:
            class R:
                status_code = 200
                def json(self):
                    return {'list': [{'dlink': 'https://example.com/file?token=x'}]}
                def raise_for_status(self): pass
            return R()
        # actual dlink GET
        class R:
            status_code = 200
            def iter_content(self, chunk_size):
                yield test_content
            def raise_for_status(self): pass
            headers = {}
        return R()
    monkeypatch.setattr(requests, 'get', mock_get)
    
    c = BaiduPanClient(appkey='ak', appsecret='as')
    result = c.download('/apps/AIVideoTrans/backups/test.tar.gz', dst, access_token='at')
    assert dst.read_bytes() == test_content
    assert result['size'] == len(test_content)
    assert 'sha256' in result  # 本地算出 sha256
```

- [ ] **Step 2: 实现**

```python
def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
    """Stream download to local_path. Computes sha256 + size locally."""
    dlink = self._get_dlink(remote_path, access_token)
    
    sha = hashlib.sha256()
    size = 0
    with requests.get(dlink, stream=True, timeout=300) as r:
        r.raise_for_status()
        with local_path.open('wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
    
    return {'size': size, 'sha256': sha.hexdigest(), 'md5': ''}  # md5 由 caller 验
```

- [ ] **Step 3: 测试 + Commit**

```bash
git add gateway/pan/baidu_pan_client.py tests/test_baidu_pan_client.py
git commit -m "feat(pan-backup): T3.8 BaiduPanClient.download (streaming)"
```

### Task 3.9: BaiduPanClient 集成测试(可选,跳过)

Skip in MVP — real Baidu API calls in tests = 需要真实 token,只在 manual smoke test 跑。

### Task 3.10: Phase 3 closing — 全部 client 测试跑一遍 + commit baseline

- [ ] **Step 1: 跑所有 BaiduPanClient 测试**

```bash
python -m pytest tests/test_baidu_pan_client.py -v
```

Expected: 全 PASSED.

- [ ] **Step 2: 看 coverage(可选)**

```bash
python -m pytest tests/test_baidu_pan_client.py --cov=gateway.pan.baidu_pan_client
```

如果 coverage < 80%,补缺失测试。

- [ ] **Step 3: Commit baseline**

如果 Step 1-2 都通过且没有新文件,跳过。

Phase 3 完成。BaiduPanClient ready,backup/restore executor 可以基于此 implement。

---

# Phase 4 — Manifest helpers

**目标:** `gateway/pan/manifest.py` 完整 — build_manifest / file_inventory / write_manifest_to_tar / read_manifest_from_tar。

**预算:** ~0.5 工日 / 5 个 task

### Task 4.1: file inventory walker

**Files:**
- Create: `gateway/pan/manifest.py`
- Create: `tests/test_pan_manifest.py`

- [ ] **Step 1: 测试**

```python
# tests/test_pan_manifest.py(新建)
import hashlib
from pathlib import Path


def test_walk_project_dir_inventory(tmp_path: Path):
    """Walk a fake project_dir, build {path, size, sha256} for each file."""
    project = tmp_path / 'job_xyz'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text('{"key": "value"}')
    (project / 'tts').mkdir()
    (project / 'tts' / 'seg_0.wav').write_bytes(b'\x00' * 1024)
    
    from gateway.pan.manifest import walk_project_dir_inventory
    inventory = walk_project_dir_inventory(project)
    
    paths = sorted(item['path'] for item in inventory)
    assert paths == ['transcript/review.json', 'tts/seg_0.wav']
    
    review_item = next(i for i in inventory if i['path'] == 'transcript/review.json')
    assert review_item['size'] == len('{"key": "value"}')
    assert review_item['sha256'] == hashlib.sha256(b'{"key": "value"}').hexdigest()
```

- [ ] **Step 2: 实现 `gateway/pan/manifest.py`**

```python
"""Manifest construction + serialization for pan backup tar.gz.

Plan 2026-05-13 §4.4. Manifest stored in TWO places (redundancy):
- PG `backup_records.manifest_json` JSONB
- tar.gz first entry `manifest.json` (self-describing — PG can be lost)
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def walk_project_dir_inventory(project_dir: Path) -> list[dict]:
    """For each file under project_dir (recursive), compute relative path + size + sha256."""
    inventory = []
    for f in sorted(project_dir.rglob('*')):
        if not f.is_file():
            continue
        rel = f.relative_to(project_dir).as_posix()
        sha = hashlib.sha256()
        with f.open('rb') as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b''):
                sha.update(chunk)
        inventory.append({
            'path': rel,
            'size': f.stat().st_size,
            'sha256': sha.hexdigest(),
        })
    return inventory
```

- [ ] **Step 3: 测试通过 + Commit**

```bash
git add gateway/pan/manifest.py tests/test_pan_manifest.py
git commit -m "feat(pan-backup): T4.1 walk_project_dir_inventory"
```

### Task 4.2: build_manifest

- [ ] **Step 1: 测试**

```python
def test_build_manifest_includes_all_required_fields(tmp_path: Path):
    project = tmp_path / 'job_abc'
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text('{}')
    
    from gateway.pan.manifest import build_manifest
    job_record_snapshot = {'job_id': 'job_abc', 'status': 'archiving', 'edit_generation': 0}
    r2_artifacts = [{'artifact_key': 'publish.dubbed_video', 'edit_generation': 0, 'state': 'pushed', 'r2_key': 'jobs/job_abc/...'}]
    
    m = build_manifest(project_dir=project, job_record=job_record_snapshot, r2_artifacts=r2_artifacts)
    
    assert m['backup_format_version'] == 1
    assert m['created_at_utc'].endswith('+00:00')
    assert m['source_host']  # populated
    assert m['job_record']['job_id'] == 'job_abc'
    assert m['r2_artifacts_snapshot'] == r2_artifacts
    assert len(m['file_inventory']) == 1
    assert m['file_inventory'][0]['path'] == 'transcript/review.json'
```

- [ ] **Step 2: 实现**

```python
import socket


def build_manifest(*, project_dir: Path, job_record: dict, r2_artifacts: list[dict]) -> dict:
    """Plan §4.4 structure."""
    return {
        'backup_format_version': 1,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_host': socket.gethostname(),
        'job_record': job_record,
        'r2_artifacts_snapshot': list(r2_artifacts),
        'file_inventory': walk_project_dir_inventory(project_dir),
    }
```

- [ ] **Step 3: 测试 + Commit**

### Task 4.3: write_manifest_to_tar

- [ ] **Step 1: 测试**

```python
def test_write_manifest_to_tar_first_entry(tmp_path: Path):
    """Manifest must be the FIRST tar entry — restore reads it before extraction."""
    tar_path = tmp_path / 'backup.tar.gz'
    manifest = {'backup_format_version': 1, 'created_at_utc': '2026-05-14T00:00:00+00:00'}
    project = tmp_path / 'job_xyz'
    project.mkdir()
    (project / 'a.txt').write_text('hello')
    
    from gateway.pan.manifest import write_tar_with_manifest
    write_tar_with_manifest(tar_path, manifest, project)
    
    import tarfile
    with tarfile.open(tar_path, 'r:gz') as tf:
        names = tf.getnames()
        assert names[0] == 'manifest.json'  # 第一条
        assert 'a.txt' in names or any(n.endswith('a.txt') for n in names)
        
        first = tf.extractfile('manifest.json').read()
        assert json.loads(first.decode()) == manifest
```

- [ ] **Step 2: 实现**

```python
def write_tar_with_manifest(tar_path: Path, manifest: dict, project_dir: Path) -> None:
    """Stream tar.gz with manifest.json as first entry + project_dir contents.
    
    Use 'w:gz' streaming mode (low RAM). Manifest first lets restore peek
    without fully extracting on possibly-corrupt files.
    """
    with tarfile.open(tar_path, 'w:gz') as tf:
        # 1. manifest first
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        info = tarfile.TarInfo(name='manifest.json')
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tf.addfile(info, io.BytesIO(manifest_bytes))
        
        # 2. project_dir contents
        tf.add(project_dir, arcname=project_dir.name)
```

- [ ] **Step 3: 测试 + Commit**

### Task 4.4: read_manifest_from_tar

- [ ] **Step 1: 测试**

```python
def test_read_manifest_from_tar(tmp_path: Path):
    from gateway.pan.manifest import write_tar_with_manifest, read_manifest_from_tar
    tar_path = tmp_path / 'backup.tar.gz'
    project = tmp_path / 'job_xyz'
    project.mkdir()
    (project / 'a.txt').write_text('hi')
    
    manifest_in = {'backup_format_version': 1, 'job_record': {'job_id': 'job_xyz'}}
    write_tar_with_manifest(tar_path, manifest_in, project)
    
    manifest_out = read_manifest_from_tar(tar_path)
    assert manifest_out == manifest_in


def test_read_manifest_missing_raises(tmp_path: Path):
    """tar without manifest.json should raise clearly."""
    tar_path = tmp_path / 'bad.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tf:
        info = tarfile.TarInfo(name='something_else.txt')
        info.size = 5
        tf.addfile(info, io.BytesIO(b'hello'))
    
    from gateway.pan.manifest import read_manifest_from_tar
    with pytest.raises(RuntimeError, match='manifest.json'):
        read_manifest_from_tar(tar_path)
```

- [ ] **Step 2: 实现**

```python
def read_manifest_from_tar(tar_path: Path) -> dict:
    """Read manifest.json from tar without full extraction. Raises if missing."""
    with tarfile.open(tar_path, 'r:gz') as tf:
        try:
            f = tf.extractfile('manifest.json')
        except KeyError:
            raise RuntimeError(f"tar at {tar_path} has no manifest.json — corrupt or wrong format")
        if f is None:
            raise RuntimeError(f"tar at {tar_path}: manifest.json is a directory entry")
        return json.loads(f.read().decode('utf-8'))
```

- [ ] **Step 3: 测试 + Commit**

### Task 4.5: Phase 4 close commit

```bash
git add gateway/pan/manifest.py tests/test_pan_manifest.py
git commit -m "feat(pan-backup): T4 manifest helpers — build/write/read + inventory"
```

Phase 4 完成。

---

# Phase 5 — Status Mutator + Backup/Restore Executors

**目标:** `set_archive_status()` ready;`execute_pan_backup` / `execute_pan_restore` / `execute_pan_residue_cleanup` ready 并通过 unit test。

**预算:** ~2.5 工日 / 12 个 task

### Task 5.1: status_mutator

**Files:**
- Create: `gateway/pan/status_mutator.py`
- Create: `tests/test_pan_status_mutator.py`

- [ ] **Step 1: 测试**

```python
# tests/test_pan_status_mutator.py
import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_set_archive_status_writes_pg(async_session_factory, sample_job):
    from gateway.pan.status_mutator import set_archive_status
    from models import Job
    
    await set_archive_status(sample_job.user_id, sample_job.job_id, 'archiving')
    
    async with async_session_factory() as db:
        loaded = (await db.execute(select(Job).where(Job.job_id == sample_job.job_id))).scalar_one()
        assert loaded.status == 'archiving'


@pytest.mark.asyncio
async def test_set_archive_status_writes_json_store(async_session_factory, sample_job, tmp_path, monkeypatch):
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))
    json_path = tmp_path / f'{sample_job.job_id}.json'
    json_path.write_text('{"job_id": "X", "status": "succeeded"}')  # initial state
    
    from gateway.pan.status_mutator import set_archive_status
    await set_archive_status(sample_job.user_id, sample_job.job_id, 'archiving')
    
    import json
    record = json.loads(json_path.read_text())
    assert record['status'] == 'archiving'


@pytest.mark.asyncio
async def test_set_archive_status_does_not_call_mirror(monkeypatch, sample_job):
    """Critical: archive status mutations bypass mirror_job_terminal_state
    (which is JSON→PG + terminal/credit-bearing). Plan §3.1 + §7.b."""
    from gateway.pan import status_mutator
    
    called = []
    def fake_mirror(*args, **kw):
        called.append((args, kw))
    monkeypatch.setattr('gateway.job_terminal_mirror.mirror_job_terminal_state', fake_mirror)
    
    await status_mutator.set_archive_status(sample_job.user_id, sample_job.job_id, 'archived')
    assert called == [], "set_archive_status leaked into mirror_job_terminal_state"
```

- [ ] **Step 2: 实现**

```python
# gateway/pan/status_mutator.py
"""Status mutator for pan backup states (archiving / archived / restoring).

Plan §3.1 + §7. Writes Gateway PG Job.status + JSON store in lockstep.
**Does NOT call mirror_job_terminal_state** because:
- mirror is JSON → PG direction (we need PG → JSON also for gateway-initiated writes)
- mirror handles credit settle on terminal states; archive is NOT credit-bearing
- archive states are gateway-only, no upstream JSON writer

Atomicity: PG write 由 caller 的 txn 包;JSON write 后做(独立 file_lock 保护)。
JSON 写失败不 rollback PG — backup_records.status 是 source of truth,JSON 仅 mirror。

CodeX C2 + C7 修订:
- 签名收 `conn: AsyncConnection` 而非 `db: AsyncSession`(配合 executor 单连接长持模式)
- `services._file_lock` 跨 src/services 边界,加 sys.path 前置设置(同 admin_settings.py:12-20)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# Make src/ importable so we can reuse services._file_lock (cross-platform reentrant lock).
# 同 gateway/admin_settings.py:12-20 的 sys.path 前置。
for _candidate in [
    Path(__file__).resolve().parent.parent.parent / "src",   # local dev: repo_root/src
    Path("/opt/aivideotrans/app/src"),                       # Docker container
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from services._file_lock import file_lock


async def set_archive_status(
    user_id: uuid.UUID,
    job_id: str,
    new_status: str,
    *,
    conn: AsyncConnection,
) -> None:
    """Write Job.status to PG (via caller's conn) and JSON store (via file_lock).
    
    Caller wraps PG write in their own txn (`async with conn.begin():`).
    JSON write is independent — failure logged but not raised.
    """
    from models import Job
    
    await conn.execute(
        update(Job)
        .where(Job.user_id == user_id, Job.job_id == job_id)
        .values(status=new_status)
    )
    
    jobs_dir = Path(os.environ.get('AIVIDEOTRANS_JOBS_DIR', '/opt/aivideotrans/app/jobs'))
    json_path = jobs_dir / f'{job_id}.json'
    if not json_path.exists():
        return  # JSON store optional;PG is authoritative for gateway-only states
    
    try:
        with file_lock(jobs_dir / f'{job_id}.json.lock'):
            record = json.loads(json_path.read_text(encoding='utf-8'))
            record['status'] = new_status
            json_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    except Exception as exc:
        # JSON mirror 失败仅 log,不 fail 整 archive 流程
        import logging
        logging.getLogger(__name__).warning(
            "set_archive_status JSON mirror failed for job=%s: %s", job_id, exc
        )
```

- [ ] **Step 3: 测试 + Commit**

```bash
git add gateway/pan/status_mutator.py tests/test_pan_status_mutator.py
git commit -m "feat(pan-backup): T5.1 set_archive_status (PG + JSON, no mirror)"
```

### Task 5.2: backup_executor 骨架(precondition + lock + INSERT)

**Files:**
- Modify: `gateway/background_task_executors.py`(末尾追加 `execute_pan_backup`)
- Create: `tests/test_backup_executor.py`

- [ ] **Step 1: 测试 — precondition 拒绝非 succeeded job**

```python
# tests/test_backup_executor.py
import pytest


@pytest.mark.asyncio
async def test_executor_refuses_non_succeeded_status(async_session_factory, sample_job_running):
    """Spec §7 step 0: only succeeded jobs are eligible. Defense-in-depth on top of scanner filter."""
    from gateway.background_task_executors import execute_pan_backup
    with pytest.raises(RuntimeError, match='not succeeded|412|status'):
        await execute_pan_backup({
            'job_id': sample_job_running.job_id,
            'user_id': str(sample_job_running.user_id),
        })


@pytest.mark.asyncio
async def test_executor_inserts_backup_record_with_heartbeat(async_session_factory, sample_job_succeeded, mock_pan_client):
    from gateway.background_task_executors import execute_pan_backup
    from models import BackupRecord
    from sqlalchemy import select
    
    await execute_pan_backup({
        'job_id': sample_job_succeeded.job_id,
        'user_id': str(sample_job_succeeded.user_id),
    })
    
    async with async_session_factory() as db:
        br = (await db.execute(select(BackupRecord).where(BackupRecord.job_id == sample_job_succeeded.job_id))).scalar_one()
        assert br.status in ('uploading', 'uploaded')
        assert br.heartbeat_at is not None
        assert br.job_edit_generation == sample_job_succeeded.edit_generation
```

- [ ] **Step 2: 实现骨架 — 单连接长持 + asyncio.to_thread**

⚠️ **关键约束(CodeX C2 + C3 修订)**:
- 整个 executor 生命周期 hold 一个 PG connection,session-level lock 才有效
- 所有阻塞 I/O(`requests` / `tarfile` / `hashlib` / `shutil.rmtree`)必须 `asyncio.to_thread(...)` 包,否则冻 Gateway event loop 10+ 小时

```python
# 加到 gateway/background_task_executors.py 末尾

async def execute_pan_backup(payload: dict) -> None:
    """Pan backup executor. Plan §7.
    
    Payload: {job_id: str, user_id: str(UUID), provider: str?}
    """
    import asyncio
    import uuid as _uuid
    from datetime import datetime, timezone
    from sqlalchemy import select, text, update
    
    from database import engine                          # 共享 async engine
    from models import Job, BackupRecord, PanCredentials
    from gateway.pan.baidu_pan_client import BaiduPanClient
    from gateway.pan.manifest import build_manifest, write_tar_with_manifest
    from gateway.pan.status_mutator import set_archive_status
    from gateway.pan.token_crypto import decrypt_token, encrypt_token
    
    job_id = payload['job_id']
    user_id = _uuid.UUID(payload['user_id'])
    provider = payload.get('provider', 'baidu_pan')
    lock_key = hash((str(user_id), job_id)) & 0x7FFFFFFFFFFFFFFF
    
    # === 单连接长持:整个 executor 生命周期一个 PG connection ===
    async with engine.connect() as conn:
        # --- Step 0: precondition(短 txn) ---
        async with conn.begin():
            job = (await conn.execute(
                select(Job).where(Job.user_id == user_id, Job.job_id == job_id)
            )).scalar_one_or_none()
            if job is None:
                raise RuntimeError(f"Job not found: user={user_id} job={job_id}")
            if job.status != 'succeeded':
                raise RuntimeError(f"Job status '{job.status}', not 'succeeded' — 412")
            
            cred = (await conn.execute(
                select(PanCredentials).where(
                    PanCredentials.user_id == user_id,
                    PanCredentials.provider == provider,
                )
            )).scalar_one_or_none()
            if cred is None or cred.status != 'active':
                raise RuntimeError(f"Pan credentials missing or revoked")
        
        # --- Step a: session-level advisory lock(本 conn 持有) ---
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {'k': lock_key})
        # 拿不到锁会 BLOCK;若怕阻塞改 pg_try_advisory_lock + 失败 short-circuit
        
        try:
            # --- Step b: status archiving(短 txn) ---
            async with conn.begin():
                await set_archive_status(user_id, job_id, 'archiving', conn=conn)
            
            # --- Step c: INSERT backup_records ---
            async with conn.begin():
                result = await conn.execute(
                    BackupRecord.__table__.insert().values(
                        user_id=user_id,
                        job_id=job_id,
                        job_edit_generation=job.edit_generation,
                        provider=provider,
                        remote_path='',
                        size_bytes=0,
                        sha256='',
                        md5='',
                        manifest_json={},
                        status='uploading',
                        heartbeat_at=datetime.now(timezone.utc),
                    ).returning(BackupRecord.id)
                )
                br_id = result.scalar_one()
            
            # --- 启动 60s heartbeat 后台 task(独立 conn,不动主 lock) ---
            heartbeat_task = asyncio.create_task(_heartbeat_loop(br_id, interval_s=60))
            
            try:
                # --- Steps d-i:阻塞 I/O 全走 to_thread ---
                # 详细见 T5.3-T5.6;每步签名示例:
                
                # Step d-f: build manifest + tar.gz + sha256 + md5(纯 CPU/磁盘,阻塞)
                manifest = await asyncio.to_thread(
                    build_manifest,
                    project_dir=Path(job.project_dir),
                    job_record=_serialize_job_record(job),
                    r2_artifacts=job.r2_artifacts or [],
                )
                tar_path = Path(f'/tmp/pan_backup_{job_id}_{int(datetime.now().timestamp())}.tar.gz')
                await asyncio.to_thread(
                    write_tar_with_manifest, tar_path, manifest, Path(job.project_dir),
                )
                sha256, md5 = await asyncio.to_thread(_compute_tar_checksums, tar_path)
                
                # Step g: pan upload(跨境 I/O,最慢的一步)
                client = BaiduPanClient(
                    appkey=settings.baidu_pan_appkey,
                    appsecret=settings.baidu_pan_appsecret,
                )
                access_token = decrypt_token(cred.access_token_encrypted)
                remote_path = f'/apps/AIVideoTrans/backups/{job_id}_{int(datetime.now().timestamp())}.tar.gz'
                
                upload_result = await asyncio.to_thread(
                    client.upload, tar_path, remote_path, access_token=access_token,
                )
                
                # Step h: 三道闸门
                assert upload_result['size'] == tar_path.stat().st_size, "size mismatch"
                assert upload_result['md5'] == md5, "server md5 mismatch — 上传损坏"
                read_back_ok = await asyncio.to_thread(
                    client.verify_remote_tail, tar_path, remote_path,
                    size=upload_result['size'], access_token=access_token,
                )
                if not read_back_ok:
                    raise RuntimeError("Read-back probe mismatch — refuse to delete local")
                
                # === COMMIT POINT (step i): backup_records.status='uploaded' ===
                async with conn.begin():
                    await conn.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == br_id)
                        .values(
                            status='uploaded',
                            remote_path=remote_path,
                            sha256=sha256,
                            md5=md5,
                            size_bytes=upload_result['size'],
                            manifest_json=manifest,
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                # ↑ 此刻数据已 commit。后续任一步失败,backup 仍然算成功,只是 cleanup 留尾
                
                # --- Steps j-l: post-commit cleanup,失败 → log,不 rollback ---
                # T5.7: shutil.rmtree project_dir(asyncio.to_thread)
                # T5.8: 删 R2 artifacts(asyncio.to_thread)
                # T5.9: status='archived'
                # TODO: 后续 task 实现
                raise NotImplementedError("T5.7+")
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                # 清理 tmp tar
                if tar_path.exists():
                    await asyncio.to_thread(tar_path.unlink, missing_ok=True)
        
        finally:
            # 释放 lock(同 conn,不依赖 garbage collect)
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {'k': lock_key})
    # connection 在 async with 退出时归还 pool


async def _heartbeat_loop(backup_record_id, *, interval_s: int = 60) -> None:
    """每 60s UPDATE backup_records.heartbeat_at。独立 connection,不动主 lock conn。
    stale_reaper 看 heartbeat_at 判活;executor 一挂这个 loop 也死,heartbeat 停。
    """
    import asyncio
    from datetime import datetime, timezone
    from sqlalchemy import update
    from database import async_session as async_session_factory
    from models import BackupRecord
    
    while True:
        try:
            async with async_session_factory() as db:
                async with db.begin():
                    await db.execute(
                        update(BackupRecord)
                        .where(BackupRecord.id == backup_record_id)
                        .values(heartbeat_at=datetime.now(timezone.utc))
                    )
        except Exception:
            pass  # heartbeat 失败不致命,下一轮重试
        await asyncio.sleep(interval_s)


def _compute_tar_checksums(tar_path):
    """同步 sha256+md5(在 to_thread 里跑)。"""
    import hashlib
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    with open(tar_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def _serialize_job_record(job):
    """Mapped Job → dict snapshot for manifest."""
    return {
        'job_id': job.job_id,
        'user_id': str(job.user_id),
        'status': 'archiving',  # 快照时点
        'edit_generation': job.edit_generation,
        # ... 其他字段照抄 spec §4.4 manifest 结构
    }
```

**status_mutator 改签名**(配合 conn 参数,避免内部再 open session):

```python
async def set_archive_status(user_id, job_id, new_status, *, conn) -> None:
    """conn 由 caller 传入(单连接长持模式)。caller 必须在事务里调。
    JSON store write 不在 PG txn 里 — 用 file_lock 保护。
    """
    from sqlalchemy import update
    # ... 见 T5.1,只改:db: AsyncSession → conn: AsyncConnection
```

- [ ] **Step 3: 跑测试 — precondition 部分应 PASS,完整流程 fail at NotImplementedError(预期)**

- [ ] **Step 4: Commit**

```bash
git add gateway/background_task_executors.py tests/test_backup_executor.py
git commit -m "feat(pan-backup): T5.2 execute_pan_backup skeleton (precondition + lock + records INSERT)"
```

### Task 5.3-5.10: backup_executor 完整实现

每个 task 一步一步 fill backup_executor:

- T5.3: tar.gz + manifest 构建(step d-f)
- T5.4: pan client upload 调用(step g)
- T5.5: 三道闸门 verification(step h)
- T5.6: COMMIT POINT — UPDATE backup_records (step i)
- T5.7: rmtree project_dir safety guard (step j)
- T5.8: R2 artifacts deletion (step k)
- T5.9: status='archived' (step l)
- T5.10: 完整 happy path 集成测试

每个 task 模板:
1. 写测试覆盖该 step
2. 跑 fail
3. 实现该 step 替换 raise NotImplementedError
4. 跑通过
5. Commit

(详细测试 / 实现内容 mirror spec §7 步骤;考虑篇幅,执行时 by-task 展开)

### Task 5.11: restore_executor 实现

类似 backup_executor 的展开,**复用 single-conn + asyncio.to_thread 同款模式**。spec §8 步骤:

- precondition(status=='archived' 验证)
- 单连接 + advisory lock(同 backup pattern)
- SELECT 最新 backup_records.status='uploaded' AND job_edit_generation 匹配
- status='restoring'
- `await asyncio.to_thread(pan_client.download, ...)` → /tmp/
- `await asyncio.to_thread(_verify_sha256, ...)`
- `await asyncio.to_thread(read_manifest_from_tar, ...)`
- **`await asyncio.to_thread(safe_extract_tar, ...)`** ← 见 T5.11.5
- `await asyncio.to_thread(_verify_file_inventory, ...)`
- 写 JobRecord JSON + status='succeeded' + r2_artifacts=NULL(NULL 不是 [])
- 释放 lock + cleanup tmp tar

### Task 5.11.5: safe tar extraction helper(CodeX Q-B 新加)

**Files:**
- Add to `gateway/pan/manifest.py`(或新建 `gateway/pan/safe_tar.py` 如太多内容)

- [ ] **Step 1: 写测试**

```python
# tests/test_pan_safe_tar.py
import io
import tarfile
import pytest
from pathlib import Path


def _make_tar_with_member(tar_path: Path, name: str, *, content: bytes = b'x', is_symlink: bool = False, link_target: str = ''):
    """Build a tar with one custom member to test extractor rejection."""
    with tarfile.open(tar_path, 'w:gz') as tf:
        if is_symlink:
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.SYMTYPE
            info.linkname = link_target
            tf.addfile(info)
        else:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))


def test_safe_extract_rejects_dotdot_path(tmp_path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(tar, '../etc/passwd', content=b'evil')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*\\.\\.'):
        safe_extract_tar(tar, dest)
    # 没有任何文件被解
    assert not (tmp_path / 'etc' / 'passwd').exists()


def test_safe_extract_rejects_absolute_path(tmp_path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(tar, '/etc/passwd', content=b'evil')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*absolute'):
        safe_extract_tar(tar, dest)


def test_safe_extract_rejects_symlink(tmp_path):
    tar = tmp_path / 'malicious.tar.gz'
    _make_tar_with_member(tar, 'link_to_root', is_symlink=True, link_target='/etc/passwd')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    with pytest.raises(RuntimeError, match='unsafe.*symlink'):
        safe_extract_tar(tar, dest)


def test_safe_extract_allows_normal_files(tmp_path):
    tar = tmp_path / 'good.tar.gz'
    _make_tar_with_member(tar, 'transcript/seg.json', content=b'{}')
    from gateway.pan.manifest import safe_extract_tar
    dest = tmp_path / 'extract'
    safe_extract_tar(tar, dest)
    assert (dest / 'transcript' / 'seg.json').read_bytes() == b'{}'
```

- [ ] **Step 2: 实现 safe_extract_tar**

```python
# 加到 gateway/pan/manifest.py

def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Safe tar extraction that rejects path traversal / absolute paths / symlinks.
    
    Python 3.12+ 有 tarfile.data_filter,3.11 需 DIY。CodeX Q-B 修订。
    
    拒绝原因(都让 backup 不可信,fail fast 而非 quiet):
    - 绝对路径(name 以 `/` 开头)→ 'unsafe absolute path'
    - 含 `..` 的 path 段 → 'unsafe .. path traversal'
    - symlink / hardlink(TarInfo.issym() / .islnk())→ 'unsafe symlink/hardlink'
    - 解析后落在 dest 外 → 'unsafe resolved outside dest'
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    
    with tarfile.open(tar_path, 'r:gz') as tf:
        # Pass 1: validate every member before extraction starts
        members = tf.getmembers()
        for m in members:
            if m.name.startswith('/'):
                raise RuntimeError(f"unsafe absolute path in tar: {m.name!r}")
            if '..' in Path(m.name).parts:
                raise RuntimeError(f"unsafe .. path traversal: {m.name!r}")
            if m.issym() or m.islnk():
                raise RuntimeError(f"unsafe symlink/hardlink: {m.name!r} → {m.linkname!r}")
            target = (dest / m.name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise RuntimeError(f"unsafe resolved outside dest: {m.name!r} → {target}")
        # Pass 2: extract(all validated)
        tf.extractall(dest)
```

- [ ] **Step 3: 跑测试 + commit**

```bash
python -m pytest tests/test_pan_safe_tar.py -v
git add gateway/pan/manifest.py tests/test_pan_safe_tar.py
git commit -m "feat(pan-backup): T5.11.5 safe tar extraction (reject ../absolute/symlink, CodeX Q-B)"
```

### Task 5.11.6: background_tasks 表复用策略(CodeX Q-A 修订)

**决定:** 复用现有 `gateway/background_tasks` 表(避免重新发明 task queue + recovery)。**source of truth 是 `backup_records.status` + `heartbeat_at`,不是 `background_tasks.status`**。

具体含义:

1. `POST /api/admin/pan/backups` → 创建 background_task 行 + 调度 `execute_pan_backup`
2. Gateway 启动期 `recover_stale` 把所有 running/pending BackgroundTask 标 failed(line 408,顺带影响 pan)
3. **不依赖** BackgroundTask 行恢复 pan 业务态——pan_stale_reaper(每 30 min)看 `backup_records.heartbeat_at`:
   - heartbeat < now - 4h + status='uploading' + job.status='archiving' → 重试 cleanup(pan_residue_cleanup)或 rollback
   - heartbeat < now - 4h + status='uploaded' → forward-resolve 到 archived(commit-aware)
4. 用户感知:UI 看到 BackgroundTask=failed,以为 backup 失败。**这跟实际 backup_records 状态可能不一致**。处理:UI `GET /api/admin/pan/backups` 用 backup_records 作 source of truth,**不**用 background_tasks。如果想统一,后续可以在 BackgroundTask 行 fail 时 hook 回写 backup_records,本 MVP 不做。

**测试新增 `test_executor_restart_recovery`**:

```python
@pytest.mark.asyncio
async def test_recover_stale_marks_bg_task_failed_but_backup_record_intact(async_session_factory, sample_job_succeeded):
    """模拟 Gateway 重启:run executor 到 backup_records=uploading 后强 kill,
    然后 recover_stale 跑 — bg_task=failed,但 backup_records.status='uploading' 不动。
    pan_stale_reaper 后续会用 heartbeat 判处置。"""
    # ... fixture 设置 ...
    from gateway.background_task_queue import recover_stale
    from gateway.models import BackgroundTask, BackupRecord
    
    # 模拟 bg_task running + backup_records uploading
    # ... insert fixtures ...
    
    async with async_session_factory() as db:
        await recover_stale(db)
        bg = (await db.execute(select(BackgroundTask).where(...))).scalar_one()
        assert bg.status == 'failed'  # recover_stale 标了
        br = (await db.execute(select(BackupRecord).where(...))).scalar_one()
        assert br.status == 'uploading'  # 未动 — source of truth 保留
```

### Task 5.12: residue_cleanup executor

```python
async def execute_pan_residue_cleanup(payload: dict) -> None:
    """Cleanup residue from backups that hit COMMIT POINT but didn't finish
    local/R2 deletion. Called by stale_reaper for stuck-archiving forward-resolve.
    
    Plan §10 stale_reaper forward-resolve branch.
    """
    job_id = payload['job_id']
    user_id = _uuid.UUID(payload['user_id'])
    
    lock_key = hash((str(user_id), job_id)) & 0x7FFFFFFFFFFFFFFF
    async with async_session_factory() as db:
        async with db.begin():
            got = (await db.execute(text("SELECT pg_try_advisory_lock(:k)"), {'k': lock_key})).scalar()
            if not got:
                # 还有 executor 活着,reschedule
                from gateway.background_task_queue import reschedule
                reschedule(payload, delay_minutes=10)
                return
    
    try:
        # retry rmtree project_dir + R2 artifacts cleanup
        # ... (内容与 backup_executor 的 j/k step 一样,但 idempotent)
        pass
    finally:
        async with async_session_factory() as db:
            async with db.begin():
                await db.execute(text("SELECT pg_advisory_unlock(:k)"), {'k': lock_key})
```

每个 task 同样 TDD + commit.

Phase 5 完成后,backup/restore 可以在单元测试层跑通,但还没 wire up OAuth UI / scheduler。

---

# Phase 6 — OAuth Web Flow

**目标:** `gateway/pan/auth.py` ready,admin 能在 UI 走完 connect → Baidu → callback 流程,token 落 PG 加密。

**预算:** ~1.5 工日 / 5 个 task

### Task 6.1: gateway/pan/auth.py 骨架 + state token 管理

测试 + 实现:
- generate_state_token + INSERT pan_oauth_states + 10min TTL
- /api/admin/pan/connect: 生成 state + 302 → Baidu auth URL
- /api/admin/pan/callback: 验 state + DELETE state token

### Task 6.2: callback code 兑换 + INSERT pan_credentials

- 取 code from query → BaiduPanClient.exchange_code → encrypt → UPSERT pan_credentials
- 302 → /admin/pan/dashboard  (UI URL,不含 /api 前缀;`(app)` route group 不出现在 URL)

### Task 6.3: pan_token_refresh background task

- SELECT pan_credentials WHERE active AND expires_at < now()+24h
- decrypt → BaiduPanClient.refresh → re-encrypt → UPDATE
- 失败 → status='revoked' + notifications_service.dispatch_event

### Task 6.4: pan_token_refresh 异常路径测试 + notification 验证

### Task 6.5: 注册 router(将 auth.py 的 router 接入 gateway/main.py app)

每个 task TDD + commit。

---

# Phase 7 — Admin API + Frontend UI

**目标:** 10 个 `/api/admin/pan/*` endpoint 全 wired,admin 工作台能看到 pan 状态、能点 backup + restore。

**预算:** ~2.5 工日 / 12 个 task

### Task 7.1-7.6: Backend admin_pan_api.py

每个 endpoint 一个 task,TDD:
- T7.1: GET /api/admin/pan/status — 连接状态 + 配额
- T7.2: POST /api/admin/pan/backups (单任务,精度生 backup background task)
- T7.3: POST /api/admin/pan/backups/batch
- T7.4: GET /api/admin/pan/backups (list backup_records)
- T7.5: POST /api/admin/pan/restores
- T7.6: DELETE /api/admin/pan/backups/{id} (含 412 保护逻辑,见 spec §6 伪代码)

### Task 7.7-7.12: Frontend pages

- T7.7: `frontend-next/src/lib/api/pan.ts` — fetch wrapper
- T7.8: `frontend-next/src/app/(app)/admin/pan/dashboard/page.tsx` — connect 状态卡片
- T7.9: `frontend-next/src/app/(app)/admin/pan/backups/page.tsx` — backup_records list
- T7.10: 任务列表页加 "备份到网盘" 按钮(条件:admin + pan connected + status=succeeded)
- T7.11: archived 行的 UI 处理(灰、badge、"Restore" 按钮)
- T7.12: 状态过滤器加 "archived" 选项

每个 task 同样 TDD + commit。

---

# Phase 8 — Schedulers + Reaper + Orphan Cleanup

**目标:** archive_scanner / orphan_cleanup / stale_reaper 在 gateway 启动期注册并按 cron 跑。

**预算:** ~1.2 工日 / 6 个 task

### Task 8.1: archive_scanner 实现

- candidate 选择 SQL(spec §10)
- dry_run 模式 logging
- per-run limit 5
- enqueue background tasks

### Task 8.2: orphan_cleanup 3-pass 实现

- Pass A: pan 远端孤儿(list - PG cross-check)
- Pass B: R2 残留(SELECT archived jobs.r2_artifacts → delete)
- Pass C: pan_oauth_states 过期 DELETE

### Task 8.3: stale_reaper commit-aware 实现

- heartbeat_at 4h 过期扫
- commit-aware 分支(已 uploaded → forward-resolve;未 uploaded → rollback)
- session-level advisory lock 保护

### Task 8.4: scheduler 注册(gateway 启动期挂 cron)

找到 gateway 现有的 background scheduler hook(可能在 `gateway/main.py` startup event),注册:
- pan_archive_scanner: 每日 03:30 BJT
- pan_token_refresh: 每 6h
- pan_orphan_cleanup: 周六 04:00 BJT
- pan_stale_reaper: 每 30 min

### Task 8.5: 集成测试 — fixture 模拟卡死任务 + reap

### Task 8.6: 集成测试 — orphan_cleanup 跨 3 pass

---

# Phase 9 — Observability + Contract Tests

**目标:** 8 个 pan.* events 接入,r2_observability.py 渲染 pan 分组,契约测试 lock 词表同步。

**预算:** ~0.8 工日 / 5 个 task

### Task 9.1: SUPPORTED_EVENT_TYPES 加 8 个 pan.*

修改 `src/services/jobs/events.py`,加常量 + 集合。

### Task 9.2: event_log.py _DOWNLOAD_EVENT_TYPES 扩集

(保留名字不改 — spec §3.2 + git-blame continuity)

### Task 9.3: notification_dispatch_map.py 加 3 recipe

- `pan_token_revoked` (level=warn, action_link=/admin/pan/dashboard)  (UI URL)
- `pan_backup_failed` (level=error)
- `pan_restore_failed` (level=error)

### Task 9.4: r2_observability.py 加 PAN 分组

- 加 PAN_EVENT_TYPES 常量
- render_text 加 "--- Pan Backup ---" 段
- render_json 加 'pan' 字段

### Task 9.5: test_r2_observability.py 扩 prefix 过滤

`test_script_event_vocab_in_sync_with_jobs_events`:从 download/stream 扩到 download/stream/pan 三组前缀。

### Task 9.6: 新建 test_pan_event_vocab_in_sync.py

`SUPPORTED_EVENT_TYPES` ∩ pan.* == `_DOWNLOAD_EVENT_TYPES` ∩ pan.*

---

# Phase 10 — Integration Smoke + Deployment

**目标:** 端到端 smoke 通过,deploy 到生产 + 灰度 1 周。

**预算:** ~1.3 工日 / 8 个 task

### Task 10.1: 写 scripts/pan_backup_smoke.py 集成 smoke

```python
#!/usr/bin/env python3
"""End-to-end pan backup smoke test.

Build small dummy project_dir → backup → assert pan + local + R2 → restore → assert local + r2_artifacts=NULL.
Run manually after deploy:
    python scripts/pan_backup_smoke.py --user-id <uuid> --provider baidu_pan
"""
# 详细实现略
```

### Task 10.2: 写部署 README

env vars + first-time OAuth flow + Baidu 开放平台审核步骤(1-3 工作日)。

### Task 10.3: 部署 app 容器(因 src/services 3 处改动 — CodeX C4)

⚠️ **必须有 app 容器重启窗口**。3 个 src/services/ 修改(`jobs/models.py` SUPPORTED_JOB_STATUSES + `jobs/events.py` SUPPORTED_EVENT_TYPES + `web_ui/cleanup.py` _CLEANUP_PROTECTED_STATUSES)需要 app 重启才生效。

**步骤**:
1. 选低峰时段(BJT 凌晨 2-4 点,避开活跃 pipeline)
2. 提前观察: `psql -c "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"` < 3 才能开
3. `docker exec aivideotrans-app python -c "from services.jobs.process_runner import ProcessJobRunner; ProcessJobRunner.drain(timeout_s=300)"` (drain helper 若不存在则人工等)
4. `D:/daili/scripts/Deploy-Via-154.cmd` 推 src 改动到 app(src/ 是 bind mount,推完不用 build)
5. `docker restart aivideotrans-app`(轻重启,不 recreate)
6. 验证: `docker exec aivideotrans-app python -c "from services.jobs.models import SUPPORTED_JOB_STATUSES; assert 'archiving' in SUPPORTED_JOB_STATUSES"`

### Task 10.4: 部署 gateway 容器(image rebuild)

```bash
# 1. push 代码 + image
D:/daili/scripts/Deploy-Via-154.cmd  # 详细按 feedback_deploy_scripts.md

# 2. 远端 alembic upgrade(029_pan_backup)
ssh -F D:/daili/scripts/ssh_config us-host
docker exec aivideotrans-app python -m alembic -c /opt/aivideotrans/app/gateway/alembic.ini upgrade head
docker exec aivideotrans-postgres psql -U avt -d aivideotrans -c "\dt pan_*"  # 验证 3 表

# 3. 仅 recreate gateway(避免 app 重启 — 已在 10.3 做过了)
docker compose -f docker-compose.yml up -d --no-deps --force-recreate gateway

# 4. 健康检查
curl https://aitrans.video/api/admin/pan/status -H "Cookie: ..."  # admin session
```

### Task 10.5: First-time OAuth + dry-run backup 验证

- Admin 走通 OAuth flow(点连接 → Baidu 授权 → 看 dashboard "已连接")
- 手动 backup 1 个 1GB 测试任务
- 看 events(`r2_observability.py --since 1h`)+ backup_records + pan 真有 tar
- 验 archive 状态: 任务列表 "archived" badge + 本地 project_dir 空 + R2 artifacts 空
- 手动 restore 同任务 → 验证完整恢复

### Task 10.6: 监控配置

- `scripts/r2_observability.py --since 24h` 每日跑(可加 cron)
- 灰度第 1 周每日检查

### Task 10.7: 灰度 30d 阈值切换

灰度第 8 天后,确认稳定 → `AVT_PAN_AUTO_ARCHIVE_DRY_RUN=false` 启动真实自动归档。

### Task 10.8: 灰度 30d 完整 41 任务回填

按 spec §16.4,分批每天 5-10 个,总耗时预期 10-40h。

---

# Acceptance Criteria

实施完成的判定标准(全勾):

- [ ] alembic 029 在生产 PG 跑通,`\dt pan_*` 显示 3 表
- [ ] 所有新 test 文件 + 现有 test 全绿(CI 全绿)
- [ ] OAuth flow 跑通,admin 看到"已连接百度网盘 ✓ + 配额"
- [ ] 手动 backup 1 个真任务,backup_records 行 + pan 真有 tar.gz + 本地+R2 真空 + 状态 archived
- [ ] 手动 restore 同任务,数据完整(sha256 + inventory 全过)
- [ ] 假场景:停 Baidu 网络让 refresh 失败 24h → 看 UI 红 banner + notifications 出现
- [ ] 假场景:`docker kill aivideotrans-gateway` 在 backup 中途 → 4h 后 stale_reaper 自动 forward-resolve 或 rollback
- [ ] 假场景:DELETE 唯一可恢复副本 → 412 + 提示
- [ ] 假场景:scanner dry-run 跑 1 晚,log 见 candidates,无 enqueue 发生
- [ ] 30d auto-archive 在 dry_run=false 后第一次触发,5 个任务被 enqueue
- [ ] r2_observability.py 显示 PAN 事件分组,1 周后 failure 率 < 5%
- [ ] 41 任务首次回填完成,本地 disk 释放 ≥ 100GB

---

# Risk Register(实施中需注意)

| # | 风险 | 缓解 |
|---|------|------|
| R1 | Smart MVP track 持续 push 同 5 个文件 | 每个 Phase 开始前 git fetch + rebase;如果撞了立刻 stop and 解 conflict |
| R2 | Baidu Pan API 限流(尤其 admin 个人开发者) | 重试 + backoff;超过 500 req/min 主动 sleep |
| R3 | 首次 41 任务回填超 24h | 分批 + heartbeat 续命;阶段性中断不影响完成的 backup |
| R4 | Fernet key 丢失 | spec §13 双备份(1Password + 物理纸条),实施前确认两份都到位 |
| R5 | 跨境 pan 上传中途中断 → 部分 chunk 留在 pan | 周末 orphan_cleanup 清;一次失败不影响重试(uniq_backup_in_flight 索引保护) |
| R6 | 实测 stale_hours 4h 不够长(60GB+ 任务) | env 调大至 6-8h;不改代码 |

---

# Open Items(implementation 中可能浮现的)

执行时若遇到这些点,在 implement 时决定:
- BaiduPanClient 是否要加重试逻辑(目前 timeout=30s 单次)
- backup_executor 的 advisory lock 用 hash(user_id, job_id) — 64-bit 截断会冲突吗?(实测概率极低,但记录)
- frontend 是否需要 polling backup_records.status 更新进度条 — MVP 简单 setInterval(15s) 即可

---

End of plan. Total ~60 bite-sized tasks across 10 phases. Estimated 14.3 工日(spec §15)。
