"""Structural validation tests for pan backup models.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 1.2
Migration: gateway/alembic/versions/029_pan_backup.py (T1.1)

Why structural (not round-trip): the project has no async DB fixtures
(verified — no async_session_factory / sample_user in tests/conftest.py).
Round-trip tests would require building test DB infra outside this task's
scope. Structural tests verify the models declare the right schema via
SQLAlchemy metadata introspection — every column name, type, FK target,
ondelete cascade, server_default, and constraint name spec'd in T1.1
migration is checked here.

What's NOT covered (deferred to integration phase Phase 10):
- server_default values are only confirmed at metadata level, not exercised
  via real DB INSERT — a misconfigured migration would not be caught here
- Real cascade behavior on row delete
- JSONB serialization round-trip
"""
import uuid

import pytest
from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


# ---------------------------------------------------------------------------
# Imports work — first sanity check
# ---------------------------------------------------------------------------

def test_pan_models_importable():
    from models import PanCredentials, BackupRecord, PanOauthState
    # Each must be a class
    assert isinstance(PanCredentials, type)
    assert isinstance(BackupRecord, type)
    assert isinstance(PanOauthState, type)


# ---------------------------------------------------------------------------
# PanCredentials
# ---------------------------------------------------------------------------

def test_pan_credentials_tablename():
    from models import PanCredentials
    assert PanCredentials.__tablename__ == "pan_credentials"


def test_pan_credentials_columns_present():
    from models import PanCredentials
    cols = {c.name for c in PanCredentials.__table__.columns}
    expected = {
        'id', 'user_id', 'provider',
        'access_token_encrypted', 'refresh_token_encrypted',
        'access_token_expires_at', 'scope', 'status',
        'connected_at', 'last_refreshed_at',
    }
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_pan_credentials_id_is_uuid_pk():
    from models import PanCredentials
    id_col = PanCredentials.__table__.columns['id']
    assert id_col.primary_key
    # Type python-side: should be UUID
    assert 'UUID' in str(id_col.type) or str(id_col.type) == 'UUID'


def test_pan_credentials_user_id_cascades():
    from models import PanCredentials
    user_id_col = PanCredentials.__table__.columns['user_id']
    fks = list(user_id_col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == 'users'
    assert fk.ondelete == 'CASCADE'


def test_pan_credentials_status_default_active():
    from models import PanCredentials
    status_col = PanCredentials.__table__.columns['status']
    # server_default should be the literal 'active'
    assert status_col.server_default is not None
    sd_text = status_col.server_default.arg
    if hasattr(sd_text, 'text'):
        sd_text = sd_text.text
    assert "active" in str(sd_text)


def test_pan_credentials_unique_constraint():
    from models import PanCredentials
    # Look for the named UniqueConstraint
    uc = next(
        (c for c in PanCredentials.__table__.constraints
         if getattr(c, 'name', None) == 'uq_pan_credentials_user_provider'),
        None,
    )
    assert uc is not None, "uq_pan_credentials_user_provider constraint missing"
    cols = {c.name for c in uc.columns}
    assert cols == {'user_id', 'provider'}


def test_pan_credentials_can_instantiate_with_required():
    """Pure Python-level instantiation (no DB) — verifies init signature is sane."""
    from models import PanCredentials
    from datetime import datetime, timezone, timedelta
    obj = PanCredentials(
        user_id=uuid.uuid4(),
        provider='baidu_pan',
        access_token_encrypted=b'enc',
        refresh_token_encrypted=b'enc',
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        scope='basic,netdisk',
    )
    assert obj.provider == 'baidu_pan'


# ---------------------------------------------------------------------------
# BackupRecord
# ---------------------------------------------------------------------------

def test_backup_record_tablename():
    from models import BackupRecord
    assert BackupRecord.__tablename__ == "backup_records"


def test_backup_record_columns_present():
    from models import BackupRecord
    cols = {c.name for c in BackupRecord.__table__.columns}
    expected = {
        'id', 'user_id', 'job_id', 'job_edit_generation', 'provider',
        'remote_path', 'size_bytes', 'sha256', 'md5', 'manifest_json',
        'status', 'heartbeat_at', 'created_at', 'completed_at', 'error_message',
    }
    assert expected.issubset(cols), f"Missing columns: {expected - cols}"


def test_backup_record_job_id_has_no_fk():
    """Critical: job_id MUST NOT be FK to jobs.job_id — must survive job row deletion.
    Migration 029 explicitly skips FK on this column."""
    from models import BackupRecord
    job_id_col = BackupRecord.__table__.columns['job_id']
    assert len(list(job_id_col.foreign_keys)) == 0, "job_id should not have FK"


def test_backup_record_size_bytes_is_bigint():
    from models import BackupRecord
    size_col = BackupRecord.__table__.columns['size_bytes']
    # BigInteger type check
    assert isinstance(size_col.type, BigInteger), (
        f"size_bytes type is {type(size_col.type).__name__}, expected BigInteger"
    )


def test_backup_record_manifest_is_jsonb():
    from models import BackupRecord
    manifest_col = BackupRecord.__table__.columns['manifest_json']
    assert isinstance(manifest_col.type, JSONB), (
        f"manifest_json type is {type(manifest_col.type).__name__}, expected JSONB"
    )


def test_backup_record_job_edit_generation_default_zero():
    from models import BackupRecord
    gen_col = BackupRecord.__table__.columns['job_edit_generation']
    assert isinstance(gen_col.type, Integer)
    assert gen_col.server_default is not None
    sd_text = gen_col.server_default.arg
    if hasattr(sd_text, 'text'):
        sd_text = sd_text.text
    assert str(sd_text).strip("'\"") == "0"


def test_backup_record_can_instantiate_with_required():
    from models import BackupRecord
    obj = BackupRecord(
        user_id=uuid.uuid4(),
        job_id='job_test_001',
        job_edit_generation=0,
        provider='baidu_pan',
        remote_path='/apps/AIVideoTrans/backups/job_test_001.tar.gz',
        size_bytes=12345,
        sha256='a' * 64,
        md5='b' * 32,
        manifest_json={'backup_format_version': 1},
        status='uploading',
    )
    assert obj.job_id == 'job_test_001'


# ---------------------------------------------------------------------------
# PanOauthState
# ---------------------------------------------------------------------------

def test_pan_oauth_state_tablename():
    from models import PanOauthState
    assert PanOauthState.__tablename__ == "pan_oauth_states"


def test_pan_oauth_state_token_is_pk_string():
    from models import PanOauthState
    token_col = PanOauthState.__table__.columns['token']
    assert token_col.primary_key
    assert isinstance(token_col.type, String)
    assert token_col.type.length == 64


def test_pan_oauth_state_user_id_cascades():
    from models import PanOauthState
    fks = list(PanOauthState.__table__.columns['user_id'].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == 'users'
    assert fks[0].ondelete == 'CASCADE'


def test_pan_oauth_state_can_instantiate():
    from models import PanOauthState
    from datetime import datetime, timezone, timedelta
    obj = PanOauthState(
        token='a' * 32,
        user_id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert obj.token == 'a' * 32
