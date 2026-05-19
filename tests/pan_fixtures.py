"""Plain Python helpers for Phase 5b pan executor tests.

Not pytest fixtures (would require conftest registration). These are
composable helper functions that each test imports explicitly.

Provided:
  - pan_test_engine() async context manager: in-memory SQLite + Job /
    BackupRecord / PanCredentials tables, with @compiles patches so PG-only
    types (JSONB, UUID) render under SQLite.
  - insert_sample_job() — Job row with sensible defaults.
  - insert_sample_pan_credentials() — PanCredentials with Fernet-encrypted
    tokens (test key auto-generated if env unset).
  - insert_sample_backup_record() — BackupRecord row.
  - make_project_dir() — realistic project_dir layout under tmp_path.
  - FakeBaiduPanClient — drop-in replacement for BaiduPanClient that
    records calls and lets tests inject failure modes.
  - setup_pan_token_env() — monkeypatches settings.pan_token_encryption_key
    + AVT_PAN_TOKEN_ENCRYPTION_KEY with a fresh Fernet key.

Convention: tests use `_run(_go())` to call async code from sync test fns
(mirrors tests/test_materials_pack_executor.py pattern).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from cryptography.fernet import Fernet
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.ext.compiler import compiles


# --- sys.path bootstrap so we can `from models import ...` etc. ---

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database module so importing gateway code that does
# `from database import engine` doesn't pull in real PG wiring.
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)


# --- PG-only types → SQLite ---

@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


# --- async test runner ---

def run_async(coro):
    """Run a coroutine in a fresh event loop. Convenience for sync test fns."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- engine + tables ---

@asynccontextmanager
async def pan_test_engine():
    """In-memory SQLite with Job + BackupRecord + PanCredentials tables.

    Disposes on exit. Users table is NOT created — FK enforcement is OFF on
    SQLite by default, so we save the schema noise and just use bare UUIDs.
    """
    from models import Job, BackupRecord, PanCredentials

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            for table_cls in (Job, BackupRecord, PanCredentials):
                await conn.run_sync(lambda c, t=table_cls: t.__table__.create(c))
        yield engine
    finally:
        await engine.dispose()


# --- token env setup ---

def setup_pan_token_env(monkeypatch) -> str:
    """Generate a Fernet key, set it as env var AND monkeypatch
    settings.pan_token_encryption_key on every settings instance we can
    reach. Returns the key (decoded string).

    Why both forms: depending on sys.path config, gateway/config.py can
    end up loaded BOTH as `config` (when gateway/ is on sys.path) and
    `gateway.config` (when repo root is on sys.path) — they become two
    distinct module objects with two distinct singleton settings instances.
    `gateway.pan.token_crypto` uses the dotted form, but other code may
    use the bare form, so we patch both.
    """
    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv('AVT_PAN_TOKEN_ENCRYPTION_KEY', test_key)

    # 1. Dotted form — gateway.pan.token_crypto, gateway.pan.status_mutator, …
    from config import settings as gw_settings
    monkeypatch.setattr(gw_settings, 'pan_token_encryption_key', test_key,
                        raising=False)

    # 2. Bare form — code that imports while gateway/ is the working dir.
    try:
        from config import settings as cfg_settings  # type: ignore
        if cfg_settings is not gw_settings:
            monkeypatch.setattr(cfg_settings, 'pan_token_encryption_key',
                                test_key, raising=False)
    except ImportError:
        pass

    return test_key


# --- row inserts ---

async def insert_sample_job(
    engine: AsyncEngine,
    *,
    user_id: uuid.UUID,
    job_id: str,
    status: str = 'succeeded',
    edit_generation: int = 0,
    project_dir: str | None = None,
    r2_artifacts: list[dict] | None = None,
) -> dict:
    """INSERT a Job row. Returns dict snapshot of inserted values."""
    from models import Job

    row_id = uuid.uuid4()
    values = {
        'id': row_id,
        'job_id': job_id,
        'user_id': user_id,
        'status': status,
        'edit_generation': edit_generation,
    }
    if project_dir is not None:
        values['project_dir'] = project_dir
    if r2_artifacts is not None:
        values['r2_artifacts'] = r2_artifacts

    async with engine.begin() as conn:
        await conn.execute(Job.__table__.insert().values(**values))
    return values


