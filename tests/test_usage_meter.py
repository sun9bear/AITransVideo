from __future__ import annotations

import json
from pathlib import Path

from services.usage_meter import (
    TTS_BUCKET_FIRST,
    TTS_BUCKET_INTERACTIVE_PREVIEW,
    TTS_BUCKET_POST_EDIT_RESYNTH,
    TTS_BUCKET_POST_TTS_RESYNTH,
    TTS_BUCKET_PROBE,
    UsageMeter,
)


def test_usage_meter_writes_events_and_summarizes_job_cost_buckets(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path, job_id="job-usage-1")

    meter.record_tts(
        bucket=TTS_BUCKET_FIRST,
        provider="minimax",
        model="speech-2.8-turbo",
        text="你好",
        billed_chars=4,
        segment_id=1,
    )
    meter.record_tts(
        bucket=TTS_BUCKET_PROBE,
        provider="minimax",
        model="speech-2.8-turbo",
        text="探针",
        billed_chars=4,
        segment_id=2,
    )
    meter.record_tts(
        bucket=TTS_BUCKET_POST_TTS_RESYNTH,
        provider="cosyvoice",
        model="cosyvoice-v2",
        text="重合成",
        billed_chars=6,
        segment_id=3,
    )
    meter.record_tts(
        bucket=TTS_BUCKET_POST_EDIT_RESYNTH,
        provider="minimax",
        model="speech-2.8-hd",
        text="编辑",
        billed_chars=4,
        segment_id=4,
    )
    meter.record_tts(
        bucket=TTS_BUCKET_INTERACTIVE_PREVIEW,
        provider="minimax",
        model="speech-2.8-turbo",
        text="试听",
        billed_chars=4,
        segment_id="preview",
    )

    meter.record_llm(
        task="s2_pass1",
        provider="gemini",
        model="gemini_pro",
        model_id="gemini-2.5-pro",
        input_text="speaker prompt",
        output_text='{"speakers":{}}',
        audio_input_bytes=1024,
        audio_input_count=1,
        audio_input_seconds=30.5,
    )
    meter.record_llm(
        task="s5_rewrite",
        phase="pre_tts_rewrite",
        provider="gemini",
        model="gemini_flash",
        input_text="rewrite prompt",
        output_text="rewritten",
    )
    meter.record_llm(
        task="s3_translate",
        phase="probe_translate",
        provider="gemini",
        model="gemini_flash",
        input_text="translate prompt",
        output_text="translated",
    )
    meter.record_llm(
        task="s1_gemini_transcribe",
        provider="gemini",
        model="gemini_pro",
        input_text="transcribe request",
        output_text="transcript",
    )

    summary = meter.write_summary()

    assert summary["usage_events_count"] == 9
    assert summary["first_tts_billed_chars"] == 4
    assert summary["probe_tts_billed_chars"] == 4
    assert summary["post_tts_resynth_billed_chars"] == 6
    assert summary["post_edit_resynth_billed_chars"] == 4
    assert summary["post_edit_resynth_tts_billed_chars"] == 4
    assert summary["interactive_preview_billed_chars"] == 4
    assert summary["interactive_preview_tts_billed_chars"] == 4
    assert summary["tts_billed_chars"] == 18
    assert summary["tts_call_count"] == 5
    assert summary["tts_billed_chars_by_provider_model"] == {
        "minimax:speech-2.8-turbo": 12,
        "cosyvoice:cosyvoice-v2": 6,
        "minimax:speech-2.8-hd": 4,
    }
    assert summary["tts_call_count_by_provider_model"] == {
        "minimax:speech-2.8-turbo": 3,
        "cosyvoice:cosyvoice-v2": 1,
        "minimax:speech-2.8-hd": 1,
    }
    assert summary["llm_call_count"] == 4
    assert summary["llm_audio_input_bytes"] == 1024
    assert summary["llm_audio_input_seconds"] == 30.5
    assert summary["s2_pass1_llm_calls"] == 1
    assert summary["s5_rewrite_llm_calls"] == 1
    assert summary["pre_tts_rewrite_llm_calls"] == 1
    assert summary["probe_translate_llm_calls"] == 1
    assert summary["legacy_gemini_transcription_call_count"] == 1
    assert summary["pre_tts_rewrite_llm_tokens"] > 0

    summary_path = tmp_path / "metering" / "usage_summary.json"
    events_path = tmp_path / "metering" / "usage_events.jsonl"
    assert summary_path.is_file()
    assert events_path.is_file()
    assert json.loads(summary_path.read_text(encoding="utf-8"))["tts_billed_chars"] == 18

    reloaded = UsageMeter(tmp_path, job_id="job-usage-1")
    assert reloaded.summarize()["usage_events_count"] == 9


