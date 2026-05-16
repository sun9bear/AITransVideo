from __future__ import annotations

import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_build_cloned_voice_label_uses_speaker_name_and_shanghai_time():
    from user_voice_service import build_cloned_voice_label

    label = build_cloned_voice_label(
        "Speaker A",
        cloned_at=datetime(2026, 5, 16, 6, 32, tzinfo=timezone.utc),
    )

    assert label == "Speaker A · 2026-05-16 14:32"


def test_normalize_speaker_name_key_is_conservative():
    from user_voice_service import normalize_speaker_name_key

    assert normalize_speaker_name_key("  Alice- ") == "alice"
    assert normalize_speaker_name_key(" Ａｌｉｃｅ ") == "alice"


def test_add_user_voice_preserves_existing_source_metadata_on_upsert():
    from user_voice_service import add_user_voice

    existing = SimpleNamespace(
        label="Old",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
        source_speaker_id="speaker_a",
        source_job_id="job_original",
        source_type="youtube_url",
        source_ref="https://youtu.be/original",
        source_content_hash="youtube:original",
        source_upload_md5=None,
        source_video_title="Original",
        source_speaker_name="Alice",
        source_speaker_name_key="alice",
        source_published_at=None,
        source_content_summary=None,
        source_content_era=None,
        source_content_tags=None,
        clone_sample_seconds=12.5,
        clone_sample_segment_ids=[1, 2],
        created_from="studio_manual",
        notes="old",
        expired_at=None,
        updated_at=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    voice = _run(
        add_user_voice(
            db,
            user_id="user-1",
            voice_id="vt_1",
            label="New",
            source_speaker_id="speaker_b",
            source_job_id="job_new",
            source_type="local_video",
            source_ref="uploads/u/new.mp4",
            source_content_hash="sha256:new",
            source_video_title="New",
            source_speaker_name="Bob",
            clone_sample_seconds=99.0,
            clone_sample_segment_ids=[3],
            created_from="smart_auto",
            notes="new",
        )
    )

    assert voice is existing
    assert existing.label == "New"
    assert existing.notes == "new"
    assert existing.source_speaker_id == "speaker_a"
    assert existing.source_job_id == "job_original"
    assert existing.source_type == "youtube_url"
    assert existing.source_ref == "https://youtu.be/original"
    assert existing.source_content_hash == "youtube:original"
    assert existing.source_video_title == "Original"
    assert existing.source_speaker_name == "Alice"
    assert existing.clone_sample_seconds == 12.5
    assert existing.clone_sample_segment_ids == [1, 2]
    assert existing.created_from == "studio_manual"
    db.commit.assert_awaited_once()


def test_add_user_voice_logs_warning_on_immutable_source_conflict(caplog):
    from user_voice_service import add_user_voice

    existing = SimpleNamespace(
        label="Old",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
        source_speaker_id="speaker_a",
        source_job_id="job_original",
        source_type="youtube_url",
        source_ref=None,
        source_content_hash="youtube:original",
        source_upload_md5=None,
        source_video_title=None,
        source_speaker_name=None,
        source_speaker_name_key=None,
        source_published_at=None,
        source_content_summary=None,
        source_content_era=None,
        source_content_tags=None,
        clone_sample_seconds=None,
        clone_sample_segment_ids=None,
        created_from=None,
        notes=None,
        expired_at=None,
        updated_at=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="user_voice_service"):
        _run(
            add_user_voice(
                db,
                user_id="user-1",
                voice_id="vt_conflict",
                label="New",
                source_job_id="job_new",
                source_content_hash="youtube:new",
            )
        )

    assert existing.source_job_id == "job_original"
    assert existing.source_content_hash == "youtube:original"
    assert "immutable source metadata conflict" in caplog.text
    assert "source_job_id" in caplog.text
    assert "source_content_hash" in caplog.text
    assert "vt_conflict" in caplog.text


def test_add_user_voice_fills_missing_source_metadata_on_upsert():
    from user_voice_service import add_user_voice

    existing = SimpleNamespace(
        label="Old",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
        source_speaker_id=None,
        source_job_id=None,
        source_type=None,
        source_ref=None,
        source_content_hash=None,
        source_upload_md5=None,
        source_video_title=None,
        source_speaker_name=None,
        source_speaker_name_key=None,
        source_published_at=None,
        source_content_summary=None,
        source_content_era=None,
        source_content_tags=None,
        clone_sample_seconds=None,
        clone_sample_segment_ids=None,
        created_from=None,
        notes=None,
        expired_at=None,
        updated_at=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    _run(
        add_user_voice(
            db,
            user_id="user-1",
            voice_id="vt_1",
            label="New",
            source_speaker_id="speaker_a",
            source_job_id="job_1",
            source_type="youtube_url",
            source_ref="https://youtu.be/abc",
            source_content_hash="youtube:abc",
            source_speaker_name="Alice",
            clone_sample_seconds=10.5,
            clone_sample_segment_ids=[1],
            created_from="studio_manual",
        )
    )

    assert existing.source_speaker_id == "speaker_a"
    assert existing.source_job_id == "job_1"
    assert existing.source_type == "youtube_url"
    assert existing.source_ref == "https://youtu.be/abc"
    assert existing.source_content_hash == "youtube:abc"
    assert existing.source_speaker_name == "Alice"
    assert existing.source_speaker_name_key == "alice"
    assert existing.clone_sample_seconds == 10.5
    assert existing.clone_sample_segment_ids == [1]
    assert existing.created_from == "studio_manual"


def _fake_match_db(voices):
    result = MagicMock()
    result.scalars.return_value.all.return_value = voices
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _voice(
    voice_id: str,
    *,
    source_content_hash: str = "youtube:abc",
    source_speaker_id: str | None = "speaker_a",
    source_speaker_name_key: str | None = "alice",
    provider: str = "minimax_voice_clone",
    tts_provider: str | None = "minimax_tts",
    platform: str | None = "minimax_domestic",
    expired_at=None,
    created_at=None,
):
    return SimpleNamespace(
        voice_id=voice_id,
        source_content_hash=source_content_hash,
        source_speaker_id=source_speaker_id,
        source_speaker_name_key=source_speaker_name_key,
        provider=provider,
        tts_provider=tts_provider,
        platform=platform,
        expired_at=expired_at,
        created_at=created_at,
    )


def test_match_user_voices_returns_strong_match_for_same_hash_and_speaker_id():
    from user_voice_service import match_user_voices

    matches = _run(
        match_user_voices(
            _fake_match_db([_voice("vt_a")]),
            user_id="user-1",
            source_content_hash="youtube:abc",
            source_speaker_id="speaker_a",
            source_speaker_name="Alice",
            provider="minimax_voice_clone",
            tts_provider="minimax_tts",
            platform="minimax_domestic",
        )
    )

    assert len(matches) == 1
    assert matches[0].voice.voice_id == "vt_a"
    assert matches[0].confidence == "strong"
    assert matches[0].auto_reuse_allowed is True
    assert matches[0].reason == "same_source_content_hash_and_speaker_id"


def test_match_user_voices_returns_medium_match_for_same_hash_and_name_key():
    from user_voice_service import match_user_voices

    matches = _run(
        match_user_voices(
            _fake_match_db([
                _voice("vt_name", source_speaker_id="speaker_b", source_speaker_name_key="alice")
            ]),
            user_id="user-1",
            source_content_hash="youtube:abc",
            source_speaker_id="speaker_a",
            source_speaker_name=" Alice ",
            provider="minimax_voice_clone",
            tts_provider="minimax_tts",
            platform="minimax_domestic",
        )
    )

    assert len(matches) == 1
    assert matches[0].confidence == "medium"
    assert matches[0].auto_reuse_allowed is False
    assert matches[0].reason == "same_source_content_hash_and_speaker_name"


def test_match_user_voices_skips_expired_and_provider_incompatible_rows():
    from datetime import datetime, timezone

    from user_voice_service import match_user_voices

    matches = _run(
        match_user_voices(
            _fake_match_db([
                _voice("vt_expired", expired_at=datetime.now(timezone.utc)),
                _voice("vt_other_provider", provider="cosyvoice_voice_clone"),
                _voice("vt_other_tts", tts_provider="cosyvoice_tts"),
                _voice("vt_ok"),
            ]),
            user_id="user-1",
            source_content_hash="youtube:abc",
            source_speaker_id="speaker_a",
            provider="minimax_voice_clone",
            tts_provider="minimax_tts",
            platform="minimax_domestic",
        )
    )

    assert [m.voice.voice_id for m in matches] == ["vt_ok"]


def test_match_user_voices_requires_non_empty_source_content_hash():
    from user_voice_service import match_user_voices

    db = _fake_match_db([_voice("vt_a")])
    matches = _run(
        match_user_voices(
            db,
            user_id="user-1",
            source_content_hash=None,
            source_speaker_id="speaker_a",
            provider="minimax_voice_clone",
            tts_provider="minimax_tts",
            platform="minimax_domestic",
        )
    )

    assert matches == []
    db.execute.assert_not_called()
