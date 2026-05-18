"""Tests for gateway.pan.status_mutator.set_archive_status.

Plan 2026-05-14 §T5.1. Uses in-memory async SQLite to exercise the PG
UPDATE path; uses tmp_path + monkeypatch to exercise the JSON mirror;
uses a source-text contract guard to lock down the "no mirror call" rule.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles


# Gateway sys.path already set up by tests/conftest.py.
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database module so importing status_mutator doesn't pull in real
# database engine (matches the pattern in test_materials_pack_executor.py).
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)


# Make PG-only types render under SQLite.
@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _setup_engine_with_job(
    *,
    job_id: str,
    user_id: uuid.UUID,
    status: str = 'succeeded',
):
    """Bootstrap an in-memory SQLite engine, create the Job table, and
    insert a single Job row. Returns the engine — caller is responsible
    for closing."""
    from models import Job

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Job.__table__.create(c))
        await conn.execute(
            Job.__table__.insert().values(
                id=uuid.uuid4(),
                job_id=job_id,
                user_id=user_id,
                status=status,
            )
        )
    return engine


# --- T5.1: PG-side update ---


def test_set_archive_status_writes_pg(tmp_path):
    """conn.execute(UPDATE Job.status) lands on the PG row."""
    from models import Job
    from gateway.pan.status_mutator import set_archive_status

    job_id = 'job_pg_update'
    user_id = uuid.uuid4()

    async def _go() -> None:
        engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await set_archive_status(user_id, job_id, 'archiving',
                                             conn=conn)

                # Read back through the same connection — verifies UPDATE
                # landed in the txn.
                loaded_status = (await conn.execute(
                    select(Job.status).where(Job.job_id == job_id)
                )).scalar_one()
                assert loaded_status == 'archiving'
        finally:
            await engine.dispose()

    _run(_go())


def test_set_archive_status_supports_all_three_new_statuses(tmp_path):
    """archiving / archived / restoring all writable (plan §3.1 triplet)."""
    from models import Job
    from gateway.pan.status_mutator import set_archive_status

    async def _go() -> None:
        for new_status in ('archiving', 'archived', 'restoring'):
            user_id = uuid.uuid4()
            job_id = f'job_status_{new_status}'
            engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
            try:
                async with engine.connect() as conn:
                    async with conn.begin():
                        await set_archive_status(
                            user_id, job_id, new_status, conn=conn
                        )
                    loaded_status = (await conn.execute(
                        select(Job.status).where(Job.job_id == job_id)
                    )).scalar_one()
                    assert loaded_status == new_status
            finally:
                await engine.dispose()

    _run(_go())


# --- T5.1: JSON-side mirror ---


def test_set_archive_status_writes_json_mirror(tmp_path, monkeypatch):
    """If {jobs_dir}/{job_id}.json exists, status field is updated in place."""
    from gateway.pan.status_mutator import set_archive_status

    job_id = 'job_json_mirror'
    user_id = uuid.uuid4()
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    json_path = tmp_path / f'{job_id}.json'
    json_path.write_text(json.dumps({
        'job_id': job_id, 'status': 'succeeded', 'extra': 'preserved',
    }), encoding='utf-8')

    async def _go() -> None:
        engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await set_archive_status(user_id, job_id, 'archiving',
                                             conn=conn)
        finally:
            await engine.dispose()

    _run(_go())

    record = json.loads(json_path.read_text(encoding='utf-8'))
    assert record['status'] == 'archiving'
    assert record['job_id'] == job_id  # other fields preserved
    assert record['extra'] == 'preserved'


def test_set_archive_status_skips_missing_json(tmp_path, monkeypatch):
    """If JSON file doesn't exist, function returns silently (no exception).
    Gateway-only states (archiving/archived/restoring) may not have a JSON
    counterpart; PG is authoritative."""
    from gateway.pan.status_mutator import set_archive_status

    job_id = 'job_no_json'
    user_id = uuid.uuid4()
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))
    # No JSON file created — only an empty dir.

    async def _go() -> None:
        engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await set_archive_status(user_id, job_id, 'archived',
                                             conn=conn)
        finally:
            await engine.dispose()

    _run(_go())
    # No exception raised — PG was updated, JSON was skipped.


def test_set_archive_status_logs_but_doesnt_raise_on_json_corrupt(
    tmp_path, monkeypatch, caplog
):
    """JSON read/write failure → log WARNING, do NOT raise. Backup records
    are the source of truth; JSON mirror is best-effort."""
    from gateway.pan.status_mutator import set_archive_status
    import logging

    job_id = 'job_json_corrupt'
    user_id = uuid.uuid4()
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    json_path = tmp_path / f'{job_id}.json'
    json_path.write_bytes(b'NOT JSON {{{')  # corrupt content

    async def _go() -> None:
        engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    with caplog.at_level(logging.WARNING,
                                         logger='gateway.pan.status_mutator'):
                        await set_archive_status(user_id, job_id, 'archiving',
                                                 conn=conn)
        finally:
            await engine.dispose()

    _run(_go())

    # Warning was logged but no exception escaped.
    assert any('JSON mirror failed' in rec.message for rec in caplog.records), \
        f"expected JSON mirror warning, got: {[r.message for r in caplog.records]}"


def test_set_archive_status_preserves_unicode_in_json(tmp_path, monkeypatch):
    """Non-ASCII fields survive read-modify-write round-trip."""
    from gateway.pan.status_mutator import set_archive_status

    job_id = 'job_unicode'
    user_id = uuid.uuid4()
    monkeypatch.setenv('AIVIDEOTRANS_JOBS_DIR', str(tmp_path))

    json_path = tmp_path / f'{job_id}.json'
    json_path.write_text(json.dumps({
        'job_id': job_id, 'status': 'succeeded',
        'title': '中文标题 ✨',
    }, ensure_ascii=False), encoding='utf-8')

    async def _go() -> None:
        engine = await _setup_engine_with_job(job_id=job_id, user_id=user_id)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await set_archive_status(user_id, job_id, 'restoring',
                                             conn=conn)
        finally:
            await engine.dispose()

    _run(_go())

    record = json.loads(json_path.read_text(encoding='utf-8'))
    assert record['status'] == 'restoring'
    assert record['title'] == '中文标题 ✨'


# --- T5.1: contract guard — must NOT touch mirror_job_terminal_state ---


def test_status_mutator_does_not_import_mirror_module():
    """Contract: status_mutator's source must not reference mirror_job_terminal_state
    by name in any form. Even indirect calls would have to be imported
    here, so a source-text guard catches the contract violation at module
    load time — long before any runtime test could fire."""
    src = Path(__file__).resolve().parent.parent / 'gateway' / 'pan' / 'status_mutator.py'
    text = src.read_text(encoding='utf-8')
    # Plain substring check — comments mentioning the name are allowed
    # ONLY in docstrings (we look for the import / call pattern, not the
    # word). 'mirror_job_terminal_state(' would be a call site; the
    # module name 'job_terminal_mirror' would be an import target.
    # We tolerate both forms only inside comment lines that explicitly
    # explain WHY mirror is not used.
    code_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    in_docstring = False
    docstring_marker = '"""'
    suspect = []
    for line in code_lines:
        # Toggle docstring state on triple-quote occurrences.
        marker_count = line.count(docstring_marker)
        if marker_count == 1:
            in_docstring = not in_docstring
            continue
        if marker_count >= 2:
            # Same-line docstring open+close — no state change.
            continue
        if in_docstring:
            continue
        # Outside docstring: any reference is a violation.
        if 'mirror_job_terminal_state' in line or 'job_terminal_mirror' in line:
            suspect.append(line)
    assert not suspect, (
        f"status_mutator.py must not reference mirror_job_terminal_state outside "
        f"docstrings. Offending lines:\n" + "\n".join(suspect)
    )


def test_status_mutator_module_has_documented_no_mirror_rationale():
    """The docstring must explicitly document the "no mirror" rule so future
    readers know it's intentional. Lock the rationale alongside the code."""
    from gateway.pan import status_mutator as sm
    doc = (sm.__doc__ or '').lower()
    assert 'mirror' in doc
    assert 'not' in doc or 'does not' in doc or "doesn't" in doc.replace("'", "'")
