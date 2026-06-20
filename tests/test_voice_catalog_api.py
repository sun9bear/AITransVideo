"""Tests for voice_catalog_api — route-level + model + serialization.

Uses FastAPI TestClient with mocked DB session (same pattern as
test_gateway_upload_video.py) to test real HTTP routes, not just helpers.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Stub database before importing gateway modules
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from voice_catalog_api import router as voice_catalog_router, internal_router as voice_catalog_internal_router, _serialize_voice
from voice_catalog_models import VoiceCatalog, VoiceLabel
from voice_catalog_service import parse_import_lines


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_voice(
    voice_id: str = "zh_female_vv_uranus_bigtts",
    provider: str = "volcengine",
    provider_config: dict | None = None,
    display_name: str = "Vivi 2.0",
    gender: str = "female",
    matchable: bool = True,
    verify_status: dict | None = None,
    source: str = "seed_migration",
) -> MagicMock:
    v = MagicMock(spec=VoiceCatalog)
    v.voice_id = voice_id
    v.provider = provider
    v.provider_config = provider_config or {"resource_id": "seed-tts-2.0"}
    v.display_name = display_name
    v.gender = gender
    v.language = "zh"
    v.scene = "通用"
    v.matchable = matchable
    v.verify_status = verify_status if verify_status is not None else {"default": {"verified": True, "at": "2026-04-02", "error": None}}
    v.verify_attempts = 0
    v.source = source
    v.archived_at = None
    v.notes = None
    v.created_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
    v.updated_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
    return v


def _make_label(
    voice_id: str = "zh_female_vv_uranus_bigtts",
    label_type: str = "text",
    is_current: bool = True,
    age_group: str = "young",
    persona_style: str = "energetic",
    energy_level: str = "high",
) -> MagicMock:
    lbl = MagicMock(spec=VoiceLabel)
    lbl.id = 1
    lbl.voice_id = voice_id
    lbl.label_type = label_type
    lbl.source_run_id = "seed-2026-04-02"
    lbl.is_current = is_current
    lbl.age_group = age_group
    lbl.persona_style = persona_style
    lbl.energy_level = energy_level
    lbl.pitch_level = None
    lbl.warmth = None
    lbl.authority = None
    lbl.intimacy = None
    lbl.brightness = None
    lbl.maturity = None
    lbl.delivery_style = None
    lbl.texture_tags = None
    lbl.childlike = None
    lbl.labeled_by = "seed_migration"
    lbl.labeled_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
    lbl.superseded_at = None
    return lbl


def _make_admin():
    return SimpleNamespace(id="admin-1", role="admin", email="admin@test.com")


def _make_user():
    return SimpleNamespace(id="user-1", role="user", email="user@test.com")


@pytest.fixture
def voice_app():
    """Lightweight FastAPI app with voice catalog routes + dependency overrides."""
    from database import get_db
    from auth import get_current_user

    app = FastAPI()
    app.include_router(voice_catalog_router)
    app.include_router(voice_catalog_internal_router)

    # Will be overridden per-test
    app.state.mock_user = _make_admin()
    app.state.mock_db = AsyncMock()

    async def override_get_current_user():
        return app.state.mock_user

    async def override_get_db():
        yield app.state.mock_db

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db

    return app


@pytest.fixture
def client(voice_app):
    return TestClient(voice_app, headers={"origin": "http://testserver"})


# ---------------------------------------------------------------------------
# ORM Model structure
# ---------------------------------------------------------------------------

class TestVoiceCatalogModel:
    def test_voice_catalog_has_expected_columns(self) -> None:
        table = VoiceCatalog.__table__
        col_names = {c.name for c in table.columns}
        expected = {
            "id", "voice_id", "provider", "provider_config", "display_name",
            "gender", "language", "scene", "matchable", "verify_status",
            "verify_attempts", "source", "archived_at", "notes",
            "created_at", "updated_at",
        }
        assert expected.issubset(col_names), f"Missing: {expected - col_names}"

    def test_voice_catalog_has_compatible_target_languages_column(self) -> None:
        # PR-E matchable migration (alembic 042): dub target-language compatibility.
        cols = {c.name for c in VoiceCatalog.__table__.columns}
        assert "compatible_target_languages" in cols

    def test_voice_label_has_fk_to_catalog(self) -> None:
        table = VoiceLabel.__table__
        fks = {fk.target_fullname for fk in table.foreign_keys}
        assert "voice_catalog.voice_id" in fks

    def test_voice_label_has_audit_columns(self) -> None:
        table = VoiceLabel.__table__
        col_names = {c.name for c in table.columns}
        for col in ("source_run_id", "is_current", "superseded_at", "labeled_by"):
            assert col in col_names


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerializeVoice:
    def test_verified_seed_voice(self) -> None:
        result = _serialize_voice(_make_voice(), {"text": True})
        assert result["is_verified"] is True
        assert result["is_seed"] is True
        assert result["verify_attempts"] == 0
        assert result["label_status"]["text"] is True
        assert result["label_status"]["final"] is False

    def test_unverified_voice(self) -> None:
        result = _serialize_voice(_make_voice(verify_status={}, source="csv_import"), {})
        assert result["is_verified"] is False
        assert result["is_seed"] is False

    def test_seed_voice_reverified_shows_as_verified(self) -> None:
        """After manual re-verify, seed voice should expose verify_attempts > 0."""
        v = _make_voice(source="seed_migration")
        v.verify_attempts = 1  # manually verified
        result = _serialize_voice(v, {})
        assert result["is_verified"] is True
        assert result["is_seed"] is True
        assert result["verify_attempts"] == 1
        # Frontend uses: is_verified && is_seed && verify_attempts == 0 → seed
        #                is_verified && verify_attempts > 0 → 已验证

    def test_failed_verify_shows_attempts(self) -> None:
        v = _make_voice(verify_status={}, source="csv_import")
        v.verify_attempts = 2
        result = _serialize_voice(v, {})
        assert result["is_verified"] is False
        assert result["verify_attempts"] == 2


# ---------------------------------------------------------------------------
# Route: GET /api/admin/voices (list)
# ---------------------------------------------------------------------------

class TestListVoicesRoute:

    def _setup_db_for_list(self, voice_app, voices, labels=None):
        """Configure mock DB to return given voices from list query."""
        db = voice_app.state.mock_db
        labels = labels or []

        # The endpoint does 3 queries: count, paginated voices, label objects
        count_result = MagicMock()
        count_result.scalar.return_value = len(voices)

        voices_result = MagicMock()
        voices_result.scalars.return_value.all.return_value = voices

        # Labels: now returns VoiceLabel objects via .scalars().all()
        label_objs = []
        for item in labels:
            if isinstance(item, tuple):
                label_objs.append(_make_label(voice_id=item[0], label_type=item[1]))
            else:
                label_objs.append(item)
        label_result = MagicMock()
        label_result.scalars.return_value.all.return_value = label_objs

        call_count = {"n": 0}
        async def smart_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return count_result
            if call_count["n"] == 2:
                return voices_result
            return label_result

        db.execute = smart_execute

    def test_admin_can_list_voices(self, voice_app, client) -> None:
        v1 = _make_voice()
        self._setup_db_for_list(voice_app, [v1])
        resp = client.get("/api/admin/voices")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["voice_id"] == "zh_female_vv_uranus_bigtts"

    def test_non_admin_rejected(self, voice_app, client) -> None:
        voice_app.state.mock_user = _make_user()
        resp = client.get("/api/admin/voices")
        assert resp.status_code == 403

    def test_unauthenticated_rejected(self, voice_app, client) -> None:
        voice_app.state.mock_user = None
        resp = client.get("/api/admin/voices")
        assert resp.status_code == 401

    def test_pagination_fields(self, voice_app, client) -> None:
        voices = [_make_voice(voice_id=f"voice_{i}") for i in range(3)]
        self._setup_db_for_list(voice_app, voices)
        resp = client.get("/api/admin/voices?page=1&page_size=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["page_size"] == 10

    def test_label_status_populated(self, voice_app, client) -> None:
        v1 = _make_voice()
        labels = [
            (v1.voice_id, "text"),
            (v1.voice_id, "audio_round1"),
        ]
        self._setup_db_for_list(voice_app, [v1], labels)
        resp = client.get("/api/admin/voices")
        data = resp.json()
        ls = data["items"][0]["label_status"]
        assert ls["text"] is True
        assert ls["audio_round1"] is True
        assert ls["final"] is False

    def test_verified_true_returns_only_verified(self, voice_app, client) -> None:
        """verified=true: DB returns only verified rows; total and items must match."""
        v_ok = _make_voice(voice_id="verified_voice", verify_status={"default": {"verified": True}})
        # When verified=true, the SQL WHERE filters in DB — mock returns only matching rows
        self._setup_db_for_list(voice_app, [v_ok])
        resp = client.get("/api/admin/voices?verified=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["voice_id"] == "verified_voice"
        assert data["items"][0]["is_verified"] is True

    def test_verified_false_returns_only_unverified(self, voice_app, client) -> None:
        """verified=false: DB returns only unverified rows."""
        v_bad = _make_voice(voice_id="unverified_voice", verify_status={})
        self._setup_db_for_list(voice_app, [v_bad])
        resp = client.get("/api/admin/voices?verified=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["voice_id"] == "unverified_voice"
        assert data["items"][0]["is_verified"] is False

    def test_verified_filter_total_matches_items(self, voice_app, client) -> None:
        """After SQL filtering, total must equal the number of filtered rows,
        not the pre-filter count. This was the original bug."""
        # Simulate: DB returns 2 verified voices out of a larger set.
        # The SQL WHERE already filtered, so count=2, items=2.
        v1 = _make_voice(voice_id="v1", verify_status={"default": {"verified": True}})
        v2 = _make_voice(voice_id="v2", verify_status={"default": {"verified": True}})
        self._setup_db_for_list(voice_app, [v1, v2])
        resp = client.get("/api/admin/voices?verified=true&page=1&page_size=50")
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["page"] == 1
        # No empty pages: items count <= page_size and <= total
        assert len(data["items"]) <= data["page_size"]
        assert len(data["items"]) <= data["total"]


# ---------------------------------------------------------------------------
# Route: GET /api/admin/voices/{voice_id} (detail)
# ---------------------------------------------------------------------------

class TestVoiceDetailRoute:

    def test_returns_voice_with_labels(self, voice_app, client) -> None:
        db = voice_app.state.mock_db
        v1 = _make_voice()
        lbl = _make_label()

        voice_result = MagicMock()
        voice_result.scalar_one_or_none.return_value = v1
        labels_result = MagicMock()
        labels_result.scalars.return_value.all.return_value = [lbl]

        call_count = {"n": 0}
        async def smart_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return voice_result
            return labels_result

        db.execute = smart_execute

        resp = client.get("/api/admin/voices/zh_female_vv_uranus_bigtts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["voice"]["voice_id"] == "zh_female_vv_uranus_bigtts"
        assert len(data["labels"]) == 1
        assert data["labels"][0]["label_type"] == "text"
        assert data["labels"][0]["is_current"] is True

    def test_404_for_nonexistent_voice(self, voice_app, client) -> None:
        db = voice_app.state.mock_db
        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        async def mock_execute(*args, **kwargs):
            return result

        db.execute = mock_execute

        resp = client.get("/api/admin/voices/nonexistent_voice")
        assert resp.status_code == 404

    def test_non_admin_rejected(self, voice_app, client) -> None:
        voice_app.state.mock_user = _make_user()
        resp = client.get("/api/admin/voices/any_voice")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Frontend API path
# ---------------------------------------------------------------------------

class TestFrontendApiPath:
    def test_uses_admin_api_not_job_api(self) -> None:
        src = Path(__file__).resolve().parent.parent / "frontend-next" / "src" / "lib" / "api" / "voiceCatalog.ts"
        content = src.read_text(encoding="utf-8")
        assert "/api/admin/voices" in content
        assert "from '@/lib/api/client'" not in content


# ---------------------------------------------------------------------------
# Phase 2: Write endpoints
# ---------------------------------------------------------------------------

class TestCreateVoice:
    def _setup_db_for_create(self, voice_app, existing=None):
        """Mock DB: select returns existing (or None), add + flush work."""
        db = voice_app.state.mock_db
        check_result = MagicMock()
        check_result.scalar_one_or_none.return_value = existing

        async def smart_execute(*args, **kwargs):
            return check_result

        db.execute = smart_execute
        db.add = MagicMock()

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    def test_create_voice_success(self, voice_app, client) -> None:
        self._setup_db_for_create(voice_app, existing=None)
        resp = client.post("/api/admin/voices", json={
            "voice_id": "test_new_voice",
            "provider": "volcengine",
            "display_name": "测试音色",
            "gender": "female",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["voice"]["voice_id"] == "test_new_voice"

    def test_create_voice_duplicate(self, voice_app, client) -> None:
        existing = _make_voice(voice_id="dup_voice")
        self._setup_db_for_create(voice_app, existing=existing)
        resp = client.post("/api/admin/voices", json={
            "voice_id": "dup_voice",
            "provider": "volcengine",
            "display_name": "重复",
        })
        assert resp.status_code == 409

    def test_create_voice_non_admin_rejected(self, voice_app, client) -> None:
        voice_app.state.mock_user = _make_user()
        resp = client.post("/api/admin/voices", json={
            "voice_id": "x", "provider": "volcengine", "display_name": "x",
        })
        assert resp.status_code == 403

    def test_create_voice_missing_required(self, voice_app, client) -> None:
        self._setup_db_for_create(voice_app)
        resp = client.post("/api/admin/voices", json={
            "provider": "volcengine",
            "display_name": "missing voice_id",
        })
        assert resp.status_code == 422  # pydantic validation


class TestUpdateVoice:
    def _setup_db_for_update(self, voice_app, voice):
        db = voice_app.state.mock_db
        result = MagicMock()
        result.scalar_one_or_none.return_value = voice

        async def smart_execute(*args, **kwargs):
            return result

        db.execute = smart_execute

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    def test_update_voice_success(self, voice_app, client) -> None:
        voice = _make_voice()
        self._setup_db_for_update(voice_app, voice)
        resp = client.patch("/api/admin/voices/zh_female_vv_uranus_bigtts", json={
            "display_name": "新名称",
        })
        assert resp.status_code == 200

    def test_update_voice_not_found(self, voice_app, client) -> None:
        self._setup_db_for_update(voice_app, None)
        resp = client.patch("/api/admin/voices/nonexistent", json={
            "display_name": "x",
        })
        assert resp.status_code == 404

    def test_update_voice_empty_body(self, voice_app, client) -> None:
        voice = _make_voice()
        self._setup_db_for_update(voice_app, voice)
        resp = client.patch("/api/admin/voices/zh_female_vv_uranus_bigtts", json={})
        assert resp.status_code == 400


class TestDeleteVoice:
    def _setup_db_for_delete(self, voice_app, voice):
        db = voice_app.state.mock_db
        result = MagicMock()
        result.scalar_one_or_none.return_value = voice

        async def smart_execute(*args, **kwargs):
            return result

        db.execute = smart_execute

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    def test_delete_voice_success(self, voice_app, client) -> None:
        voice = _make_voice()
        self._setup_db_for_delete(voice_app, voice)
        resp = client.delete("/api/admin/voices/zh_female_vv_uranus_bigtts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["archived"] is True

    def test_delete_voice_not_found(self, voice_app, client) -> None:
        self._setup_db_for_delete(voice_app, None)
        resp = client.delete("/api/admin/voices/nonexistent")
        assert resp.status_code == 404

    def test_delete_already_archived(self, voice_app, client) -> None:
        voice = _make_voice()
        voice.archived_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
        self._setup_db_for_delete(voice_app, voice)
        resp = client.delete("/api/admin/voices/zh_female_vv_uranus_bigtts")
        assert resp.status_code == 400


class TestVerifyVoice:
    def _setup_db_for_verify(self, voice_app, voice):
        db = voice_app.state.mock_db
        result = MagicMock()
        result.scalar_one_or_none.return_value = voice

        async def smart_execute(*args, **kwargs):
            return result

        db.execute = smart_execute

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    @patch("voice_catalog_service.verify_volcengine")
    def test_verify_volcengine_success(self, mock_verify, voice_app, client) -> None:
        voice = _make_voice(verify_status={})
        self._setup_db_for_verify(voice_app, voice)

        mock_verify.return_value = {
            "default": {"verified": True, "at": "2026-04-02T12:00:00+00:00", "error": None}
        }
        resp = client.post("/api/admin/voices/zh_female_vv_uranus_bigtts/verify")
        assert resp.status_code == 200
        data = resp.json()
        assert data["verify_status"]["default"]["verified"] is True

    def test_verify_voice_not_found(self, voice_app, client) -> None:
        self._setup_db_for_verify(voice_app, None)
        resp = client.post("/api/admin/voices/nonexistent/verify")
        assert resp.status_code == 404


class TestBatchVerify:
    def _setup_db_for_batch(self, voice_app, voices_map):
        """voices_map: dict[voice_id, VoiceCatalog | None]"""
        db = voice_app.state.mock_db

        async def smart_execute(*args, **kwargs):
            # Extract voice_id from the query (simplified)
            result = MagicMock()
            # We need to figure out which voice_id was queried
            for vid, v in voices_map.items():
                result.scalar_one_or_none.return_value = v
                break
            # Rotate for sequential calls
            return result

        db.execute = smart_execute

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    @patch("voice_catalog_service.verify_volcengine")
    def test_batch_verify(self, mock_verify, voice_app, client) -> None:
        voice = _make_voice(verify_status={})
        self._setup_db_for_batch(voice_app, {"zh_female_vv_uranus_bigtts": voice})
        mock_verify.return_value = {
            "default": {"verified": True, "at": "2026-04-02", "error": None}
        }
        resp = client.post("/api/admin/voices/verify-batch", json={
            "voice_ids": ["zh_female_vv_uranus_bigtts"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1


class TestImportVoices:
    def _setup_db_for_import(self, voice_app, existing_ids=None):
        """Mock DB: select returns existing if voice_id in existing_ids."""
        existing_ids = existing_ids or set()
        db = voice_app.state.mock_db

        async def smart_execute(*args, **kwargs):
            result = MagicMock()
            # Simple mock: always return None (no duplicates)
            result.scalar_one_or_none.return_value = None
            return result

        db.execute = smart_execute
        db.add = MagicMock()

        async def noop_flush():
            pass
        db.flush = noop_flush

        async def noop_commit():
            pass
        db.commit = noop_commit

    def test_import_dry_run(self, voice_app, client) -> None:
        self._setup_db_for_import(voice_app)
        resp = client.post("/api/admin/voices/import", json={
            "text": "voice_id,display_name,gender\nnew_voice_1,测试,female",
            "provider": "volcengine",
            "dry_run": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert len(data["entries"]) == 1
        assert data["entries"][0]["voice_id"] == "new_voice_1"

    def test_import_actual(self, voice_app, client) -> None:
        self._setup_db_for_import(voice_app)
        resp = client.post("/api/admin/voices/import", json={
            "text": "new_voice_1,测试,female",
            "provider": "volcengine",
            "dry_run": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is False
        assert "new_voice_1" in data["created"]

    def test_import_empty_text(self, voice_app, client) -> None:
        resp = client.post("/api/admin/voices/import", json={
            "text": "",
            "provider": "volcengine",
            "dry_run": True,
        })
        assert resp.status_code == 422  # pydantic: min_length=1


# ---------------------------------------------------------------------------
# Unit: parse_import_lines
# ---------------------------------------------------------------------------

class TestParseImportLines:
    def test_csv_with_header(self) -> None:
        text = "voice_id,display_name,gender,scene,resource_id\nv1,测试,female,通用,seed-tts-1.0"
        result = parse_import_lines(text, "volcengine")
        assert len(result) == 1
        assert result[0]["voice_id"] == "v1"
        assert result[0]["provider_config"] == {"resource_id": "seed-tts-1.0"}

    def test_csv_without_header(self) -> None:
        text = "v1,测试,female"
        result = parse_import_lines(text, "cosyvoice")
        assert len(result) == 1
        assert result[0]["voice_id"] == "v1"
        assert result[0]["provider"] == "cosyvoice"

    def test_tab_separated(self) -> None:
        text = "v1\t测试\tmale\t通用"
        result = parse_import_lines(text, "volcengine")
        assert len(result) == 1
        assert result[0]["gender"] == "male"
        assert result[0]["scene"] == "通用"

    def test_empty_text(self) -> None:
        assert parse_import_lines("", "volcengine") == []

    def test_multi_line(self) -> None:
        text = "v1,名称1,female\nv2,名称2,male\nv3,名称3,child"
        result = parse_import_lines(text, "volcengine")
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Phase 3: Internal endpoint for app runtime
# ---------------------------------------------------------------------------

class TestInternalVoiceCatalog:
    # T4 — _require_internal_access reads settings.internal_api_key at request
    # time and requires X-Internal-Key header to match. Tests use this fixed
    # key + header pair.
    _TEST_KEY = "test-internal-key-32-chars-xxxxxx"
    _HDR = {"X-Internal-Key": _TEST_KEY}

    @pytest.fixture(autouse=True)
    def _set_internal_key(self, monkeypatch, voice_app):
        # Configure the key settings expects, so _require_internal_access
        # neither 503s (unset) nor 403s (mismatch).
        from config import settings as _settings
        monkeypatch.setattr(_settings, "internal_api_key", self._TEST_KEY)
        # TestClient sets request.client.host to "testclient" (not 127.0.0.1),
        # which the loopback check would reject. Override the dependency with
        # a Header-based version that still enforces the key but skips the
        # loopback check. In production, the primary defense is Caddy
        # blocking /api/internal/* from public ingress.
        from fastapi import Header, HTTPException
        from voice_catalog_api import _require_internal_access

        async def _require_internal_access_no_loopback(
            x_internal_key: str | None = Header(default=None),
        ) -> None:
            key = _settings.internal_api_key
            if not key:
                raise HTTPException(status_code=503, detail="Internal endpoint misconfigured")
            if (x_internal_key or "") != key:
                raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Key")
            # NB: Skip loopback check — TestClient is not 127.0.0.1.

        voice_app.dependency_overrides[_require_internal_access] = _require_internal_access_no_loopback

    def _setup_db_for_internal(self, voice_app, voices, labels=None):
        """Mock DB for internal endpoint: voices query + labels query."""
        db = voice_app.state.mock_db
        labels = labels or []

        voices_result = MagicMock()
        voices_result.scalars.return_value.all.return_value = voices

        label_result = MagicMock()
        label_result.scalars.return_value.all.return_value = labels

        call_count = {"n": 0}
        async def smart_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return voices_result
            return label_result

        db.execute = smart_execute

    def test_internal_endpoint_returns_voices(self, voice_app, client) -> None:
        v1 = _make_voice(voice_id="zh_test_1", provider_config={"resource_id": "seed-tts-1.0"})
        v2 = _make_voice(voice_id="zh_test_2", provider_config={"resource_id": "seed-tts-1.0"})
        self._setup_db_for_internal(voice_app, [v1, v2])

        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0",
            headers=self._HDR,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["voices"]) == 2
        assert data["default_voice_id"] == "zh_female_shuangkuaisisi_moon_bigtts"
        assert "ts" in data

    def test_internal_endpoint_includes_labels(self, voice_app, client) -> None:
        v1 = _make_voice(voice_id="zh_test_1")
        lbl = _make_label(voice_id="zh_test_1", label_type="text", age_group="young", persona_style="warm", energy_level="medium")
        self._setup_db_for_internal(voice_app, [v1], [lbl])

        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-2.0",
            headers=self._HDR,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["voices"][0]["age_group"] == "young"
        assert data["voices"][0]["persona_style"] == "warm"
        assert data["voices"][0]["energy_level"] == "medium"

    # --- PR-E matchable migration: target-language filter (kill switch) ---

    def _setup_db_capture(self, voice_app, voices, labels=None):
        """Like _setup_db_for_internal but captures the first (voices) query."""
        db = voice_app.state.mock_db
        labels = labels or []
        captured: dict = {}
        voices_result = MagicMock()
        voices_result.scalars.return_value.all.return_value = voices
        label_result = MagicMock()
        label_result.scalars.return_value.all.return_value = labels
        call_count = {"n": 0}

        async def smart_execute(query, *a, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                captured["query"] = query
                return voices_result
            return label_result

        db.execute = smart_execute
        return captured

    def test_internal_endpoint_target_language_filter_off_by_default(self, voice_app, client) -> None:
        # Kill switch OFF (default) → legacy query, no language predicate (byte-identical).
        captured = self._setup_db_capture(voice_app, [_make_voice(voice_id="zh_test_1")])
        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0&target_language=zh-CN",
            headers=self._HDR,
        )
        assert resp.status_code == 200
        # The column appears once in the SELECT list; OFF means no extra WHERE predicate.
        assert str(captured["query"]).count("compatible_target_languages") == 1

    def test_internal_endpoint_target_language_filter_on(self, voice_app, client, monkeypatch) -> None:
        # Kill switch ON → query gains compatible_target_languages @> [target], so a zh
        # dub never returns en voices (the "止血" assertion).
        import admin_settings

        class _S:
            voice_catalog_target_language_filter_enabled = True

        monkeypatch.setattr(admin_settings, "load_settings", lambda: _S())
        captured = self._setup_db_capture(voice_app, [_make_voice(voice_id="zh_test_1")])
        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0&target_language=zh-CN",
            headers=self._HDR,
        )
        assert resp.status_code == 200
        # SELECT list (1) + the WHERE @> predicate (1) → at least 2 occurrences.
        _sql = str(captured["query"])
        assert _sql.count("compatible_target_languages") >= 2
        assert "compatible_target_languages @>" in _sql

    def test_internal_endpoint_no_user_auth_required(self, voice_app, client) -> None:
        """Internal endpoint should work without a user session (only shared-secret)."""
        voice_app.state.mock_user = None
        self._setup_db_for_internal(voice_app, [])

        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0",
            headers=self._HDR,
        )
        assert resp.status_code == 200

    def test_internal_endpoint_default_voice_2_0(self, voice_app, client) -> None:
        self._setup_db_for_internal(voice_app, [])

        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-2.0",
            headers=self._HDR,
        )
        data = resp.json()
        assert data["default_voice_id"] == "zh_female_shuangkuaisisi_uranus_bigtts"

    def test_internal_endpoint_requires_params(self, voice_app, client) -> None:
        resp = client.get("/api/internal/voice-catalog", headers=self._HDR)
        assert resp.status_code == 422

    # --- T4 access-control checks ---

    def test_missing_key_returns_403(self, voice_app, client) -> None:
        """No X-Internal-Key header -> 403."""
        self._setup_db_for_internal(voice_app, [])
        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0",
        )
        assert resp.status_code == 403

    def test_wrong_key_returns_403(self, voice_app, client) -> None:
        """Mismatched X-Internal-Key -> 403."""
        self._setup_db_for_internal(voice_app, [])
        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0",
            headers={"X-Internal-Key": "wrong-key"},
        )
        assert resp.status_code == 403

    def test_unset_key_returns_503(self, voice_app, client, monkeypatch) -> None:
        """Settings key empty -> 503 (fail-closed, defense-in-depth)."""
        from config import settings as _settings
        monkeypatch.setattr(_settings, "internal_api_key", "")
        self._setup_db_for_internal(voice_app, [])
        resp = client.get(
            "/api/internal/voice-catalog?provider=volcengine&resource_id=seed-tts-1.0",
            headers=self._HDR,
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 4 v2: Trigger labeling endpoints
# ---------------------------------------------------------------------------

class _TriggerTestMixin:
    """Shared helpers for trigger endpoint tests."""

    def _setup_validation_db(self, voice_app, voices_found):
        """Mock DB for _validate_volcengine_voices: return given voices."""
        db = voice_app.state.mock_db
        result = MagicMock()
        result.scalars.return_value.all.return_value = voices_found

        async def mock_execute(*args, **kwargs):
            return result
        db.execute = mock_execute

    def _setup_full_trigger_db(self, voice_app, voices_found):
        """Mock DB for validation + write_labels_batch."""
        db = voice_app.state.mock_db

        validation_result = MagicMock()
        validation_result.scalars.return_value.all.return_value = voices_found

        write_voice_result = MagicMock()
        write_voice_result.scalar_one_or_none.return_value = voices_found[0] if voices_found else None

        write_label_result = MagicMock()
        write_label_result.scalars.return_value.all.return_value = []

        call_count = {"n": 0}
        async def smart_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return validation_result
            if call_count["n"] <= 3:
                return write_voice_result
            return write_label_result

        db.execute = smart_execute
        db.add = MagicMock()
        async def noop(): pass
        db.flush = noop
        db.commit = noop


class TestTriggerTextLabeling(_TriggerTestMixin):
    def test_non_admin_rejected(self, voice_app, client) -> None:
        voice_app.state.mock_user = _make_user()
        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": ["v1"]})
        assert resp.status_code == 403

    def test_empty_voice_ids(self, voice_app, client) -> None:
        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": []})
        assert resp.status_code == 422

    def test_nonexistent_voice_rejected(self, voice_app, client) -> None:
        self._setup_validation_db(voice_app, [])  # no voices found
        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": ["nonexistent"]})
        assert resp.status_code == 400
        assert "不存在" in resp.json()["detail"]

    def test_unsupported_provider_rejected(self, voice_app, client) -> None:
        minimax = _make_voice(voice_id="mm_voice", provider="minimax")
        self._setup_validation_db(voice_app, [minimax])
        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": ["mm_voice"]})
        assert resp.status_code == 400
        assert "不是" in resp.json()["detail"]

    def test_archived_voice_rejected(self, voice_app, client) -> None:
        archived = _make_voice(voice_id="v1")
        archived.archived_at = datetime(2026, 4, 2, tzinfo=timezone.utc)
        self._setup_validation_db(voice_app, [archived])
        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": ["v1"]})
        assert resp.status_code == 400
        assert "归档" in resp.json()["detail"]

    @patch("voice_catalog_api.httpx")
    def test_success_path(self, mock_httpx, voice_app, client) -> None:
        """Full success: validation → app call → write_labels_batch."""
        v1 = _make_voice(voice_id="v1", provider="volcengine")
        self._setup_full_trigger_db(voice_app, [v1])

        # Mock httpx async client
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "labels": [{"voice_id": "v1", "age_group": "young", "persona_style": "warm", "energy_level": "medium"}],
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)
        mock_httpx.AsyncClient.return_value = mock_client_instance

        resp = client.post("/api/admin/voices/label/trigger-text", json={"voice_ids": ["v1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert "source_run_id" in data
        assert "v1" in data.get("written", [])


class TestTriggerAudioLabeling(_TriggerTestMixin):
    def test_invalid_round(self, voice_app, client) -> None:
        resp = client.post("/api/admin/voices/label/trigger-audio/round4", json={"voice_ids": ["v1"]})
        assert resp.status_code == 400

    def test_over_10_triggers_auto_chunking(self, voice_app, client) -> None:
        """Over 10 voices: auto-chunked, not rejected."""
        ids = [f"v{i}" for i in range(11)]
        # Will fail at validation (voices don't exist in mock DB) but NOT at limit check
        self._setup_validation_db(voice_app, [])
        resp = client.post("/api/admin/voices/label/trigger-audio/round1", json={"voice_ids": ids})
        # 400 because voices don't exist, NOT because of limit
        assert resp.status_code == 400
        assert "不存在" in resp.json()["detail"]

    def test_nonexistent_voice_rejected(self, voice_app, client) -> None:
        self._setup_validation_db(voice_app, [])
        resp = client.post("/api/admin/voices/label/trigger-audio/round1", json={"voice_ids": ["nonexistent"]})
        assert resp.status_code == 400

    @patch("voice_catalog_api.httpx")
    def test_success_path_round1(self, mock_httpx, voice_app, client) -> None:
        v1 = _make_voice(voice_id="v1", provider="volcengine")
        self._setup_full_trigger_db(voice_app, [v1])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "labels": [{"voice_id": "v1", "pitch_level": "high", "warmth": "medium"}],
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)
        mock_httpx.AsyncClient.return_value = mock_client_instance

        resp = client.post("/api/admin/voices/label/trigger-audio/round1", json={"voice_ids": ["v1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert "source_run_id" in data
        assert "round1" in data["source_run_id"]
