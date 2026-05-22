"""Strong-named cross-source auto-reuse (2026-05-21).

== User feedback ==

Stanford communication video (job_f2abf73878b343b6bbbde36ced9fa63c)
exposed two gaps in smart's cross-source voice matching:

1. **NULL-hash legacy voices excluded.** Pre-2026-05-16 user voices
   were stored without source_content_hash. The cross-source SQL
   filter ``source_content_hash IS NOT NULL`` excluded them from
   candidate queries — so Matt's voice from 2026-04-26 never
   surfaced even though name matched.
2. **Cross-source matches stayed weak.** Even when name_key matched
   uniquely (the user only has one Matt Abrahams in their library),
   the match was rated "weak" with auto_reuse_allowed=False,
   forcing a user confirmation pause. User expects: distinctive
   name + unique in user library = auto-reuse.

This spec change:
  - Removes the NULL-hash SQL filter so legacy voices participate.
  - Adds a new confidence tier "strong_named" that's auto-reuse-
    allowed when name_key matches EXACTLY ONE voice in the user's
    library (deterministic uniqueness, not name-length heuristic).
  - 2+ candidates for same name_key stay weak → smart pauses for
    user to pick which one.

Real production impact: 100+ legacy user voices (Matt, Warren
Buffett, Charlie Munger, Jensen Huang, etc.) become auto-reusable
on next job that names the speaker.
"""
from __future__ import annotations

import sys
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