async def insert_sample_pan_credentials(
    engine: AsyncEngine,
    *,
    user_id: uuid.UUID,
    provider: str = 'baidu_pan',
    access_token: str = 'access_test_token',
    refresh_token: str = 'refresh_test_token',
    status: str = 'active',
    expires_in_seconds: int = 2592000,
) -> dict:
    """INSERT a PanCredentials row with encrypted tokens.

    setup_pan_token_env() MUST be called first so token_crypto can encrypt.
    """
    from models import PanCredentials
    from pan.token_crypto import encrypt_token

    row_id = uuid.uuid4()
    access_enc = encrypt_token(access_token)
    refresh_enc = encrypt_token(refresh_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    values = {
        'id': row_id,
        'user_id': user_id,
        'provider': provider,
        'access_token_encrypted': access_enc,
        'refresh_token_encrypted': refresh_enc,
        'access_token_expires_at': expires_at,
        'status': status,
        'connected_at': datetime.now(timezone.utc),
    }
    async with engine.begin() as conn:
        await conn.execute(PanCredentials.__table__.insert().values(**values))
    return values


async def insert_sample_backup_record(
    engine: AsyncEngine,
    *,
    user_id: uuid.UUID,
    job_id: str,
    job_edit_generation: int = 0,
    provider: str = 'baidu_pan',
    remote_path: str = '/apps/AIVideoTrans/backups/test.tar.gz',
    size_bytes: int = 0,
    sha256: str = '',
    md5: str = '',
    manifest_json: dict | None = None,
    status: str = 'uploading',
    heartbeat_at: datetime | None = None,
) -> dict:
    """INSERT a BackupRecord row."""
    from models import BackupRecord

    row_id = uuid.uuid4()
    values = {
        'id': row_id,
        'user_id': user_id,
        'job_id': job_id,
        'job_edit_generation': job_edit_generation,
        'provider': provider,
        'remote_path': remote_path,
        'size_bytes': size_bytes,
        'sha256': sha256,
        'md5': md5,
        'manifest_json': manifest_json if manifest_json is not None else {},
        'status': status,
        'heartbeat_at': heartbeat_at,
        'created_at': datetime.now(timezone.utc),
    }
    async with engine.begin() as conn:
        await conn.execute(BackupRecord.__table__.insert().values(**values))
    return values


# --- file system fixtures ---

def make_project_dir(
    parent: Path,
    job_id: str = 'job_test',
    *,
    monkeypatch=None,
) -> Path:
    """Create a realistic project_dir layout under `parent` (typically tmp_path).

    Layout:
        {parent}/{job_id}/
          transcript/review.json
          tts/seg_0.wav
          publish/dubbed.mp4

    When `monkeypatch` is provided, registers `parent` as a safe project
    root via AIVIDEOTRANS_PROJECTS_DIR — required for tests that hit
    backup_executor / restore_executor / residue_cleanup, since those
    now enforce the gateway.project_cleanup safe-root whitelist
    (CodeX P0 unification).
    """
    if monkeypatch is not None:
        monkeypatch.setenv('AIVIDEOTRANS_PROJECTS_DIR', str(parent))
    project = parent / job_id
    (project / 'transcript').mkdir(parents=True)
    (project / 'transcript' / 'review.json').write_text(
        json.dumps({'job_id': job_id, 'segments': []}),
        encoding='utf-8',
    )
    (project / 'tts').mkdir()
    (project / 'tts' / 'seg_0.wav').write_bytes(b'\x52\x49\x46\x46' + b'\x00' * 1020)
    (project / 'publish').mkdir()
    (project / 'publish' / 'dubbed.mp4').write_bytes(b'mp4_payload' * 100)
    return project


# --- FakeBaiduPanClient ---

class FakeBaiduPanClient:
    """Drop-in replacement for BaiduPanClient. No HTTP, no real Baidu API.

    Records every call. Simulates remote storage in an in-memory dict so
    upload-then-download round-trips work for integration tests.

    Failure injection:
      - inject_upload_failure(exc)    — upload() raises exc on next call
      - inject_upload_response(dict)  — upload() returns this dict (override
                                         the real md5/size calculation —
                                         useful for md5-mismatch gate tests)
      - inject_verify_result(bool)    — verify_remote_tail() returns this
      - inject_delete_failure(exc)    — delete() raises exc on next call
    """

    OAUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
    XPAN_BASE = "https://pan.baidu.com/rest/2.0/xpan"
    PCS_BASE = "https://d.pcs.baidu.com/rest/2.0/pcs"

    def __init__(self, appkey: str = 'fake_appkey', appsecret: str = 'fake_secret'):
        self.appkey = appkey
        self.appsecret = appsecret

        # Call records — every public method appends.
        self.upload_calls: list[dict] = []
        self.download_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.list_calls: list[dict] = []
        self.verify_remote_tail_calls: list[dict] = []
        self.exchange_code_calls: list[dict] = []
        self.refresh_calls: list[dict] = []
        self.get_quota_calls: list[dict] = []

        # In-memory storage simulation: remote_path → file content bytes.
        self._storage: dict[str, bytes] = {}

        # Failure injection.
        self._upload_failure: Exception | None = None
        self._upload_response_override: dict | None = None
        self._verify_result: bool = True
        self._delete_failure: Exception | None = None
        self._download_failure: Exception | None = None

    # --- failure injection ---
    def inject_upload_failure(self, exc: Exception) -> None:
        self._upload_failure = exc

    def inject_upload_response(self, response: dict) -> None:
        self._upload_response_override = response

    def inject_verify_result(self, ok: bool) -> None:
        self._verify_result = ok

    def inject_delete_failure(self, exc: Exception) -> None:
        self._delete_failure = exc

    def inject_download_failure(self, exc: Exception) -> None:
        self._download_failure = exc

    # --- pan client surface (matches gateway.pan.baidu_pan_client.BaiduPanClient) ---

    def upload(self, local_path: Path, remote_path: str, *, access_token: str) -> dict:
        self.upload_calls.append({
            'local_path': str(local_path),
            'remote_path': remote_path,
            'access_token': access_token,
        })
        if self._upload_failure:
            exc, self._upload_failure = self._upload_failure, None
            raise exc

        local_path = Path(local_path)
        content = local_path.read_bytes()
        self._storage[remote_path] = content

        if self._upload_response_override is not None:
            return self._upload_response_override
        return {
            'size': len(content),
            'md5': hashlib.md5(content).hexdigest(),
            'fs_id': 12345,
        }

    def download(self, remote_path: str, local_path: Path, *, access_token: str) -> dict:
        self.download_calls.append({
            'remote_path': remote_path,
            'local_path': str(local_path),
            'access_token': access_token,
        })
        if self._download_failure:
            exc, self._download_failure = self._download_failure, None
            raise exc

        content = self._storage.get(remote_path)
        if content is None:
            raise RuntimeError(
                f"FakeBaiduPanClient: nothing uploaded at {remote_path!r}"
            )
        Path(local_path).write_bytes(content)
        return {
            'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest(),
            'md5': '',  # caller verifies against BackupRecord.md5
        }

    def delete(self, remote_path: str, *, access_token: str) -> None:
        self.delete_calls.append({
            'remote_path': remote_path,
            'access_token': access_token,
        })
        if self._delete_failure:
            exc, self._delete_failure = self._delete_failure, None
            raise exc
        self._storage.pop(remote_path, None)  # idempotent

    def list(self, prefix: str, *, access_token: str) -> list[dict]:
        self.list_calls.append({'prefix': prefix, 'access_token': access_token})
        return [
            {'path': k, 'size': len(v), 'fs_id': abs(hash(k)) & 0x7FFFFFFF}
            for k, v in sorted(self._storage.items())
            if k.startswith(prefix)
        ]

    def verify_remote_tail(
        self,
        local_path: Path,
        remote_path: str,
        size: int,
        *,
        access_token: str,
        probe_bytes: int = 64 * 1024,
    ) -> bool:
        self.verify_remote_tail_calls.append({
            'local_path': str(local_path),
            'remote_path': remote_path,
            'size': size,
        })
        return self._verify_result

    def get_quota(self, *, access_token: str) -> dict:
        self.get_quota_calls.append({'access_token': access_token})
        return {'total': 2 * 10**12, 'used': 500 * 10**9, 'free': int(1.5 * 10**12)}

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        self.exchange_code_calls.append({'code': code, 'redirect_uri': redirect_uri})
        return {
            'access_token': 'fake_access',
            'refresh_token': 'fake_refresh',
            'expires_in': 2592000,
            'scope': 'basic netdisk',
        }

    def refresh(self, refresh_token: str) -> dict:
        self.refresh_calls.append({'refresh_token': refresh_token})
        return {
            'access_token': 'new_access',
            'refresh_token': 'new_refresh',
            'expires_in': 2592000,
            'scope': 'basic netdisk',
        }


# ---------------------------------------------------------------------------
# CodeX P2 (2026-05-19): launch isolation for pan tests
# ---------------------------------------------------------------------------
#
# Phase 8 tests for scanner/reaper trigger ``run_archive_scanner_tick`` /
# ``run_stale_reaper_tick`` which call into ``pan._enqueue.enqueue_pan_task``.
# The helper does ``asyncio.create_task(executor(...))`` which schedules the
# REAL backup_executor / residue_cleanup coroutine. Those executors then run
# against the fake ``database`` MagicMock and hit ``async with conn.begin()``,
# producing ``RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call'
# was never awaited``. The warnings are harmless (the coroutine is
# immediately GC'd) but they pollute the test output and could hide other
# real warnings.
#
# Use ``install_no_launch(monkeypatch)`` in any test that goes through
# ``enqueue_pan_task`` and doesn't itself assert on the launched coroutine.
# The patch replaces ``pan._enqueue._launch_coroutine`` with a record-only
# stub that calls ``coro.close()`` to suppress the warning.


def install_no_launch(monkeypatch) -> list[dict]:
    """Block the real executor coroutine from running in pan tests.

    Returns a list that captures every launch attempt as
    ``{'name': str, 'coro_qualname': str}`` so individual tests can still
    assert how many enqueues happened.

    Pattern lifted from tests/test_pan_enqueue.py._install_launch_capture
    so all pan tests share one strategy. Keep them in sync.
    """
    from pan import _enqueue as mod

    launched: list[dict] = []

    def fake_launch(coro, name: str):
        launched.append({'name': name, 'coro_qualname': coro.__qualname__})
        coro.close()  # avoid RuntimeWarning: coroutine never awaited
        return None

    monkeypatch.setattr(mod, '_launch_coroutine', fake_launch)
    return launched
