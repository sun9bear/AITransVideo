from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any


TTS_BUCKET_FIRST = "first_tts"
TTS_BUCKET_PROBE = "probe_tts"
TTS_BUCKET_POST_TTS_RESYNTH = "post_tts_resynth"
TTS_BUCKET_POST_EDIT_RESYNTH = "post_edit_resynth"
TTS_BUCKET_INTERACTIVE_PREVIEW = "interactive_preview"
VOICE_CLONE_BUCKET = "voice_clone"

_JOB_TTS_BUCKETS = {
    TTS_BUCKET_FIRST,
    TTS_BUCKET_PROBE,
    TTS_BUCKET_POST_TTS_RESYNTH,
    TTS_BUCKET_POST_EDIT_RESYNTH,
}


def estimate_text_tokens(text: str | None) -> int:
    """Cheap, deterministic estimate for metering when provider usage is absent."""
    if not text:
        return 0
    return max(1, int(math.ceil(len(str(text).encode("utf-8")) / 4)))


def _safe_key(value: object) -> str:
    raw = str(value or "").strip().lower()
    chars = [ch if ch.isalnum() else "_" for ch in raw]
    key = "".join(chars).strip("_")
    while "__" in key:
        key = key.replace("__", "_")
    return key or "unknown"


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class UsageMeter:
    """Append-only per-job LLM/TTS usage recorder.

    This is intentionally sidecar state. Recording failures are warnings, not
    pipeline failures.
    """

    def __init__(
        self,
        project_dir: str | Path | None,
        *,
        job_id: str | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve(strict=False) if project_dir else None
        self.job_id = str(job_id or "").strip()
        self.events_path = (
            self.project_dir / "metering" / "usage_events.jsonl"
            if self.project_dir is not None
            else None
        )
        self.summary_path = (
            self.project_dir / "metering" / "usage_summary.json"
            if self.project_dir is not None
            else None
        )
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._event_ids: set[str] = set()
        self.reload()

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]

    def reload(self) -> None:
        if self.events_path is None or not self.events_path.is_file():
            return
        loaded: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        try:
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id") or "")
                if event_id and event_id in event_ids:
                    continue
                loaded.append(event)
                if event_id:
                    event_ids.add(event_id)
        except Exception as exc:
            print(f"[metering] usage event reload skipped: {exc}", flush=True)
            return
        with self._lock:
            self._events = loaded
            self._event_ids = event_ids

    def record_tts(
        self,
        *,
        bucket: str,
        provider: str,
        model: str = "",
        text: str = "",
        billed_chars: int = 0,
        segment_id: int | str | None = None,
        voice_id: str = "",
        selected_voice: str = "",
        duration_ms: int | None = None,
        fallback_used_provider: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        bucket_key = _safe_key(bucket)
        payload: dict[str, Any] = {
            "kind": "tts",
            "bucket": bucket_key,
            "provider": str(provider or ""),
            "model": str(model or ""),
            "segment_id": segment_id,
            "voice_id": str(voice_id or ""),
            "selected_voice": str(selected_voice or ""),
            "input_chars": len(text or ""),
            "billed_chars": max(0, _coerce_int(billed_chars)),
            "duration_ms": max(0, _coerce_int(duration_ms)),
            "fallback_used_provider": str(fallback_used_provider or ""),
        }
        if extra:
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        self.record_event(payload)

    def record_llm(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        model_id: str = "",
        phase: str = "",
        input_text: str = "",
        output_text: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        input_audio_tokens: int | None = None,
        token_count_source: str | None = None,
        audio_input_bytes: int = 0,
        audio_input_count: int = 0,
        audio_input_seconds: float = 0.0,
        attempt_label: str = "",
        success: bool = True,
        error: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append a single LLM attempt event.

        Plan 2026-05-03 §B5: ``extra`` carries optional structured fields like
        ``error_class`` / ``error_code`` / ``provider_response_received`` /
        ``fallback_from`` / ``fallback_to`` / ``duration_ms`` / ``prompt_hash``
        without forcing every caller to deal with new positional kwargs.
        Unknown keys flow through to disk as-is; ``summarize()`` ignores them.
        Core fields are never overwritten by ``extra``.
        """
        in_tokens = (
            max(0, _coerce_int(input_tokens))
            if input_tokens is not None
            else estimate_text_tokens(input_text)
        )
        out_tokens = (
            max(0, _coerce_int(output_tokens))
            if output_tokens is not None
            else estimate_text_tokens(output_text)
        )
        payload: dict[str, Any] = {
            "kind": "llm",
            "task": _safe_key(task),
            "phase": _safe_key(phase) if phase else "",
            "provider": str(provider or ""),
            "model": str(model or ""),
            "model_id": str(model_id or model or ""),
            "attempt_label": str(attempt_label or ""),
            "input_text_chars": len(input_text or ""),
            "output_text_chars": len(output_text or ""),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "token_count_source": (
                token_count_source
                or ("provider_usage" if input_tokens is not None else "estimated_text_length")
            ),
            "audio_input_bytes": max(0, _coerce_int(audio_input_bytes)),
            "audio_input_count": max(0, _coerce_int(audio_input_count)),
            "audio_input_seconds": max(0.0, _coerce_float(audio_input_seconds)),
            "success": bool(success),
            "error": str(error or "")[:500],
        }
        # Provider-reported usage components (plan 2026-05-27 PR 2). Only added
        # when the caller actually parsed them from the provider response, so
        # estimate-only events keep their historical payload shape.
        if cached_input_tokens is not None:
            payload["cached_input_tokens"] = max(0, _coerce_int(cached_input_tokens))
        if input_audio_tokens is not None:
            payload["input_audio_tokens"] = max(0, _coerce_int(input_audio_tokens))
        if extra:
            for key, value in extra.items():
                if key in payload:
                    # Never let extra clobber core metering fields — those have
                    # invariants (e.g. token coercion, error truncation).
                    continue
                payload[key] = value
        self.record_event(payload)

    def record_voice_clone(
        self,
        *,
        provider: str,
        model: str = "voice_clone",
        voice_id: str = "",
        speaker_id: str = "",
        source_audio_seconds: float = 0.0,
        source_audio_bytes: int = 0,
        selected_segment_count: int = 0,
        clone_count: int = 1,
        billable: bool = True,
        success: bool = True,
        error: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "kind": "voice_clone",
            "bucket": VOICE_CLONE_BUCKET,
            "provider": str(provider or ""),
            "model": str(model or "voice_clone"),
            "voice_id": str(voice_id or ""),
            "speaker_id": str(speaker_id or ""),
            "source_audio_seconds": max(0.0, _coerce_float(source_audio_seconds)),
            "source_audio_bytes": max(0, _coerce_int(source_audio_bytes)),
            "selected_segment_count": max(0, _coerce_int(selected_segment_count)),
            "clone_count": max(0, _coerce_int(clone_count)),
            "billable": bool(billable),
            "success": bool(success),
            "error": str(error or "")[:500],
        }
        if extra:
            for key, value in extra.items():
                if key in payload:
                    continue
                payload[key] = value
        self.record_event(payload)

    def record_voice_reuse(
        self,
        *,
        provider: str,
        voice_id: str,
        speaker_id: str = "",
        source_voice_id: str = "",
        match_confidence: str = "",
        match_reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        reuse_extra: dict[str, Any] = {
            "reuse": True,
            "source_voice_id": str(source_voice_id or voice_id or ""),
            "match_confidence": str(match_confidence or ""),
            "match_reason": str(match_reason or ""),
            "billing_policy": "reuse_existing_user_voice_no_clone_charge",
        }
        if extra:
            reuse_extra.update(extra)
        self.record_voice_clone(
            provider=provider,
            model="voice_reuse",
            voice_id=voice_id,
            speaker_id=speaker_id,
            source_audio_seconds=0.0,
            source_audio_bytes=0,
            selected_segment_count=0,
            clone_count=0,
            billable=False,
            success=True,
            extra=reuse_extra,
        )

    def record_voice_candidate_rejected(
        self,
        *,
        provider: str,
        rejected_voice_id: str,
        speaker_id: str = "",
        rejected_match_confidence: str = "",
        rejected_match_reason: str = "",
        chosen_voice_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Phase 4 audit (plan 2026-05-17-user-voice-candidate-first
        §计费和审计 ``smart_possible_user_voice_match_rejected`` /
        ``studio_user_voice_candidate_rejected``): record that the
        pipeline offered a possible (non-strong) personal-voice
        candidate but the user picked a different voice (official
        catalog OR a new clone).

        Non-billable (mirrors ``record_voice_reuse``): this is an
        audit trail of the user's explicit rejection, NOT a billable
        event. ``billing_policy`` is set to
        ``candidate_rejected_no_clone_charge`` so the audit ledger
        consumer can distinguish from the reuse path.

        Provider arg is the personal-voice's clone provider (whatever
        ``UserVoice.provider`` holds), not the picked voice's
        provider, so the audit links back to the candidate that was
        offered.
        """
        reject_extra: dict[str, Any] = {
            "reuse": False,
            "rejected_voice_id": str(rejected_voice_id or ""),
            "rejected_match_confidence": str(rejected_match_confidence or ""),
            "rejected_match_reason": str(rejected_match_reason or ""),
            "chosen_voice_id": str(chosen_voice_id or ""),
            "user_action": "rejected",
            "billing_policy": "candidate_rejected_no_clone_charge",
        }
        if extra:
            reject_extra.update(extra)
        self.record_voice_clone(
            provider=provider,
            model="voice_candidate_rejected",
            voice_id=rejected_voice_id,
            speaker_id=speaker_id,
            source_audio_seconds=0.0,
            source_audio_bytes=0,
            selected_segment_count=0,
            clone_count=0,
            billable=False,
            success=True,
            extra=reject_extra,
        )

    def record_event(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        payload["event_id"] = event_id
        payload.setdefault("created_at_ms", int(time.time() * 1000))
        if self.job_id:
            payload.setdefault("job_id", self.job_id)

        with self._lock:
            if event_id in self._event_ids:
                return
            self._events.append(payload)
            self._event_ids.add(event_id)
            events_path = self.events_path

        if events_path is None:
            return
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                f.write("\n")
        except Exception as exc:
            print(f"[metering] usage event append skipped: {exc}", flush=True)

    def summarize(self) -> dict[str, Any]:
        events = self.events
        summary: dict[str, Any] = {
            "usage_events_count": len(events),
            "usage_metering_version": 1,
        }

        llm_calls = 0
        llm_input_tokens = 0
        llm_output_tokens = 0
        llm_audio_bytes = 0
        llm_audio_seconds = 0.0
        llm_task_calls: dict[str, int] = {}
        llm_task_input_tokens: dict[str, int] = {}
        llm_task_output_tokens: dict[str, int] = {}
        llm_phase_calls: dict[str, int] = {}
        llm_phase_input_tokens: dict[str, int] = {}
        llm_phase_output_tokens: dict[str, int] = {}
        llm_model_calls: dict[str, int] = {}

        tts_total = 0
        tts_calls = 0
        tts_by_bucket: dict[str, int] = {}
        tts_calls_by_bucket: dict[str, int] = {}
        tts_by_provider: dict[str, int] = {}
        tts_calls_by_provider: dict[str, int] = {}
        tts_by_provider_model: dict[str, int] = {}
        tts_calls_by_provider_model: dict[str, int] = {}
        voice_clone_calls = 0
        voice_clone_success_calls = 0
        voice_clone_billable_count = 0
        voice_clone_source_audio_seconds = 0.0
        voice_clone_by_provider: dict[str, int] = {}

        legacy_gemini_transcription_calls = 0

        for event in events:
            kind = event.get("kind")
            if kind == "llm":
                llm_calls += 1
                task = _safe_key(event.get("task"))
                phase = _safe_key(event.get("phase")) if event.get("phase") else ""
                provider = _safe_key(event.get("provider"))
                model_id = str(event.get("model_id") or event.get("model") or "")
                model_key = f"{provider}:{model_id or 'unknown'}:{task}"
                input_tokens = max(0, _coerce_int(event.get("input_tokens")))
                output_tokens = max(0, _coerce_int(event.get("output_tokens")))
                llm_input_tokens += input_tokens
                llm_output_tokens += output_tokens
                llm_audio_bytes += max(0, _coerce_int(event.get("audio_input_bytes")))
                llm_audio_seconds += max(0.0, _coerce_float(event.get("audio_input_seconds")))
                llm_task_calls[task] = llm_task_calls.get(task, 0) + 1
                llm_task_input_tokens[task] = llm_task_input_tokens.get(task, 0) + input_tokens
                llm_task_output_tokens[task] = llm_task_output_tokens.get(task, 0) + output_tokens
                llm_model_calls[model_key] = llm_model_calls.get(model_key, 0) + 1
                if phase:
                    llm_phase_calls[phase] = llm_phase_calls.get(phase, 0) + 1
                    llm_phase_input_tokens[phase] = llm_phase_input_tokens.get(phase, 0) + input_tokens
                    llm_phase_output_tokens[phase] = llm_phase_output_tokens.get(phase, 0) + output_tokens
                if task == "s1_gemini_transcribe":
                    legacy_gemini_transcription_calls += 1
            elif kind == "tts":
                bucket = _safe_key(event.get("bucket"))
                provider = _safe_key(event.get("provider"))
                model = str(event.get("model") or "").strip().lower() or "unknown"
                provider_model = f"{provider}:{model}"
                billed = max(0, _coerce_int(event.get("billed_chars")))
                tts_calls += 1
                if bucket in _JOB_TTS_BUCKETS:
                    tts_total += billed
                tts_by_bucket[bucket] = tts_by_bucket.get(bucket, 0) + billed
                tts_calls_by_bucket[bucket] = tts_calls_by_bucket.get(bucket, 0) + 1
                tts_by_provider[provider] = tts_by_provider.get(provider, 0) + billed
                tts_calls_by_provider[provider] = tts_calls_by_provider.get(provider, 0) + 1
                tts_by_provider_model[provider_model] = (
                    tts_by_provider_model.get(provider_model, 0) + billed
                )
                tts_calls_by_provider_model[provider_model] = (
                    tts_calls_by_provider_model.get(provider_model, 0) + 1
                )
            elif kind == "voice_clone":
                provider = _safe_key(event.get("provider"))
                clone_count = max(0, _coerce_int(event.get("clone_count")))
                if clone_count <= 0 and bool(event.get("success", True)):
                    clone_count = 1
                voice_clone_calls += 1
                if bool(event.get("success", True)):
                    voice_clone_success_calls += 1
                if bool(event.get("billable", True)):
                    voice_clone_billable_count += clone_count
                    voice_clone_by_provider[provider] = voice_clone_by_provider.get(provider, 0) + clone_count
                voice_clone_source_audio_seconds += max(
                    0.0,
                    _coerce_float(event.get("source_audio_seconds")),
                )

        summary.update({
            "llm_call_count": llm_calls,
            "llm_input_tokens": llm_input_tokens,
            "llm_output_tokens": llm_output_tokens,
            "llm_total_tokens": llm_input_tokens + llm_output_tokens,
            "llm_audio_input_bytes": llm_audio_bytes,
            "llm_audio_input_seconds": round(llm_audio_seconds, 3),
            "llm_task_call_distribution": llm_task_calls,
            "llm_model_call_distribution": llm_model_calls,
            "tts_call_count": tts_calls,
            "tts_billed_chars": tts_total,
            "tts_billed_chars_by_bucket": tts_by_bucket,
            "tts_call_count_by_bucket": tts_calls_by_bucket,
            "tts_billed_chars_by_provider": tts_by_provider,
            "tts_call_count_by_provider": tts_calls_by_provider,
            "tts_billed_chars_by_provider_model": tts_by_provider_model,
            "tts_call_count_by_provider_model": tts_calls_by_provider_model,
            "voice_clone_call_count": voice_clone_calls,
            "voice_clone_success_call_count": voice_clone_success_calls,
            "voice_clone_billable_count": voice_clone_billable_count,
            "voice_clone_count_by_provider": voice_clone_by_provider,
            "voice_clone_source_audio_seconds": round(voice_clone_source_audio_seconds, 3),
            "legacy_gemini_transcription_call_count": legacy_gemini_transcription_calls,
        })

        for bucket in (
            TTS_BUCKET_FIRST,
            TTS_BUCKET_PROBE,
            TTS_BUCKET_POST_TTS_RESYNTH,
            TTS_BUCKET_POST_EDIT_RESYNTH,
            TTS_BUCKET_INTERACTIVE_PREVIEW,
        ):
            summary[f"{bucket}_billed_chars"] = tts_by_bucket.get(bucket, 0)
            summary[f"{bucket}_call_count"] = tts_calls_by_bucket.get(bucket, 0)
        summary["post_edit_resynth_tts_billed_chars"] = tts_by_bucket.get(
            TTS_BUCKET_POST_EDIT_RESYNTH,
            0,
        )
        summary["post_edit_resynth_tts_call_count"] = tts_calls_by_bucket.get(
            TTS_BUCKET_POST_EDIT_RESYNTH,
            0,
        )
        summary["interactive_preview_tts_billed_chars"] = tts_by_bucket.get(
            TTS_BUCKET_INTERACTIVE_PREVIEW,
            0,
        )
        summary["interactive_preview_tts_call_count"] = tts_calls_by_bucket.get(
            TTS_BUCKET_INTERACTIVE_PREVIEW,
            0,
        )

        for task, calls in llm_task_calls.items():
            input_tokens = llm_task_input_tokens.get(task, 0)
            output_tokens = llm_task_output_tokens.get(task, 0)
            summary[f"{task}_llm_calls"] = calls
            summary[f"{task}_llm_input_tokens"] = input_tokens
            summary[f"{task}_llm_output_tokens"] = output_tokens
            summary[f"{task}_llm_tokens"] = input_tokens + output_tokens

        for phase, calls in llm_phase_calls.items():
            input_tokens = llm_phase_input_tokens.get(phase, 0)
            output_tokens = llm_phase_output_tokens.get(phase, 0)
            summary[f"{phase}_llm_calls"] = calls
            summary[f"{phase}_llm_input_tokens"] = input_tokens
            summary[f"{phase}_llm_output_tokens"] = output_tokens
            summary[f"{phase}_llm_tokens"] = input_tokens + output_tokens

        return summary

    def write_summary(self) -> dict[str, Any]:
        summary = self.summarize()
        if self.summary_path is None:
            return summary
        try:
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.summary_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(self.summary_path)
        except Exception as exc:
            print(f"[metering] usage summary write skipped: {exc}", flush=True)
        return summary