def test_usage_meter_deduplicates_event_ids_across_memory_and_reload(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path, job_id="job-usage-2")
    event = {
        "event_id": "fixed-event-id",
        "kind": "tts",
        "bucket": TTS_BUCKET_FIRST,
        "provider": "minimax",
        "model": "speech-2.8-turbo",
        "billed_chars": 10,
    }

    meter.record_event(event)
    meter.record_event(event)

    assert meter.summarize()["usage_events_count"] == 1
    assert meter.summarize()["tts_billed_chars"] == 10

    reloaded = UsageMeter(tmp_path, job_id="job-usage-2")
    assert reloaded.summarize()["usage_events_count"] == 1
    assert reloaded.summarize()["tts_billed_chars"] == 10


def test_record_voice_reuse_is_non_billable_voice_clone_audit(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path, job_id="job-voice-reuse")

    meter.record_voice_reuse(
        provider="minimax_voice_clone",
        voice_id="vt_existing",
        speaker_id="speaker_a",
        source_voice_id="vt_existing",
        match_confidence="user_confirmed",
        match_reason="studio_reuse_confirmed",
        extra={"event_id": "reuse-1"},
    )

    event = meter.events[0]
    assert event["kind"] == "voice_clone"
    assert event["model"] == "voice_reuse"
    assert event["billable"] is False
    assert event["clone_count"] == 0
    assert event["reuse"] is True
    summary = meter.summarize()
    assert summary["voice_clone_call_count"] == 1
    assert summary["voice_clone_success_call_count"] == 1
    assert summary["voice_clone_billable_count"] == 0
    assert summary["voice_clone_count_by_provider"] == {}


def test_record_voice_candidate_rejected_emits_non_billable_audit(tmp_path: Path) -> None:
    """Phase 4 (plan 2026-05-17-user-voice-candidate-first §计费和审计):
    when the pipeline offered a possible (non-strong) personal-voice
    candidate but the user picked a different voice, write a
    non-billable audit event so the support / dispute path can trace
    what was offered and rejected.

    Mirrors ``record_voice_reuse`` in:
      - billable=False
      - clone_count=0
      - kind=voice_clone (so existing summary aggregator catches it)
      - reuse=False (distinguishing from reuse path)
      - user_action=rejected (plan §计费和审计 enum)
    """
    meter = UsageMeter(tmp_path, job_id="job-voice-candidate-reject")

    meter.record_voice_candidate_rejected(
        provider="minimax_voice_clone",
        rejected_voice_id="vt_possible_one",
        speaker_id="speaker_a",
        rejected_match_confidence="weak",
        rejected_match_reason="cross_source_named_speaker_match",
        chosen_voice_id="moss_audio_official_zh",
        extra={
            "event_id": "voice_candidate_rejected:job-1:speaker_a:vt_possible_one",
            "source_user_voice_id": "7",
            "source_content_hash": "hash-xyz",
            "match_scope": "cross_source_named",
        },
    )

    event = meter.events[0]
    assert event["kind"] == "voice_clone"
    assert event["model"] == "voice_candidate_rejected"
    assert event["voice_id"] == "vt_possible_one"
    assert event["speaker_id"] == "speaker_a"
    assert event["billable"] is False
    assert event["clone_count"] == 0
    assert event["reuse"] is False
    assert event["user_action"] == "rejected"
    assert event["rejected_voice_id"] == "vt_possible_one"
    assert event["rejected_match_confidence"] == "weak"
    assert event["rejected_match_reason"] == "cross_source_named_speaker_match"
    assert event["chosen_voice_id"] == "moss_audio_official_zh"
    assert (
        event["billing_policy"] == "candidate_rejected_no_clone_charge"
    )
    # extra payload survives
    assert event["source_user_voice_id"] == "7"
    assert event["source_content_hash"] == "hash-xyz"
    assert event["match_scope"] == "cross_source_named"
    assert (
        event["event_id"]
        == "voice_candidate_rejected:job-1:speaker_a:vt_possible_one"
    )
    # Non-billable: audit event is recorded but doesn't move the
    # billable counter. The aggregate counter still ticks (this is
    # a voice_clone-bucketed event) so support can spot rejections
    # easily, but the billable count stays at 0.
    summary = meter.summarize()
    assert summary["voice_clone_call_count"] == 1
    assert summary["voice_clone_billable_count"] == 0


def test_record_voice_candidate_rejected_extra_cannot_clobber_payload_invariants(tmp_path: Path) -> None:
    """``record_voice_clone`` enforces ``if key in payload: continue``
    so extra cannot overwrite the core voice_clone payload — billable,
    clone_count, kind, voice_id, speaker_id, model, etc. This matches
    the existing ``record_voice_reuse`` contract (audit fields are
    intentionally caller-overridable on the wrapper helper, but the
    primary event invariants are locked)."""
    meter = UsageMeter(tmp_path, job_id="job-voice-candidate-reject-extra")

    meter.record_voice_candidate_rejected(
        provider="minimax_voice_clone",
        rejected_voice_id="vt_possible_two",
        speaker_id="speaker_b",
        rejected_match_confidence="medium",
        rejected_match_reason="same_source_other_speaker",
        chosen_voice_id="",
        # Attempt to spoof the core payload — must NOT succeed.
        extra={
            "billable": True,
            "clone_count": 99,
            "kind": "spoof",
            "model": "spoof_model",
            "voice_id": "spoofed_voice",
            "speaker_id": "spoofed_speaker",
            "event_id": "rej-2",
        },
    )

    event = meter.events[0]
    # Core voice_clone payload invariants are locked by record_voice_clone.
    assert event["billable"] is False
    assert event["clone_count"] == 0
    assert event["kind"] == "voice_clone"
    assert event["model"] == "voice_candidate_rejected"
    assert event["voice_id"] == "vt_possible_two"
    assert event["speaker_id"] == "speaker_b"
    # event_id is not in the primary payload dict, so it flows through
    # via the extra-merge and gets used as the dedup key.
    assert event["event_id"] == "rej-2"


