"""T1-9 + T1-10 round 2 fixes (CodeX Phase 1 review).

Two P1 issues and their guards:

1. Gateway ``_apply_editing_commit_gateway_side`` — after Job-API's
   editing/commit returns 2xx with the T1-9 response shape, Gateway must
   sync its PostgreSQL ``jobs`` row. Overwrite flips the source row to
   ``running`` + bumps ``edit_generation``; copy_as_new resets source to
   ``succeeded`` and INSERTs a new row for the copy.

2. ``main.run_job_api_command`` — must call
   ``inject_editing_cancel_callback(service)`` + ``start_cleanup_thread()``
   at Job-API startup. Without these the idle scanner stays on no-op
   forever.

We test the gateway helper directly (faking AsyncSession + Job) rather
than spinning up the full FastAPI app — the branching logic is all in
this function and mocks keep the test hermetic.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Response

_GATEWAY_DIR = Path(__file__).resolve().parents[1] / "gateway"
_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
for _cand in (_GATEWAY_DIR, _SRC_DIR):
    if str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

import job_intercept  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Fake SQLAlchemy surface — minimum we need from AsyncSession / Job
# ---------------------------------------------------------------------------


@dataclass
class _FakeJobRow:
    """Stand-in for ``gateway.models.Job``. Supports attribute reads + writes
    the same way the real ORM row does, but we don't care about declarative
    base hooks here."""
    job_id: str
    user_id: str = "u1"
    source_type: str = "youtube_url"
    source_ref: str = "https://example.com"
    title: str = ""
    speakers: str = "auto"
    status: str = "editing"
    current_stage: str | None = None
    project_dir: str | None = None
    review_gate: Any | None = None
    error_summary: Any | None = None
    service_mode: str = "studio"
    tts_provider: str | None = None
    tts_model: str | None = None
    requires_review: bool | None = None
    voice_clone_enabled: bool | None = None
    voice_strategy: str | None = None
    plan_code_snapshot: str | None = None
    role_snapshot: str | None = None
    source_duration_seconds: float | None = None
    quota_cost: int = 0
    quota_state: str = "none"
    estimated_duration_seconds: float | None = None
    create_idempotency_key: str | None = None
    display_name: str | None = None
    expires_at: datetime | None = None
    editing_touched_at: datetime | None = None
    copy_of_job_id: str | None = None
    root_job_id: str | None = None
    edit_generation: int = 0
    source_content_hash: str | None = None
    metering_snapshot: dict | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class _FakeResult:
    value: Any = None

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value


class _FakeSession:
    """Tracks queries + INSERTs so assertions can introspect behaviour.

    The helper issues execute() calls in a fixed order for copy_as_new:
      1. Idempotency lookup — "does this new_job_id already exist?"
      2. Sibling lookup — "most recent live copy in the same lineage"
    We pre-seed one ``_FakeResult`` per expected call so both branches
    (normal / already-exists) work deterministically.
    """

    def __init__(
        self,
        *,
        existing_sibling: _FakeJobRow | None = None,
        new_id_already_exists: bool = False,
    ) -> None:
        self.added_rows: list[Any] = []
        self.committed = False
        idempotency_value = (
            _FakeJobRow(job_id="existing") if new_id_already_exists else None
        )
        sibling_value = (
            existing_sibling.expires_at if existing_sibling else None
        )
        # The helper stops after the idempotency check if it hits a row,
        # so we never need the sibling response in that case; keeping
        # both seeded is harmless (extra responses are ignored).
        self._execute_queue = [
            _FakeResult(value=idempotency_value),
            _FakeResult(value=sibling_value),
        ]

    async def execute(self, stmt):
        if not self._execute_queue:
            return _FakeResult(value=None)
        return self._execute_queue.pop(0)

    def add(self, obj):
        self.added_rows.append(obj)

    async def commit(self):
        self.committed = True


def _upstream_response(body: dict, status: int = 200) -> Response:
    return Response(
        content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        status_code=status,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# overwrite branch
# ---------------------------------------------------------------------------


def test_overwrite_flips_status_and_bumps_edit_generation(monkeypatch) -> None:
    source = _FakeJobRow(job_id="job_src", status="editing", edit_generation=0)
    session = _FakeSession()
    resp = _upstream_response({
        "strategy": "overwrite",
        "job_id": "job_src",
        "edit_generation": 1,
    })
    now = datetime.now(timezone.utc)

    # Stub the materials_pack invalidation hook so this unit test stays
    # hermetic (the real helper runs a SELECT against BackgroundTask which
    # the fake session can't reproduce). The dedicated integration test
    # below verifies the wiring; this one focuses on status/generation.
    invalidate_calls: list[str] = []

    async def _fake_invalidate(db, *, job_id, **kwargs):
        invalidate_calls.append(job_id)
        return 0

    import background_task_queue as _bq
    monkeypatch.setattr(_bq, "invalidate_materials_pack_for_job", _fake_invalidate)

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=now,
        )
    )

    assert source.status == "running"
    assert source.current_stage == "alignment"
    assert source.edit_generation == 1
    assert source.editing_touched_at is None
    assert source.metering_snapshot["post_edit_usage"]["overwrite_commits"] == 1
    # No new row inserted for overwrite
    assert session.added_rows == []
    # And the materials_pack invalidation hook was called for THIS job
    assert invalidate_calls == ["job_src"]


def test_overwrite_second_commit_bumps_generation_to_2(monkeypatch) -> None:
    source = _FakeJobRow(job_id="job_src", status="editing", edit_generation=1)
    session = _FakeSession()
    resp = _upstream_response({"strategy": "overwrite", "job_id": "job_src"})

    async def _fake_invalidate(db, *, job_id, **kwargs):
        return 0

    import background_task_queue as _bq
    monkeypatch.setattr(_bq, "invalidate_materials_pack_for_job", _fake_invalidate)

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    assert source.edit_generation == 2
    assert source.metering_snapshot["post_edit_usage"]["overwrite_commits"] == 1


def test_overwrite_invokes_materials_pack_invalidation_with_now_utc(monkeypatch) -> None:
    """Wiring check: the invalidation hook is called with the source job_id
    and the same now_utc the rest of the handler is using, so the
    materials_pack rows' updated_at lines up with the source row's
    transition timestamp."""
    source = _FakeJobRow(job_id="job_src_invl", status="editing")
    session = _FakeSession()
    resp = _upstream_response({"strategy": "overwrite", "job_id": "job_src_invl"})
    fixed_now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
    captured: dict[str, Any] = {}

    async def _fake_invalidate(db, *, job_id, now=None, **kwargs):
        captured["db_is_session"] = db is session
        captured["job_id"] = job_id
        captured["now"] = now
        return 1  # pretend one stale pack got expired

    import background_task_queue as _bq
    monkeypatch.setattr(_bq, "invalidate_materials_pack_for_job", _fake_invalidate)

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=fixed_now,
        )
    )

    assert captured["db_is_session"] is True
    assert captured["job_id"] == "job_src_invl"
    assert captured["now"] == fixed_now


def test_copy_as_new_does_not_invalidate_materials_pack(monkeypatch) -> None:
    """copy_as_new keeps the source as the pre-edit baseline (Phase B
    Status=succeeded). Source's materials_pack still reflects pre-edit
    content, which IS the source job's content — so it stays valid.
    The new copy has a fresh job_id with no pack rows yet."""
    source = _FakeJobRow(
        job_id="job_src_copy",
        status="editing",
        editing_touched_at=datetime.now(timezone.utc),
        root_job_id="job_src_copy",
    )
    session = _FakeSession()
    resp = _upstream_response({
        "strategy": "copy_as_new",
        "source_job_id": "job_src_copy",
        "new_job_id": "job_copy_new",
        "new_project_dir": "/projects/job_copy_new",
        "new_display_name": "副本",
    })

    invalidate_calls: list[str] = []

    async def _fake_invalidate(db, *, job_id, **kwargs):
        invalidate_calls.append(job_id)
        return 0

    import background_task_queue as _bq
    monkeypatch.setattr(_bq, "invalidate_materials_pack_for_job", _fake_invalidate)

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    # copy_as_new must NOT invalidate source's pack — different rationale
    # than overwrite (source stays = pre-edit content = source's pack stays
    # valid for source).
    assert invalidate_calls == []


# ---------------------------------------------------------------------------
# copy_as_new branch — happy path
# ---------------------------------------------------------------------------


def test_copy_as_new_resets_source_and_inserts_new_row() -> None:
    source = _FakeJobRow(
        job_id="job_src",
        user_id="user_42",
        status="editing",
        editing_touched_at=datetime.now(timezone.utc),
        root_job_id="job_src",  # source's own root
        service_mode="studio",
        tts_provider="minimax",
        source_content_hash="hash_abc",
        title="Original Title",
    )
    session = _FakeSession()
    resp = _upstream_response({
        "strategy": "copy_as_new",
        "source_job_id": "job_src",
        "new_job_id": "job_copy_1",
        "new_project_dir": "/projects/job_copy_1",
        "new_display_name": "A · 副本 1",
    })
    now = datetime.now(timezone.utc)

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=now,
        )
    )

    # Source was reset (Phase B mirror)
    assert source.status == "succeeded"
    assert source.editing_touched_at is None
    assert source.metering_snapshot["post_edit_usage"]["copy_as_new"] == 1
    # One INSERT for the copy
    assert len(session.added_rows) == 1
    new_row = session.added_rows[0]
    assert new_row.job_id == "job_copy_1"
    assert new_row.status == "running"
    assert new_row.current_stage == "alignment"
    assert new_row.user_id == "user_42"  # inherited
    assert new_row.display_name == "A · 副本 1"
    assert new_row.copy_of_job_id == "job_src"
    assert new_row.root_job_id == "job_src"
    assert new_row.edit_generation == 0
    assert new_row.editing_touched_at is None
    assert new_row.project_dir == "/projects/job_copy_1"
    assert new_row.source_content_hash == "hash_abc"  # inherited
    assert new_row.service_mode == "studio"           # inherited
    assert new_row.tts_provider == "minimax"          # inherited
    # TTL first copy in lineage → now + 7d
    expected_expires = now + timedelta(days=7)
    assert abs((new_row.expires_at - expected_expires).total_seconds()) < 5


def test_copy_as_new_ttl_uses_prev_plus_24h_when_sibling_live() -> None:
    """Plan §5.1 simplified rule: new copy TTL = min(now+7d, prev+24h)."""
    now = datetime.now(timezone.utc)
    sibling = _FakeJobRow(
        job_id="job_copy_earlier",
        expires_at=now + timedelta(days=3),  # prev+24h = now+4d < now+7d
    )
    source = _FakeJobRow(
        job_id="job_src",
        user_id="u1",
        status="editing",
        root_job_id="job_src",
        editing_touched_at=now,
    )
    session = _FakeSession(existing_sibling=sibling)
    resp = _upstream_response({
        "strategy": "copy_as_new",
        "source_job_id": "job_src",
        "new_job_id": "job_copy_newest",
        "new_project_dir": "/projects/job_copy_newest",
        "new_display_name": "副本",
    })

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=now,
        )
    )

    new_row = session.added_rows[0]
    expected = sibling.expires_at + timedelta(hours=24)
    assert abs((new_row.expires_at - expected).total_seconds()) < 5


def test_copy_as_new_missing_new_job_id_only_resets_source() -> None:
    """Defensive: if Job-API somehow returns without new_job_id (should
    never happen), we still reset the source but don't insert a junk row."""
    source = _FakeJobRow(
        job_id="job_src",
        status="editing",
        editing_touched_at=datetime.now(timezone.utc),
    )
    session = _FakeSession()
    resp = _upstream_response({
        "strategy": "copy_as_new",
        "source_job_id": "job_src",
        # new_job_id missing
    })

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    assert source.status == "succeeded"
    assert source.editing_touched_at is None
    assert session.added_rows == []