def _voice(
    voice_id: str,
    *,
    source_content_hash: str | None = "youtube:abc",
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


def _multi_query_db(*result_voices):
    results = []
    for voices in result_voices:
        r = MagicMock()
        r.scalars.return_value.all.return_value = list(voices)
        results.append(r)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    return db


class TestUserVoiceMatchStrongNamedTier:
    """The new ``strong_named`` confidence tier."""

    def test_strong_named_confidence_is_auto_reuse_allowed(self):
        from user_voice_service import UserVoiceMatch

        match = UserVoiceMatch(
            voice=_voice("vt_x"),
            confidence="strong_named",
            reason="cross_source_unique_specific_name",
            score=60,
        )
        assert match.auto_reuse_allowed is True, (
            "strong_named must be auto-reuse-allowed; otherwise the "
            "promotion has no effect — smart still pauses."
        )

    def test_strong_named_match_scope_defaults_to_cross_source_named_unique(self):
        from user_voice_service import UserVoiceMatch

        match = UserVoiceMatch(
            voice=_voice("vt_x"),
            confidence="strong_named",
            reason="cross_source_unique_specific_name",
            score=60,
        )
        assert match.match_scope == "cross_source_named_unique"

    def test_weak_confidence_still_not_auto_reuse_allowed(self):
        """Backward compat: existing weak tier behavior preserved."""
        from user_voice_service import UserVoiceMatch

        match = UserVoiceMatch(
            voice=_voice("vt_x"),
            confidence="weak",
            reason="cross_source_same_speaker_name_key",
            score=20,
        )
        assert match.auto_reuse_allowed is False


class TestUniqueCrossSourcePromotion:
    """``match_user_voices`` promotes unique cross-source named matches."""

    def test_single_cross_source_named_promoted_to_strong_named(self):
        """Real Matt scenario: only 1 voice named 马特·亚伯拉罕斯 in user
        library → auto-reuse without pausing."""
        from user_voice_service import match_user_voices

        matt_voice = _voice(
            "vt_matt",
            source_content_hash="youtube:original_matt_video",
            source_speaker_id="speaker_a",
            source_speaker_name_key="马特·亚伯拉罕斯",
        )
        db = _multi_query_db([], [matt_voice])  # no same-source, 1 cross-source
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:new_stanford_video",
                source_speaker_id="speaker_a",
                source_speaker_name="马特·亚伯拉罕斯",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        assert len(matches) == 1
        m = matches[0]
        assert m.confidence == "strong_named", (
            f"Single cross-source named match must be promoted to "
            f"strong_named; got confidence={m.confidence!r}. Without "
            f"the promotion smart would pause + ask user to confirm."
        )
        assert m.auto_reuse_allowed is True
        assert m.score == 60, f"strong_named score should be 60, got {m.score}"
        assert m.match_scope == "cross_source_named_unique"

    def test_two_cross_source_named_both_stay_weak(self):
        """When user has 2+ voices with same name (e.g., re-cloned the
        same speaker from different videos), can't auto-pick — both
        stay weak so smart pauses for user to choose."""
        from user_voice_service import match_user_voices

        matt_v1 = _voice(
            "vt_matt_v1",
            source_content_hash="youtube:matt_video_1",
            source_speaker_id="speaker_a",
            source_speaker_name_key="马特·亚伯拉罕斯",
        )
        matt_v2 = _voice(
            "vt_matt_v2",
            source_content_hash="youtube:matt_video_2",
            source_speaker_id="speaker_b",
            source_speaker_name_key="马特·亚伯拉罕斯",
        )
        db = _multi_query_db([], [matt_v1, matt_v2])
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:new_video",
                source_speaker_id="speaker_a",
                source_speaker_name="马特·亚伯拉罕斯",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        assert len(matches) == 2
        for m in matches:
            assert m.confidence == "weak", (
                f"2+ same-name candidates must STAY weak (smart pauses "
                f"for user pick); got {m.confidence!r}"
            )
            assert m.auto_reuse_allowed is False

    def test_strong_same_source_outscores_strong_named_cross_source(self):
        """If both exist (strong same-source AND unique cross-source-
        named), sort order must put strong same-source first so smart
        auto-reuses the more confident match."""
        from user_voice_service import match_user_voices

        same_source = _voice(
            "vt_same",
            source_content_hash="youtube:current",
            source_speaker_id="speaker_a",  # matches speaker_a in current
            source_speaker_name_key="alice",
        )
        cross_source = _voice(
            "vt_cross",
            source_content_hash="youtube:other",
            source_speaker_id="speaker_x",
            source_speaker_name_key="alice",
        )
        db = _multi_query_db([same_source], [cross_source])
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:current",
                source_speaker_id="speaker_a",
                source_speaker_name="Alice",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        assert len(matches) == 2
        # Strong same-source (score 100) sorts first
        assert matches[0].voice.voice_id == "vt_same"
        assert matches[0].confidence == "strong"
        # Strong_named cross-source (score 60) second
        assert matches[1].voice.voice_id == "vt_cross"
        assert matches[1].confidence == "strong_named"


class TestNullHashLegacyVoicesIncluded:
    """Pre-2026-05-16 NULL-hash legacy voices must participate in
    cross-source matching (after my backfill of name_key)."""

    def test_legacy_null_hash_voice_with_name_key_is_matched(self):
        """The real Matt voice (vt_speaker_a_1777199510032, 2026-04-26,
        NULL hash, backfilled name_key='马特·亚伯拉罕斯') must surface
        as a cross-source candidate. The OLD ``IS NOT NULL`` SQL
        filter excluded it; new behavior includes it."""
        from user_voice_service import match_user_voices

        legacy = _voice(
            "vt_legacy_matt",
            source_content_hash=None,  # NULL hash
            source_speaker_id="speaker_legacy",
            source_speaker_name_key="马特·亚伯拉罕斯",
        )
        db = _multi_query_db([], [legacy])
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:new_video",
                source_speaker_id="speaker_a",
                source_speaker_name="马特·亚伯拉罕斯",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        assert len(matches) == 1, (
            "Legacy NULL-hash voice with matching name_key MUST surface "
            "now. Before this change, the ``IS NOT NULL`` SQL filter "
            "excluded it — that was the root cause of Matt failing to "
            "match on the Stanford job after backfilling name_key alone."
        )
        m = matches[0]
        # It's also unique → should be promoted to strong_named
        assert m.confidence == "strong_named"
        assert m.auto_reuse_allowed is True
        assert m.voice.voice_id == "vt_legacy_matt"

    def test_legacy_null_hash_query_does_not_use_is_not_null_filter(self):
        """Pin the SQL: the cross-source query must NOT include the
        ``source_content_hash IS NOT NULL`` clause anymore."""
        from user_voice_service import match_user_voices

        db = _multi_query_db([], [])  # empty result
        _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:new",
                source_speaker_id="speaker_a",
                source_speaker_name="马特·亚伯拉罕斯",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        # The 2nd call is the cross-source query.
        cross_call = db.execute.await_args_list[1]
        stmt = cross_call.args[0]
        sql_text = str(stmt.compile())
        assert "source_content_hash IS NOT NULL" not in sql_text, (
            f"cross-source SQL still has IS NOT NULL filter — this "
            f"excludes legacy NULL-hash voices, which is the bug we're "
            f"fixing. SQL:\n{sql_text}"
        )


class TestGenericNamesNeverPromoted:
    """Generic placeholder names (主持人, Speaker A) can't be
    cross-source matched — already filtered at SQL level. Pin the
    promotion logic doesn't accidentally promote them either."""

    def test_generic_name_never_reaches_cross_source_query(self):
        from user_voice_service import match_user_voices

        # No cross query needed — generic name short-circuits.
        db = _multi_query_db([], [])
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:abc",
                source_speaker_id="speaker_a",
                source_speaker_name="主持人",  # generic
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=True,
            )
        )
        assert matches == []


class TestPromotionDoesNotAffectSameSourceMatches:
    """The promotion logic targets cross-source matches only. Same-
    source matches (strong / medium / weak via _score_user_voice_match)
    are untouched."""

    def test_same_source_strong_match_unchanged(self):
        from user_voice_service import match_user_voices

        same_source = _voice(
            "vt_x",
            source_content_hash="youtube:current",
            source_speaker_id="speaker_a",
            source_speaker_name_key="alice",
        )
        db = _multi_query_db([same_source])
        matches = _run(
            match_user_voices(
                db,
                user_id="user-1",
                source_content_hash="youtube:current",
                source_speaker_id="speaker_a",
                source_speaker_name="Alice",
                provider="minimax_voice_clone",
                tts_provider="minimax_tts",
                platform="minimax_domestic",
                include_cross_source=False,  # no cross-source query
            )
        )
        assert len(matches) == 1
        assert matches[0].confidence == "strong"
        assert matches[0].match_scope == "same_source_strong"