# ===========================================================================
# Phase B: record_llm extra dict + failure attempt support
# (plan 2026-05-03 §B5)
# ===========================================================================


def test_record_llm_extra_fields_persist_to_event_payload(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path, job_id="job-extra-1")
    meter.record_llm(
        task="s3_translate",
        provider="gemini",
        model="gemini_pro",
        input_text="prompt",
        output_text="",
        attempt_label="primary",
        success=False,
        error="rate limited",
        extra={
            "error_class": "provider_error",
            "error_code": "rate_limit",
            "duration_ms": 4200,
            "fallback_to": "deepseek",
            "fallback_policy_source": "llm_registry_defaults",
            "provider_response_received": False,
        },
    )
    events = meter.events
    assert len(events) == 1
    ev = events[0]
    assert ev["success"] is False
    assert ev["error"] == "rate limited"
    assert ev["error_class"] == "provider_error"
    assert ev["error_code"] == "rate_limit"
    assert ev["duration_ms"] == 4200
    assert ev["fallback_to"] == "deepseek"
    assert ev["fallback_policy_source"] == "llm_registry_defaults"
    assert ev["provider_response_received"] is False


def test_record_llm_extra_cannot_clobber_core_fields(tmp_path: Path) -> None:
    """Plan §B5: ``extra`` is for diagnostic side-channel fields; it must not
    overwrite ``success``, ``error``, ``provider``, etc. — those have invariants
    (token coercion, error truncation) the rest of the code relies on."""
    meter = UsageMeter(tmp_path, job_id="job-extra-2")
    meter.record_llm(
        task="s3_translate",
        provider="gemini",
        model="gemini_pro",
        input_text="x",
        output_text="y",
        success=True,
        extra={
            "success": False,       # MUST NOT override
            "error": "fake",        # MUST NOT override
            "provider": "evil",     # MUST NOT override
            "task": "spoofed",      # MUST NOT override
        },
    )
    ev = meter.events[0]
    assert ev["success"] is True
    assert ev["error"] == ""
    assert ev["provider"] == "gemini"
    assert ev["task"] == "s3_translate"


def test_record_llm_extra_unknown_keys_dont_break_summarize(tmp_path: Path) -> None:
    """Forward compatibility: summarize() must ignore unknown event keys so
    schema can evolve without rewriting old events."""
    meter = UsageMeter(tmp_path, job_id="job-extra-3")
    meter.record_llm(
        task="s3_translate",
        provider="gemini",
        model="gemini_pro",
        input_text="prompt",
        output_text="resp",
        extra={"future_field": "any value", "another": [1, 2, 3]},
    )
    summary = meter.summarize()
    assert summary["llm_call_count"] == 1
    assert summary["s3_translate_llm_calls"] == 1


def test_record_llm_failure_attempts_persist_alongside_success(tmp_path: Path) -> None:
    """Failed attempts must coexist with success attempts in the JSONL — they
    are separate events, not merged. Plan §B7 / §10.1.4 audit chain depends on
    this being a count of attempts, not just successes."""
    meter = UsageMeter(tmp_path, job_id="job-attempts")
    meter.record_llm(
        task="s3_translate",
        provider="gemini",
        model="gemini_pro",
        input_text="p",
        attempt_label="primary",
        success=False,
        error="provider exploded",
        extra={"error_class": "provider_error"},
    )
    meter.record_llm(
        task="s3_translate",
        provider="deepseek",
        model="deepseek",
        input_text="p",
        output_text="ok",
        attempt_label="fallback_1",
        success=True,
    )
    summary = meter.summarize()
    assert summary["llm_call_count"] == 2
    assert summary["s3_translate_llm_calls"] == 2
    events = meter.events
    assert events[0]["attempt_label"] == "primary"
    assert events[0]["success"] is False
    assert events[1]["attempt_label"] == "fallback_1"
    assert events[1]["success"] is True


def test_record_llm_truncates_long_error_message(tmp_path: Path) -> None:
    meter = UsageMeter(tmp_path, job_id="job-trunc")
    long_err = "x" * 5000
    meter.record_llm(
        task="s3_translate",
        provider="gemini",
        model="gemini_pro",
        input_text="p",
        success=False,
        error=long_err,
    )
    ev = meter.events[0]
    assert len(ev["error"]) == 500
