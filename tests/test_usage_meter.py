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