def test_shadow_settle_skips_post_edit_overwrite_and_copies() -> None:
    original = _FakeJobRow(job_id="job_original", edit_generation=0)
    overwritten = _FakeJobRow(job_id="job_original", edit_generation=1)
    copied = _FakeJobRow(job_id="job_copy", copy_of_job_id="job_original")

    assert job_intercept._should_shadow_settle_job_credits(original) is True
    assert job_intercept._should_shadow_settle_job_credits(overwritten) is False
    assert job_intercept._should_shadow_settle_job_credits(copied) is False


def test_free_user_has_no_post_edit_limits() -> None:
    user = _FakeUser(plan_code="free")

    assert job_intercept._post_edit_limits_for_user(user) is None


def test_trial_cannot_copy_as_new() -> None:
    now = datetime.now(timezone.utc)
    user = _FakeUser(
        plan_code="free",
        trial_granted_at=now - timedelta(days=1),
        trial_ends_at=now + timedelta(days=1),
    )
    source = _FakeJobRow(job_id="job_src", status="editing", expires_at=now + timedelta(days=1))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            job_intercept._check_post_edit_commit_limit(
                _FakeSession(), source, user, strategy="copy_as_new", now_utc=now,
            )
        )

    assert exc.value.status_code == 403


@dataclass
class _FakeUser:
    plan_code: str = "free"
    role: str = "user"
    trial_granted_at: datetime | None = None
    trial_ends_at: datetime | None = None


