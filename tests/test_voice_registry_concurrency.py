"""T3.2 regression guards: VoiceRegistry's load→modify→save sequence must be
serialized against concurrent writers.

Before T3.2, ``save()`` was already atomic (tempfile + ``os.replace``), but the
full read-modify-write sequence inside ``register_voice`` / ``record_voice_verification``
/ ``set_default_voice`` / ``set_project_default_builtin_voice`` was not. Two threads
calling ``register_voice`` at the same time would both read the same base state,
each append their own voice to the in-memory list, and the second ``save()`` would
silently overwrite the first — losing an entry.

These tests construct that exact race and assert both writes are preserved.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from services.voice_registry import VoiceRegistry


@pytest.fixture
def registry(tmp_path):
    return VoiceRegistry(str(tmp_path / "voice_registry.json"))


class TestConcurrentRegisterVoice:
    def test_two_threads_register_different_speakers_both_persist(self, registry):
        """Two threads register voices for different speakers simultaneously.
        Without the lock, one write would overwrite the other. With T3.2 in
        place, both entries end up in the final file."""
        errors: list[BaseException] = []

        def _register(speaker_id: str, voice_id: str):
            try:
                registry.register_voice(
                    speaker_id,
                    speaker_name=speaker_id.upper(),
                    voice_id=voice_id,
                    voice_type="builtin",
                    provider="volcengine",
                    label=f"Label {voice_id}",
                )
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_register, args=("speaker_a", "voice_alpha"))
        t2 = threading.Thread(target=_register, args=("speaker_b", "voice_beta"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"unexpected errors: {errors}"

        # Both speakers must be present. Without the lock, one would be
        # missing because two save() calls racing overwrite each other.
        data = registry.load()
        speakers = data["speakers"]
        assert "speaker_a" in speakers
        assert "speaker_b" in speakers
        assert len(speakers["speaker_a"]["voices"]) == 1
        assert len(speakers["speaker_b"]["voices"]) == 1
        assert speakers["speaker_a"]["voices"][0]["voice_id"] == "voice_alpha"
        assert speakers["speaker_b"]["voices"][0]["voice_id"] == "voice_beta"

    def test_two_threads_register_same_speaker_both_voices_persist(self, registry):
        """Register two different voices for the SAME speaker concurrently.
        Both should end up in the speaker's voices list."""
        errors: list[BaseException] = []

        def _register(voice_id: str):
            try:
                registry.register_voice(
                    "shared_speaker",
                    speaker_name="Shared Speaker",
                    voice_id=voice_id,
                    voice_type="builtin",
                    provider="volcengine",
                    label=f"Label {voice_id}",
                )
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_register, args=("voice_x",))
        t2 = threading.Thread(target=_register, args=("voice_y",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"unexpected errors: {errors}"

        data = registry.load()
        voices = data["speakers"]["shared_speaker"]["voices"]
        voice_ids = {v["voice_id"] for v in voices}
        assert voice_ids == {"voice_x", "voice_y"}, (
            f"expected both voice_x and voice_y under shared_speaker, got {voice_ids}"
        )


class TestReentrancy:
    def test_set_default_voice_fallthrough_to_register_does_not_deadlock(self, registry):
        """``set_default_voice`` wraps its body in ``file_lock``; when the
        voice isn't found locally it delegates to ``register_voice``, which
        also takes the lock. Must not self-deadlock (reentrant lock)."""
        # Use a known CosyVoice builtin that the catalog helper will resolve.
        # The key point is the call shouldn't hang. A 5s timeout is generous
        # — the real risk is infinite-block on deadlock.
        import services.voice_registry as vr_mod

        # Stub the builtin-voice resolver so the delegate path is guaranteed.
        def _fake_resolver(voice_id: str):
            return {
                "voice_id": voice_id,
                "provider": "cosyvoice",
                "tts_provider": "cosyvoice",
                "platform": "cosyvoice",
                "label": f"Builtin {voice_id}",
                "created_at": "2026-04-17T00:00:00Z",
            }

        original = vr_mod.build_cosyvoice_v3_flash_builtin_voice_option
        vr_mod.build_cosyvoice_v3_flash_builtin_voice_option = _fake_resolver

        try:
            # First, ensure the speaker exists with a voice so we can test
            # set_default_voice path (otherwise it would raise "Speaker not found").
            registry.register_voice(
                "test_speaker",
                speaker_name="Test",
                voice_id="existing_voice",
                voice_type="builtin",
                provider="cosyvoice",
                label="Existing",
            )

            # Now call set_default with a voice_id that's NOT yet registered.
            # This triggers: file_lock.acquire → _find_voice_payload misses
            # → register_voice(set_default=True) → file_lock.acquire (reentrant).
            result = registry.set_default_voice("test_speaker", "new_builtin")
            assert result is not None
            assert result.default_voice_id == "new_builtin"
        finally:
            vr_mod.build_cosyvoice_v3_flash_builtin_voice_option = original


class TestFileLockHelper:
    def test_file_lock_is_reentrant_same_thread(self, tmp_path):
        """Direct test on the underlying helper — nested file_lock() calls
        on the same path, same thread, must return quickly rather than
        deadlocking."""
        from services._file_lock import file_lock

        target = tmp_path / "target.json"
        with file_lock(target):
            # Nested entry — must not block.
            with file_lock(target):
                pass

    def test_file_lock_serializes_different_threads(self, tmp_path):
        """Two threads contending for the same lock run serially, never
        concurrently."""
        from services._file_lock import file_lock

        target = tmp_path / "target.json"
        log: list[str] = []
        log_guard = threading.Lock()

        def _hold_briefly(tag: str):
            with file_lock(target):
                with log_guard:
                    log.append(f"enter-{tag}")
                # Brief work window — if serialization works, the other
                # thread cannot enter between our enter and leave events.
                import time
                time.sleep(0.05)
                with log_guard:
                    log.append(f"leave-{tag}")

        t1 = threading.Thread(target=_hold_briefly, args=("A",))
        t2 = threading.Thread(target=_hold_briefly, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Legal sequences (one of two):
        #   [enter-A, leave-A, enter-B, leave-B]
        #   [enter-B, leave-B, enter-A, leave-A]
        # Illegal (race):
        #   [enter-A, enter-B, leave-A, leave-B]  ← interleaved
        assert len(log) == 4, f"expected 4 events, got {log}"
        # First two must match (enter X, leave X), then other thread.
        first_thread = log[0].split("-")[1]
        assert log[1] == f"leave-{first_thread}", f"interleaved log: {log}"
