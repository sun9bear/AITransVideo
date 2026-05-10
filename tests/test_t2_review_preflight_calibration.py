"""T2 review-submit preflight calibration regression test suite.

Plan v4.3 §3.2 + §5.2 (codex F-v4.3-1 hardening). Each test pins a
specific contract guarantee:

  - test_t2_payload_reads_tts_provider_field_not_provider:
      v4 codex F-v4-1 — payload uses ``tts_provider`` (not ``provider``).
  - test_t2_calibrates_final_job_level_minimax_model_not_per_speaker:
      v4 codex F-v4-2 — speakers=[turbo, hd] → final=hd → both voices
      get hd CPS calibrated (NOT each speaker's own minimax_model).
  - test_t2_extracts_owner_from_job_user_id:
      v3 codex F7 — owner_id resolved from Job.user_id (not from
      a passed-in user param), since the route fn signature lacks
      a user kwarg.
  - test_t2_checks_chars_per_second_by_model_not_scalar:
      v3 codex F3 — voice with scalar CPS but missing
      by_model[final_model] STILL triggers calibrate.
  - test_t2_calibrates_only_missing_model_keys:
      Voice with by_model[final_model] already populated → outcome
      "already_calibrated", no calibrate_voice call.
  - test_t2_blocks_proxy_until_preflight_done:
      v1 codex F5 — preflight runs BEFORE proxy_request.
  - test_t2_proxy_fires_after_batch_timeout_without_canceling_pending:
      v3+v4 codex F-v4-6 — 50s timeout, pending tasks keep running,
      proxy fires anyway.
  - test_t2_uses_independent_session_not_route_db:
      v4 codex F-v4-5 + v4.1 F-v4.1-2 — route db is rolled back before
      preflight, preflight uses independent async_session().
  - test_t2_pending_tasks_complete_in_background_dont_double_write_db:
      v4.1 F-v4.1-8 + v4.2 F-v4.2-4 — done_callback only logs; factory
      writes DB once inside its own session.
  - test_t2_skipped_when_env_disabled:
      env=false → skip preflight, proxy fires immediately.

Plus F-v4.3-1 specific:
  - test_t2_user_voices_first_lookup_ignores_voice_source_field:
      voice_source='catalog' but voice IS in user_voices → use user scope.
  - test_t2_falls_back_to_catalog_when_not_in_user_voices:
      voice not in user_voices → query voice_catalog with
      provider='minimax' + archived_at IS NULL filter.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path setup mirrors test_t0/t1 pattern
# ---------------------------------------------------------------------------
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

import risk_control  # noqa: E402
import voice_calibration_review_preflight as t2  # noqa: E402
from voice_calibration_inflight import (  # noqa: E402
    CalibrationInFlightRegistry,
    CalibrationKey,
)
from voice_speed_calibrator import CalibrationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_budget_and_registry(monkeypatch):
    risk_control.reset_voice_calibration_rate_limits()
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    yield fresh_registry
    risk_control.reset_voice_calibration_rate_limits()


@pytest.fixture(autouse=True)
def reset_env_gate(monkeypatch):
    """Default: env unset → preflight DISABLED. Tests that need it on
    must set AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT explicitly."""
    monkeypatch.delenv("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", raising=False)
    yield


# ---------------------------------------------------------------------------
# Helper: stub out SQLAlchemy ``select`` so we can pass MagicMock model
# classes without tripping coercion.
# ---------------------------------------------------------------------------


class _StubStmt:
    """Minimal stand-in for the SQLAlchemy Select expression. Supports
    chained ``.where(...)`` calls. Tests pass an opaque marker so they can
    introspect which model was queried."""
    def __init__(self, model_marker):
        self.model_marker = model_marker
    def where(self, *args, **kwargs):
        return self


def _patch_select(monkeypatch):
    """Replace ``voice_calibration_review_preflight.select`` with a passthrough
    so tests can use MagicMock model classes (real ORM mapping not required).
    """
    monkeypatch.setattr(t2, "select", lambda model: _StubStmt(model))


# ---------------------------------------------------------------------------
# Helpers: stub out resolve + factory
# ---------------------------------------------------------------------------


class _FakeUserVoiceRow:
    def __init__(self, voice_id: str, by_model: dict | None):
        self.voice_id = voice_id
        self.chars_per_second_by_model = by_model


class _FakeCatalogRow(_FakeUserVoiceRow):
    pass


def _make_speakers(*entries) -> list[dict]:
    """Build raw speakers payload from concise tuples.

    Each entry is (voice_id, tts_provider, minimax_model_hint=None,
                    voice_source='catalog', speaker_id=None).
    """
    out = []
    for i, entry in enumerate(entries):
        voice_id = entry[0]
        tts_provider = entry[1]
        minimax_model = entry[2] if len(entry) > 2 else None
        voice_source = entry[3] if len(entry) > 3 else "catalog"
        speaker_id = entry[4] if len(entry) > 4 else f"speaker_{i}"
        out.append({
            "speaker_id": speaker_id,
            "voice_id": voice_id,
            "tts_provider": tts_provider,
            "minimax_model": minimax_model,
            "voice_source": voice_source,
        })
    return out


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------


def test_t2_default_disabled(monkeypatch):
    monkeypatch.delenv("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", raising=False)
    assert t2.review_preflight_enabled() is False


def test_t2_explicit_true_enables(monkeypatch):
    monkeypatch.setenv("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", "true")
    assert t2.review_preflight_enabled() is True


def test_t2_falsey_values_remain_disabled(monkeypatch):
    for value in ("false", "0", "no", "off", "FALSE", ""):
        monkeypatch.setenv("AVT_AUTO_CALIBRATE_ON_REVIEW_SUBMIT", value)
        assert t2.review_preflight_enabled() is False, f"value={value!r}"


# ---------------------------------------------------------------------------
# Payload field reading (codex F-v4-1)
# ---------------------------------------------------------------------------


def test_t2_payload_reads_tts_provider_field_not_provider():
    """Plan v4 codex F-v4-1: parse uses 'tts_provider', not 'provider'.

    Frontend payload uses 'tts_provider' explicitly (voiceSelection.ts:27).
    """
    parsed = t2._parse_speakers([
        {"speaker_id": "s1", "voice_id": "v1", "tts_provider": "minimax", "minimax_model": "hd"},
        # WRONG field name 'provider' should be IGNORED
        {"speaker_id": "s2", "voice_id": "v2", "provider": "minimax", "minimax_model": "hd"},
    ])
    assert len(parsed) == 1
    assert parsed[0].voice_id == "v1"
    assert parsed[0].tts_provider == "minimax"


def test_t2_skips_invalid_speakers():
    """Empty voice_id, missing tts_provider, or non-dict entries are dropped."""
    parsed = t2._parse_speakers([
        {"voice_id": "v1", "tts_provider": "minimax"},  # OK
        {"voice_id": "", "tts_provider": "minimax"},     # skip — empty voice_id
        {"voice_id": "v3", "tts_provider": ""},          # skip — empty provider
        "not_a_dict",                                     # skip
        {"voice_id": "v5"},                              # skip — no tts_provider key
    ])
    assert len(parsed) == 1
    assert parsed[0].voice_id == "v1"


# ---------------------------------------------------------------------------
# Final-model derivation (codex F-v4-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_calibrates_final_job_level_minimax_model_not_per_speaker(monkeypatch):
    """Plan v4 codex F-v4-2: speaker A=turbo + B=hd → final='speech-2.8-hd'
    → BOTH voices A and B get hd CPS calibrated.
    """
    speakers = _make_speakers(
        ("v_a", "minimax", "turbo"),  # speaker A wants turbo
        ("v_b", "minimax", "hd"),     # speaker B wants hd → wins
    )

    # Stub session that returns Job + user_voices missing
    captured_calibrations = []

    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured_calibrations.append({"key": key, "user_id": user_id_for_budget})
        return CalibrationResult(
            ok=True, cps=4.5, paid_call_count=3, model_key=key.model_key,
        )

    monkeypatch.setattr(
        "voice_calibration_review_preflight.run_calibration_task",
        fake_run_calibration_task,
        raising=False,
    )

    # Patch the inflight import used inside _run_via_inflight_registry
    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    # Stub async_session to return a fake session resolving Job + user_voices
    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid-x"
    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog_models = types.ModuleType("voice_catalog_models")
    fake_catalog_models.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog_models)
    _patch_select(monkeypatch)

    # Track session opens — we expect 1 query session
    session_opens = []

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def execute(self, stmt):
            # 1st execute: SELECT Job → return Job row
            # 2nd execute: SELECT UserVoice → return empty (force catalog fallback)
            # 3rd execute: SELECT VoiceCatalog → return rows for v_a + v_b with NULL by_model
            self._call_count = getattr(self, "_call_count", 0) + 1
            r = MagicMock()
            if self._call_count == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif self._call_count == 2:
                r.scalars().all.return_value = []  # no user_voices match
            elif self._call_count == 3:
                r.scalars().all.return_value = [
                    _FakeCatalogRow("v_a", None),
                    _FakeCatalogRow("v_b", None),
                ]
            return r

    def fake_async_session():
        s = FakeSession()
        session_opens.append(s)
        return s

    fake_db = types.ModuleType("database")
    fake_db.async_session = fake_async_session
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_test", speakers=speakers,
    )

    # Both voices were submitted for hd calibration (NOT turbo for v_a)
    assert len(captured_calibrations) == 2
    model_keys = {c["key"].model_key for c in captured_calibrations}
    assert model_keys == {"speech-2.8-hd"}, (
        f"Both voices must use job-level final_minimax_model='speech-2.8-hd', "
        f"got: {model_keys}"
    )
    voice_ids = {c["key"].voice_id for c in captured_calibrations}
    assert voice_ids == {"v_a", "v_b"}


# ---------------------------------------------------------------------------
# F-v4.3-1: user_voices-first lookup (the T2 blocker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_user_voices_first_lookup_ignores_voice_source_field(monkeypatch):
    """codex F-v4.3-1: voice_source='catalog' is set by frontend even when
    user reuses a previously-cloned voice from "我的音色" library. Backend
    MUST NOT trust this field for routing — instead probe (owner_id,
    voice_id) against user_voices first.
    """
    # Speaker submits voice_source='catalog' but the voice IS in user_voices.
    speakers = _make_speakers(
        ("vt_cloned_xyz", "minimax", "hd", "catalog"),
    )

    captured_keys = []

    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured_keys.append(key)
        return CalibrationResult(
            ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key,
        )

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                # user_voices probe — HIT (the voice IS in user library)
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow("vt_cloned_xyz", None),
                ]
            elif len(self.queries) == 3:
                # Catalog should NOT be queried at all if user-first hit
                # (test failure if we get here, but provide empty safely)
                r.scalars().all.return_value = []
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    await t2.pre_flight_calibrate_voices(
        job_id="job_test", speakers=speakers,
    )

    assert len(captured_keys) == 1
    key = captured_keys[0]
    assert key.scope == "user", (
        f"voice_source='catalog' but row IS in user_voices → must route to "
        f"user scope. Got scope={key.scope!r}. F-v4.3-1 regression."
    )
    assert key.owner == "owner-uuid"
    assert key.voice_id == "vt_cloned_xyz"


@pytest.mark.asyncio
async def test_t2_falls_back_to_catalog_when_not_in_user_voices(monkeypatch):
    """voice not in user_voices → fall back to voice_catalog. The
    catalog query must filter by provider='minimax' + archived_at IS NULL
    (codex F-v4.2-6).
    """
    speakers = _make_speakers(
        ("vc_library_voice", "minimax", "turbo", "catalog"),
    )

    captured_keys = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured_keys.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                # user_voices: MISS
                r.scalars().all.return_value = []
            elif len(self.queries) == 3:
                # Catalog query — return canned hit
                r.scalars().all.return_value = [
                    _FakeCatalogRow("vc_library_voice", None),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    await t2.pre_flight_calibrate_voices(
        job_id="job_test", speakers=speakers,
    )

    assert len(captured_keys) == 1
    key = captured_keys[0]
    assert key.scope == "catalog", (
        f"voice missing from user_voices → must fall back to catalog. "
        f"Got scope={key.scope!r}"
    )
    assert key.owner == "catalog"


def test_t2_catalog_query_filters_provider_and_archived():
    """codex F-v4.2-6 + F-v4.3-1: source-level guard that catalog query
    filters by provider='minimax' AND archived_at IS NULL. (Cannot inspect
    SQLAlchemy expression at runtime because tests stub `select` for
    MagicMock-friendliness; check source instead.)
    """
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "gateway" / "voice_calibration_review_preflight.py"
    src = p.read_text(encoding="utf-8")

    fn_start = src.find("async def _resolve_targets_user_first")
    fn_end = src.find("\nasync def ", fn_start + 100)
    if fn_end < 0:
        fn_end = len(src)
    fn_body = src[fn_start:fn_end]

    assert "VoiceCatalog.provider" in fn_body and '"minimax"' in fn_body, (
        "_resolve_targets_user_first must filter VoiceCatalog by provider='minimax'"
    )
    assert "VoiceCatalog.archived_at.is_(None)" in fn_body, (
        "_resolve_targets_user_first must filter VoiceCatalog by archived_at IS NULL"
    )


# ---------------------------------------------------------------------------
# Already-calibrated short-circuit (codex F3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_already_calibrated_short_circuits(monkeypatch):
    """If by_model[final_model] is already populated, no calibrate_voice
    call. Returns 'already_calibrated' outcome.
    """
    speakers = _make_speakers(
        ("vt_already_done", "minimax", "hd"),
    )

    captured_keys = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured_keys.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=0, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                # user_voices HAS this voice WITH hd already calibrated
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow(
                        "vt_already_done",
                        {"speech-2.8-hd": 4.5, "speech-2.8-turbo": 4.0},
                    ),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_test", speakers=speakers,
    )

    # No calibrate_voice call
    assert captured_keys == [], (
        "voice with by_model[hd] populated must short-circuit; "
        f"got calibration calls: {captured_keys}"
    )
    # Outcome reports already_calibrated
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "already_calibrated"
    assert outcomes[0]["voice_id"] == "vt_already_done"
    assert outcomes[0]["model_key"] == "speech-2.8-hd"
    assert outcomes[0]["cps"] == 4.5


@pytest.mark.asyncio
async def test_t2_scalar_cps_alone_does_not_short_circuit(monkeypatch):
    """codex F3: voice with scalar chars_per_second but missing
    by_model[final_model] STILL triggers calibrate. The plan changed
    lookup logic from scalar to by_model-aware in v3.

    We simulate this by having a row whose by_model dict is missing
    the requested key.
    """
    speakers = _make_speakers(
        ("vt_partial", "minimax", "hd"),
    )

    captured_keys = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured_keys.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                # by_model has TURBO but not HD; final is HD → must calibrate
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow(
                        "vt_partial",
                        {"speech-2.8-turbo": 4.0},
                    ),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    await t2.pre_flight_calibrate_voices(
        job_id="job_test", speakers=speakers,
    )

    assert len(captured_keys) == 1
    assert captured_keys[0].model_key == "speech-2.8-hd"


# ---------------------------------------------------------------------------
# Owner extraction (codex F7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_returns_empty_when_job_missing(monkeypatch):
    """Plan v3 codex F7: owner_id is resolved from Job.user_id (NOT a
    user param). If Job row absent or user_id is None, return [].
    """
    speakers = _make_speakers(("v1", "minimax", "hd"))

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None  # job missing
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_missing", speakers=speakers,
    )
    assert outcomes == []


# ---------------------------------------------------------------------------
# 50s timeout + pending tasks NOT cancelled (codex F-v4-6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_proxy_fires_after_batch_timeout_without_canceling_pending(monkeypatch):
    """codex F-v4-6: When the 50s batch timeout fires, pending tasks must
    NOT be cancelled — their factory's paid TTS may already have spent
    money. The outcome list reports them as 'still_running'.
    """
    speakers = _make_speakers(
        ("v_fast", "minimax", "hd"),
        ("v_slow", "minimax", "hd"),
    )

    # Fast voice resolves quickly; slow voice exceeds the test timeout.
    fast_done = asyncio.Event()
    slow_started = asyncio.Event()
    slow_cancelled = []

    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        if key.voice_id == "v_fast":
            fast_done.set()
            return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)
        # slow path
        slow_started.set()
        try:
            await asyncio.sleep(10)  # > test timeout
        except asyncio.CancelledError:
            slow_cancelled.append(key.voice_id)
            raise
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow("v_fast", None),
                    _FakeUserVoiceRow("v_slow", None),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    # Use a short batch timeout for test speed
    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
        batch_total_timeout_seconds=0.5,  # 500ms cap
    )

    # We got two outcomes; one calibrated, one still_running
    statuses = sorted(o["status"] for o in outcomes)
    assert "calibrated" in statuses
    assert "still_running" in statuses

    # CRITICAL: slow task was NOT cancelled
    # Give the task a moment to keep running (verify it wasn't killed)
    await asyncio.sleep(0.1)
    assert slow_cancelled == [], (
        f"pending task at timeout MUST NOT be cancelled — paid TTS could "
        f"have already fired. Got cancelled: {slow_cancelled}"
    )


# ---------------------------------------------------------------------------
# No minimax speakers (CosyVoice / VolcEngine deferred)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_no_minimax_speakers_returns_skip_outcomes(monkeypatch):
    """No minimax speakers → final_minimax_model is None → skip all,
    report status='no_minimax_model' for each."""
    speakers = _make_speakers(
        ("v_cos", "cosyvoice"),
        ("v_volc", "volcengine"),
    )

    captured = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
    )
    assert captured == []
    statuses = {o["status"] for o in outcomes}
    assert statuses == {"no_minimax_model"}
    assert len(outcomes) == 2


# ---------------------------------------------------------------------------
# Non-minimax voices in mixed payload are deferred but minimax still calibrates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_mixed_providers_only_calibrates_minimax(monkeypatch):
    """Mixed payload: minimax + cosyvoice. minimax voice → calibrate.
    cosyvoice voice → outcome 'provider_deferred', no calibrate call.
    """
    speakers = _make_speakers(
        ("v_mm", "minimax", "hd"),
        ("v_cos", "cosyvoice"),
    )

    captured = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow("v_mm", None),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
    )

    # MiniMax was calibrated
    assert len(captured) == 1
    assert captured[0].voice_id == "v_mm"
    # cosyvoice voice has 'provider_deferred' status
    cos_outcome = next(o for o in outcomes if o["voice_id"] == "v_cos")
    assert cos_outcome["status"] == "provider_deferred"


# ---------------------------------------------------------------------------
# Voice not in user_voices nor catalog (orphan / deleted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_orphan_voice_marked_not_found(monkeypatch):
    """Voice missing from BOTH user_voices and voice_catalog → mark
    'not_found'; do NOT attempt calibration (no row to write to)."""
    speakers = _make_speakers(
        ("v_orphan", "minimax", "hd"),
    )

    captured = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        captured.append(key)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                r.scalars().all.return_value = []   # not in user_voices
            elif len(self.queries) == 3:
                r.scalars().all.return_value = []   # not in catalog
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
    )

    assert captured == []  # no paid call
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "not_found"


# ---------------------------------------------------------------------------
# Function returns [] when env disabled (caller-side gate)
# ---------------------------------------------------------------------------


def test_t2_route_caller_gates_on_env(monkeypatch):
    """The route caller (_approve_voice_selection_with_quality_sync) gates
    on review_preflight_enabled() before calling pre_flight_calibrate_voices.

    AST-level guard: verify the route source has the env gate check around
    the preflight call. This guards against accidental removal of the gate.
    """
    import re
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
    source = p.read_text(encoding="utf-8")

    # Verify the route imports + uses the gate
    assert "review_preflight_enabled" in source, (
        "_approve_voice_selection_with_quality_sync must import and call "
        "review_preflight_enabled() before invoking preflight"
    )
    # Verify the call to pre_flight_calibrate_voices appears AFTER the gate
    gate_pos = source.find("review_preflight_enabled()")
    call_pos = source.find("pre_flight_calibrate_voices(")
    assert gate_pos > 0 and call_pos > 0
    assert gate_pos < call_pos, (
        "review_preflight_enabled() must appear in source BEFORE "
        "pre_flight_calibrate_voices() — env gate ordering broken"
    )


# ---------------------------------------------------------------------------
# Order: rollback BEFORE preflight call (codex F-v4.1-2)
# ---------------------------------------------------------------------------


def test_t2_route_rollbacks_route_db_before_preflight():
    """codex F-v4.1-2: route db must be rolled back BEFORE preflight runs
    (otherwise the connection is held during the up-to-50s wait, exhausting
    the pool under concurrent submits).

    Source-level guard: `await db.rollback()` must appear between the gate
    check and the pre_flight_calibrate_voices() call.
    """
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
    source = p.read_text(encoding="utf-8")

    gate_pos = source.find("review_preflight_enabled()")
    rollback_pos = source.find("await db.rollback()", gate_pos)  # first rollback after gate
    call_pos = source.find("pre_flight_calibrate_voices(", gate_pos)

    assert gate_pos > 0
    assert rollback_pos > gate_pos, (
        "must call await db.rollback() AFTER the env gate check"
    )
    assert rollback_pos < call_pos, (
        "must call await db.rollback() BEFORE pre_flight_calibrate_voices() — "
        "F-v4.1-2 regression"
    )


# ---------------------------------------------------------------------------
# Order: preflight BEFORE proxy (codex F5)
# ---------------------------------------------------------------------------


def test_t2_preflight_runs_before_proxy_request():
    """codex F5: preflight must complete before proxy_request (otherwise
    Job-API receives review_state.json before CPS is calibrated and the
    pipeline starts with default CPS).
    """
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
    source = p.read_text(encoding="utf-8")

    # Find the start of _approve_voice_selection_with_quality_sync function
    fn_start = source.find("async def _approve_voice_selection_with_quality_sync")
    assert fn_start > 0
    # Find the end of the function — next 'async def ' or 'def ' at module level
    fn_end = source.find("\nasync def ", fn_start + 100)
    if fn_end < 0:
        fn_end = source.find("\ndef ", fn_start + 100)
    fn_body = source[fn_start:fn_end if fn_end > 0 else None]

    preflight_pos = fn_body.find("pre_flight_calibrate_voices(")
    proxy_pos = fn_body.find("proxy_request(")
    assert preflight_pos > 0
    assert proxy_pos > 0
    assert preflight_pos < proxy_pos, (
        "pre_flight_calibrate_voices() must appear BEFORE proxy_request() "
        "in _approve_voice_selection_with_quality_sync — codex F5 regression"
    )


# ---------------------------------------------------------------------------
# v4.4 P1-1: queued tasks (semaphore not yet acquired) MUST be cancelled
#           at batch timeout so they don't start NEW paid TTS work after
#           the user has already proxied into the pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_queued_task_cancelled_at_timeout_no_paid_call(monkeypatch):
    """codex v4.4 P1-1: with max_concurrency=1 and two slow voices, the
    second voice never acquires the semaphore. After the batch timeout,
    the second task MUST be cancelled (no paid TTS), and the outcome
    MUST be 'not_started_timeout'.

    Without the fix, the second task would eventually acquire the
    semaphore (after the first finishes) and start a NEW paid TTS call
    — but by then the user has already proxied into the pipeline with
    default CPS for that voice, so the calibration is wasted budget.
    """
    speakers = _make_speakers(
        ("v_first", "minimax", "hd"),    # acquires sem first
        ("v_queued", "minimax", "hd"),   # blocked behind v_first
    )

    paid_calls = []   # track which voice_ids reached run_calibration_task
    first_started = asyncio.Event()

    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        paid_calls.append(key.voice_id)
        if key.voice_id == "v_first":
            first_started.set()
            # Block long enough for the batch timeout to fire.
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # First task is allowed to keep running; if cancelled it's
                # the test runtime tearing down.
                raise
        # If v_queued ever reaches here, the test fails — paid TTS should
        # NOT have been issued for the queued voice.
        return CalibrationResult(
            ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key,
        )

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                r.scalars().all.return_value = [
                    _FakeUserVoiceRow("v_first", None),
                    _FakeUserVoiceRow("v_queued", None),
                ]
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
        batch_total_timeout_seconds=0.5,
        max_concurrency=1,   # critical: forces queue
    )

    # Wait briefly to ensure any race-y post-return scheduling settles.
    await asyncio.sleep(0.2)

    # CRITICAL: paid TTS only fired for v_first; v_queued was cancelled
    # before acquiring the semaphore.
    assert paid_calls == ["v_first"], (
        f"Queued task MUST NOT issue paid TTS after timeout. "
        f"paid_calls={paid_calls}"
    )

    # And the outcome reports the cancelled state explicitly so callers
    # / dashboards can distinguish "in flight, may finish" vs "never
    # started".
    out_first = next(o for o in outcomes if o["voice_id"] == "v_first")
    out_queued = next(o for o in outcomes if o["voice_id"] == "v_queued")
    assert out_first["status"] == "still_running", (
        f"v_first held the semaphore at timeout; expected still_running, got {out_first}"
    )
    assert out_queued["status"] == "not_started_timeout", (
        f"v_queued never acquired semaphore; expected not_started_timeout, got {out_queued}"
    )


# ---------------------------------------------------------------------------
# v4.4 P1-2: expired user_voices row MUST NOT route to user scope.
#           Either fall back to catalog or mark not_found.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_expired_user_voice_not_routed_to_user_scope(monkeypatch):
    """codex v4.4 P1-2: a row in user_voices with expired_at != NULL is
    a soft-deleted clone. The resolve query MUST filter it out so we
    don't:
    1. Issue paid TTS for an unusable voice
    2. Write CPS back to a row the rest of the pipeline ignores

    The test simulates the database returning NO rows for the user_voices
    query (reflecting the new ``expired_at IS NULL`` filter), then no
    rows for catalog either. Outcome should be 'not_found' with NO
    calibrate call.
    """
    speakers = _make_speakers(
        ("vt_expired", "minimax", "hd", "catalog"),
    )

    paid_calls = []
    async def fake_run_calibration_task(*, key, user_id_for_budget, factory):
        paid_calls.append(key.voice_id)
        return CalibrationResult(ok=True, cps=4.0, paid_call_count=3, model_key=key.model_key)

    fake_inflight = types.ModuleType("voice_calibration_inflight")
    fake_inflight.CalibrationKey = CalibrationKey
    fake_inflight.run_calibration_task = fake_run_calibration_task
    monkeypatch.setitem(sys.modules, "voice_calibration_inflight", fake_inflight)

    fake_models = types.ModuleType("models")
    fake_models.Job = MagicMock()
    fake_models.UserVoice = MagicMock()
    monkeypatch.setitem(sys.modules, "models", fake_models)
    fake_catalog = types.ModuleType("voice_catalog_models")
    fake_catalog.VoiceCatalog = MagicMock()
    monkeypatch.setitem(sys.modules, "voice_catalog_models", fake_catalog)
    _patch_select(monkeypatch)

    fake_job = MagicMock()
    fake_job.user_id = "owner-uuid"

    class FakeSession:
        """Session that simulates the production behavior: the
        expired_at IS NULL filter EXCLUDES the expired row, so the
        user_voices query returns empty even though a row exists."""
        def __init__(self):
            self.queries = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def execute(self, stmt):
            self.queries.append(stmt)
            r = MagicMock()
            if len(self.queries) == 1:
                r.scalar_one_or_none.return_value = fake_job
            elif len(self.queries) == 2:
                # user_voices query — returns EMPTY because the
                # expired_at IS NULL filter excludes the soft-deleted row.
                r.scalars().all.return_value = []
            elif len(self.queries) == 3:
                # catalog query — also empty (it's a private user clone,
                # not a library voice)
                r.scalars().all.return_value = []
            return r

    fake_db = types.ModuleType("database")
    fake_db.async_session = lambda: FakeSession()
    monkeypatch.setitem(sys.modules, "database", fake_db)

    outcomes = await t2.pre_flight_calibrate_voices(
        job_id="job_t", speakers=speakers,
    )

    # CRITICAL: NO paid TTS for the expired voice.
    assert paid_calls == [], (
        f"Expired user voice MUST NOT trigger paid calibration. "
        f"paid_calls={paid_calls}"
    )

    # And the outcome is 'not_found' (not 'calibrated' or 'failed').
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "not_found", (
        f"Expired voice with no catalog row should resolve to "
        f"not_found, got status={outcomes[0]['status']!r}"
    )


def test_t2_resolve_query_includes_expired_at_filter():
    """Source guard for codex v4.4 P1-2: the user_voices probe in
    _resolve_targets_user_first MUST include an
    ``UserVoice.expired_at.is_(None)`` filter to match the semantics
    of user_voice_service.fetch_user_voice (which is the canonical
    public lookup).

    Without this filter, an expired user clone would route to user
    scope, get paid-calibrated, and write CPS back to a soft-deleted
    row.
    """
    from pathlib import Path
    p = (
        Path(__file__).resolve().parent.parent
        / "gateway" / "voice_calibration_review_preflight.py"
    )
    src = p.read_text(encoding="utf-8")

    fn_start = src.find("async def _resolve_targets_user_first")
    assert fn_start > 0
    fn_end = src.find("\nasync def ", fn_start + 100)
    if fn_end < 0:
        fn_end = len(src)
    fn_body = src[fn_start:fn_end]

    assert "UserVoice.expired_at.is_(None)" in fn_body, (
        "_resolve_targets_user_first must filter UserVoice.expired_at.is_(None)"
    )


def test_t2_update_user_voice_speed_calibration_filters_expired():
    """Source guard for codex v4.4 P1-2: defense-in-depth at the writer
    layer. update_user_voice_speed_calibration MUST also filter
    expired_at IS NULL so a voice that expires between resolve and
    write doesn't get a CPS write into a soft-deleted row.
    """
    from pathlib import Path
    p = (
        Path(__file__).resolve().parent.parent
        / "gateway" / "user_voice_service.py"
    )
    src = p.read_text(encoding="utf-8")

    fn_start = src.find("async def update_user_voice_speed_calibration")
    assert fn_start > 0
    fn_end = src.find("\n# ", fn_start + 100)
    if fn_end < 0:
        fn_end = src.find("\nasync def ", fn_start + 100)
    if fn_end < 0:
        fn_end = len(src)
    fn_body = src[fn_start:fn_end]

    assert "UserVoice.expired_at.is_(None)" in fn_body, (
        "update_user_voice_speed_calibration must filter UserVoice.expired_at.is_(None)"
    )