def test_consume_post_edit_tts_usage_records_trial_allowance(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    source = _FakeJobRow(job_id="job_src", status="editing", expires_at=now + timedelta(days=1))
    user = _FakeUser(plan_code="free", trial_granted_at=now, trial_ends_at=now + timedelta(days=1))

    async def fake_enforce(db, job, passed_user, *, subpath: str, now_utc: datetime):
        assert db is session
        assert job is source
        assert passed_user is user
        assert subpath == "regenerate-tts"
        assert now_utc is now
        return source, job_intercept.POST_EDIT_LIMITS["trial"]

    session = _FakeSession()
    monkeypatch.setattr(job_intercept, "_enforce_post_edit_access", fake_enforce)

    asyncio.run(
        job_intercept._consume_post_edit_tts_usage(
            session,
            source,
            user,
            segments=2,
            chars=120,
            batch_start=True,
            now_utc=now,
        )
    )

    usage = source.metering_snapshot[job_intercept.POST_EDIT_USAGE_KEY]
    assert usage["batch_regenerates"] == 1
    assert usage["tts_segments"] == 2
    assert usage["tts_chars"] == 120


def test_consume_post_edit_tts_usage_accumulates_existing_usage(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    source = _FakeJobRow(
        job_id="job_src",
        status="editing",
        expires_at=now + timedelta(days=1),
        metering_snapshot={
            job_intercept.POST_EDIT_USAGE_KEY: {
                "tts_segments": 1,
                "tts_chars": 50,
            }
        },
    )
    user = _FakeUser(plan_code="free", trial_granted_at=now, trial_ends_at=now + timedelta(days=1))

    async def fake_enforce(db, job, passed_user, *, subpath: str, now_utc: datetime):
        return source, job_intercept.POST_EDIT_LIMITS["trial"]

    monkeypatch.setattr(job_intercept, "_enforce_post_edit_access", fake_enforce)

    asyncio.run(
        job_intercept._consume_post_edit_tts_usage(
            _FakeSession(),
            source,
            user,
            segments=2,
            chars=120,
            batch_start=False,
            now_utc=now,
        )
    )

    usage = source.metering_snapshot[job_intercept.POST_EDIT_USAGE_KEY]
    assert usage["tts_segments"] == 3
    assert usage["tts_chars"] == 170


def test_consume_post_edit_tts_usage_rejects_segment_limit_without_increment(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    source = _FakeJobRow(
        job_id="job_src",
        status="editing",
        expires_at=now + timedelta(days=1),
        metering_snapshot={
            job_intercept.POST_EDIT_USAGE_KEY: {
                "tts_segments": 7,
                "tts_chars": 100,
            }
        },
    )
    user = _FakeUser(plan_code="free", trial_granted_at=now, trial_ends_at=now + timedelta(days=1))

    async def fake_enforce(db, job, passed_user, *, subpath: str, now_utc: datetime):
        return source, job_intercept.POST_EDIT_LIMITS["trial"]

    monkeypatch.setattr(job_intercept, "_enforce_post_edit_access", fake_enforce)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            job_intercept._consume_post_edit_tts_usage(
                _FakeSession(),
                source,
                user,
                segments=2,
                chars=10,
                batch_start=False,
                now_utc=now,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "本项目免费重合成段数已用完。"
    usage = source.metering_snapshot[job_intercept.POST_EDIT_USAGE_KEY]
    assert usage["tts_segments"] == 7
    assert usage["tts_chars"] == 100


def test_consume_post_edit_tts_usage_rejects_char_limit_without_increment(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    source = _FakeJobRow(
        job_id="job_src",
        status="editing",
        expires_at=now + timedelta(days=1),
        metering_snapshot={
            job_intercept.POST_EDIT_USAGE_KEY: {
                "tts_segments": 2,
                "tts_chars": 995,
            }
        },
    )
    user = _FakeUser(plan_code="free", trial_granted_at=now, trial_ends_at=now + timedelta(days=1))

    async def fake_enforce(db, job, passed_user, *, subpath: str, now_utc: datetime):
        return source, job_intercept.POST_EDIT_LIMITS["trial"]

    monkeypatch.setattr(job_intercept, "_enforce_post_edit_access", fake_enforce)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            job_intercept._consume_post_edit_tts_usage(
                _FakeSession(),
                source,
                user,
                segments=1,
                chars=10,
                batch_start=False,
                now_utc=now,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "本项目免费重合成字数已用完。"
    usage = source.metering_snapshot[job_intercept.POST_EDIT_USAGE_KEY]
    assert usage["tts_segments"] == 2
    assert usage["tts_chars"] == 995


@pytest.mark.parametrize("plan_code", ["plus", "pro"])
def test_consume_post_edit_tts_usage_accepts_paid_plan_limits(
    monkeypatch,
    plan_code: str,
) -> None:
    now = datetime.now(timezone.utc)
    limits = job_intercept.POST_EDIT_LIMITS[plan_code]
    source = _FakeJobRow(
        job_id="job_src",
        status="editing",
        expires_at=now + timedelta(days=1),
        metering_snapshot={
            job_intercept.POST_EDIT_USAGE_KEY: {
                "batch_regenerates": int(limits["batch_regenerates"] or 0) - 1,
            }
        },
    )
    user = _FakeUser(plan_code=plan_code)

    async def fake_enforce(db, job, passed_user, *, subpath: str, now_utc: datetime):
        assert passed_user is user
        return source, limits

    monkeypatch.setattr(job_intercept, "_enforce_post_edit_access", fake_enforce)

    asyncio.run(
        job_intercept._consume_post_edit_tts_usage(
            _FakeSession(),
            source,
            user,
            segments=int(limits["tts_segments"] or 0),
            chars=int(limits["tts_chars"] or 0),
            batch_start=True,
            now_utc=now,
        )
    )

    usage = source.metering_snapshot[job_intercept.POST_EDIT_USAGE_KEY]
    assert usage["batch_regenerates"] == limits["batch_regenerates"]
    assert usage["tts_segments"] == limits["tts_segments"]
    assert usage["tts_chars"] == limits["tts_chars"]


def test_enter_edit_transition_passes_user_to_access_policy(monkeypatch) -> None:
    source = _FakeJobRow(job_id="job_src", status="succeeded")
    session = _FakeSession()
    session._execute_queue = [_FakeResult(value=source)]
    user = _FakeUser(plan_code="plus")
    seen: dict[str, Any] = {}

    async def fake_enforce_post_edit_access(
        db, job, passed_user, *, subpath: str, now_utc: datetime,
    ):
        seen["db"] = db
        seen["job"] = job
        seen["user"] = passed_user
        seen["subpath"] = subpath
        seen["now_utc"] = now_utc
        return job, {"overwrite_commits": 1}

    async def fake_proxy_request(**kwargs):
        seen["proxy_request"] = kwargs
        return _upstream_response({"ok": True})

    monkeypatch.setattr(
        job_intercept,
        "_enforce_post_edit_access",
        fake_enforce_post_edit_access,
    )
    monkeypatch.setattr(job_intercept, "proxy_request", fake_proxy_request)

    response = asyncio.run(
        job_intercept._editing_transition_with_lock(
            object(), "job_src", session, user, subpath="enter-edit",
        )
    )

    assert response.status_code == 200
    assert seen["user"] is user
    assert seen["subpath"] == "enter-edit"
    assert source.status == "editing"
    assert source.editing_touched_at is not None
    assert session.committed is True


def test_duplicate_overwrite_commit_after_start_is_idempotent(
    monkeypatch, tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    (project_dir / "editor").mkdir(parents=True)
    source = _FakeJobRow(
        job_id="job_src",
        status="running",
        current_stage="alignment",
        edit_generation=1,
        project_dir=str(project_dir),
        editing_touched_at=None,
    )
    session = _FakeSession()
    session._execute_queue = [_FakeResult(value=source)]

    class _Request:
        async def body(self):
            return b'{"strategy":"overwrite"}'

    async def _unexpected_proxy(**kwargs):
        raise AssertionError("duplicate overwrite commit should not proxy upstream")

    monkeypatch.setattr(job_intercept, "proxy_request", _unexpected_proxy)

    response = asyncio.run(
        job_intercept._editing_transition_with_lock(
            _Request(), "job_src", session, None, subpath="editing/commit",
        )
    )
    body = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert body["success"] is True
    assert body["already_started"] is True
    assert body["strategy"] == "overwrite"
    assert session.committed is True


def test_duplicate_overwrite_commit_after_finish_is_idempotent(
    monkeypatch, tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    (project_dir / "editor").mkdir(parents=True)
    source = _FakeJobRow(
        job_id="job_src",
        status="succeeded",
        current_stage="completed",
        edit_generation=1,
        project_dir=str(project_dir),
        editing_touched_at=None,
    )
    session = _FakeSession()
    session._execute_queue = [_FakeResult(value=source)]

    class _Request:
        async def body(self):
            return b'{"strategy":"overwrite"}'

    async def _unexpected_proxy(**kwargs):
        raise AssertionError("completed duplicate overwrite should not proxy upstream")

    monkeypatch.setattr(job_intercept, "proxy_request", _unexpected_proxy)

    response = asyncio.run(
        job_intercept._editing_transition_with_lock(
            _Request(), "job_src", session, None, subpath="editing/commit",
        )
    )
    body = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert body["success"] is True
    assert body["already_started"] is True
    assert body["already_completed"] is True
    assert body["strategy"] == "overwrite"
    assert session.committed is True


def test_copy_as_new_idempotent_on_duplicate_new_job_id() -> None:
    """If the same commit request retries and Job-API creates the record
    but the gateway INSERT never fired last time, a second attempt should
    not duplicate the row."""
    source = _FakeJobRow(job_id="job_src", user_id="u1", status="editing", root_job_id="job_src")
    session = _FakeSession(new_id_already_exists=True)
    resp = _upstream_response({
        "strategy": "copy_as_new",
        "source_job_id": "job_src",
        "new_job_id": "job_copy_1",
        "new_project_dir": "/p",
        "new_display_name": "C",
    })

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    # Source still reset
    assert source.status == "succeeded"
    # But no duplicate INSERT
    assert session.added_rows == []


def test_unknown_strategy_is_no_op() -> None:
    source = _FakeJobRow(job_id="job_src", status="editing")
    initial_status = source.status
    session = _FakeSession()
    resp = _upstream_response({"strategy": "rebase", "job_id": "job_src"})

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    assert source.status == initial_status
    assert session.added_rows == []


def test_non_json_body_is_no_op() -> None:
    source = _FakeJobRow(job_id="job_src", status="editing")
    session = _FakeSession()
    resp = Response(
        content=b"not json",
        status_code=200,
        media_type="text/plain",
    )

    asyncio.run(
        job_intercept._apply_editing_commit_gateway_side(
            session, source, resp, now_utc=datetime.now(timezone.utc),
        )
    )

    # Source untouched, no row inserted — fail open
    assert source.status == "editing"
    assert session.added_rows == []


# ---------------------------------------------------------------------------
# main.run_job_api_command wires the idle cancel callback
# ---------------------------------------------------------------------------


def test_run_job_api_command_wires_idle_cancel_callback_source() -> None:
    """Static guard: run_job_api_command's body must apply runtime wiring.
    CodeX P1-2 regression risk: without wiring, the idle scanner stays on
    ``_noop_cancel`` and the cleanup thread never starts.

    After the 2026-04-19 runtime_wiring refactor, the three concrete
    inject calls (inject_editing_cancel_callback / segment TTS caller /
    start_cleanup_thread) live inside ``apply_runtime_wiring`` and are
    reached from both entry points (main.py + scripts/). We still pin
    them down here — one check on the entry body (must call the helper),
    one check on the helper (must call each inject)."""
    repo_root = Path(__file__).resolve().parents[1]
    main_src = (repo_root / "main.py").read_text(encoding="utf-8")
    func_match = re.search(
        r"def run_job_api_command\([^)]*\)\s*->\s*None:(.*?)(?=\ndef |\Z)",
        main_src,
        re.DOTALL,
    )
    assert func_match, "run_job_api_command not found in main.py"
    body = func_match.group(1)
    assert "apply_runtime_wiring" in body, (
        "run_job_api_command must delegate post-build wiring to "
        "apply_runtime_wiring(service) so main.py and the container "
        "entry (scripts/run_remote_workbench_service.py) stay in lock-step"
    )

    # And the helper itself must call every inject step.
    helper_src = (repo_root / "src" / "services" / "jobs" / "runtime_wiring.py").read_text(encoding="utf-8")
    for needle, purpose in (
        ("inject_editing_cancel_callback", "idle-cancel callback (T1-10)"),
        ("build_real_segment_tts_caller",  "segment TTS caller (A.2)"),
        ("start_cleanup_thread",           "cleanup background thread"),
    ):
        assert needle in helper_src, (
            f"runtime_wiring.apply_runtime_wiring missing {needle} call — "
            f"{purpose} would silently regress"
        )
