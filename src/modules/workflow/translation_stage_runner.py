from dataclasses import dataclass, field

from core.enums import StageStatus
from core.exceptions import TranslationProviderUnavailableError, WorkflowError
from core.models import SubtitleLine
from core.retry import (
    build_retry_audit_payload,
    merge_retry_audit_payload,
    read_provider_retry_report,
    reset_provider_retry_report,
)
from modules.translation.providers import TranslationProvider, classify_translation_error
from modules.translation.sanitizer import SanitizedBatchResult
from modules.translation.translator import TranslationPipeline
from modules.translation.validators import validate_source_lines
from modules.workflow.restore_policy import build_cache_restore_audit
from modules.workflow.stage_helpers import build_artifacts_payload, read_stage_payload, resolve_cache_execution_mode
from services.cache_manager import CacheManager
from services.state_manager import StateManager


@dataclass(slots=True)
class TranslationStageRunnerConfig:
    provider_name: str
    target_language: str = "zh-CN"
    model_name: str | None = None
    provider_mode: str = "mock"
    version_context: dict[str, object] = field(default_factory=dict)
    fallback_applied: bool = False
    fallback_reason: str | None = None
    fallback_stage: str | None = None
    runtime_fallback_enabled: bool = False
    fallback_from: str | None = None
    fallback_to: str | None = None


class TranslationStageRunner:
    """Execute the translation stage while preserving workflow state and cache semantics."""

    def __init__(
        self,
        translation_pipeline: TranslationPipeline,
        state_manager: StateManager,
        cache_manager: CacheManager,
        config: TranslationStageRunnerConfig,
    ) -> None:
        self.translation_pipeline = translation_pipeline
        self.state_manager = state_manager
        self.cache_manager = cache_manager
        self.config = config

    def run(self, subtitle_lines: list[SubtitleLine]) -> list[SubtitleLine]:
        stage_name = "translation"
        previous_stage = self.state_manager.get_stage(stage_name)
        previous_payload = read_stage_payload(previous_stage) if previous_stage is not None else None
        source_input_hash = self._resolve_source_input_hash(subtitle_lines)
        self._reset_retry_reports()
        self.state_manager.set_stage(
            stage_name,
            StageStatus.RUNNING,
            self._build_provider_run_report(),
        )
        try:
            validate_source_lines(subtitle_lines)
            translated_lines: list[SubtitleLine] = []
            cache_hit_batches = 0
            batch_hashes: list[str] = []
            reused_batch_hashes: list[str] = []
            sanitizer_action_counts: dict[str, int] = {}
            sanitized_line_count = 0
            runtime_fallback_batches = 0
            last_fallback_event: dict[str, object] | None = None

            for chunk in self.translation_pipeline.router.route(subtitle_lines):
                primary_hash = self._build_batch_hash(
                    chunk,
                    provider_name=self.config.provider_name,
                    model_name=self.config.model_name,
                    version_context=self.config.version_context,
                )
                cached_entry = self.cache_manager.get_entry("translation_batch", primary_hash)
                cached_texts = self._read_cached_translations(cached_entry)
                if cached_texts is not None:
                    processed_batch = self.translation_pipeline.process_batch_output(
                        chunk,
                        cached_texts,
                        sanitize=False,
                    )
                    cache_hit_batches += 1
                    batch_hashes.append(primary_hash)
                    reused_batch_hashes.append(primary_hash)
                else:
                    processed_batch, used_hash, fallback_event, fallback_cache_hit = self._translate_batch_with_fallback(
                        chunk,
                    )
                    batch_hashes.append(used_hash)
                    self._merge_action_counts(sanitizer_action_counts, processed_batch.action_counts)
                    sanitized_line_count += processed_batch.changed_line_count
                    if fallback_event is not None:
                        runtime_fallback_batches += 1
                        last_fallback_event = fallback_event
                    if fallback_cache_hit:
                        cache_hit_batches += 1
                        reused_batch_hashes.append(used_hash)

                translated_lines.extend(self.translation_pipeline.merge_batch(chunk, processed_batch.texts))

            execution_mode = resolve_cache_execution_mode(cache_hit_batches, len(batch_hashes))
            retry_report = self._collect_retry_report()
            fallback_trigger = None
            fallback_from = None
            fallback_to = None
            fallback_stage = self.config.fallback_stage
            if last_fallback_event is not None:
                fallback_trigger = last_fallback_event.get("fallback_trigger")
                fallback_from = last_fallback_event.get("fallback_from")
                fallback_to = last_fallback_event.get("fallback_to")
                fallback_stage = last_fallback_event.get("fallback_stage", fallback_stage)
            restore_audit = build_cache_restore_audit(
                stage_name=stage_name,
                previous_payload=previous_payload,
                source_input_hash=source_input_hash,
                context_fields={
                    "provider_name": self.config.provider_name,
                    "model_name": self.config.model_name,
                    "target_language": self.config.target_language,
                    "version_context": self.config.version_context,
                },
                total_units=len(batch_hashes),
                cache_hits=cache_hit_batches,
                artifact_paths=[],
                reused_artifacts=reused_batch_hashes,
            )

            payload = self._build_provider_run_report(
                execution_mode=execution_mode,
                fallback_applied=self.config.fallback_applied or runtime_fallback_batches > 0,
                fallback_trigger=fallback_trigger if isinstance(fallback_trigger, str) else None,
                fallback_from=fallback_from if isinstance(fallback_from, str) else None,
                fallback_to=fallback_to if isinstance(fallback_to, str) else None,
                retry_attempted=bool(retry_report["retry_attempted"]),
                retry_count=int(retry_report["retry_count"]),
                retry_candidate=retry_report["retry_candidate"],
            )
            text_layer_summary = self._summarize_text_layers(translated_lines)
            payload.update(
                {
                    **restore_audit,
                    "target_language": self.config.target_language,
                    "line_count": len(translated_lines),
                    "text_layer_summary": text_layer_summary,
                    "batch_count": len(batch_hashes),
                    "cache_hit_batches": cache_hit_batches,
                    "fallback_stage": fallback_stage,
                    "sanitizer_summary": {
                        "sanitized_line_count": sanitized_line_count,
                        "action_counts": sanitizer_action_counts,
                    },
                    "artifacts": build_artifacts_payload(
                        kind="translation_batches",
                        file_paths=[],
                        extra={
                            "batch_hashes": batch_hashes,
                            "version_context": self.config.version_context,
                        },
                    ),
                    "runtime_fallback_enabled": self.config.runtime_fallback_enabled,
                    "runtime_fallback_batches": runtime_fallback_batches,
                }
            )

            self.state_manager.set_stage(stage_name, StageStatus.DONE, payload)
            return translated_lines
        except Exception as exc:
            error_info = classify_translation_error(exc)
            retry_report = self._collect_retry_report()
            restore_audit = build_cache_restore_audit(
                stage_name=stage_name,
                previous_payload=previous_payload,
                source_input_hash=source_input_hash,
                context_fields={
                    "provider_name": self.config.provider_name,
                    "model_name": self.config.model_name,
                    "target_language": self.config.target_language,
                    "version_context": self.config.version_context,
                },
                total_units=0,
                cache_hits=0,
                artifact_paths=[],
                reused_artifacts=[],
            )
            payload = self._build_provider_run_report(
                fallback_from=self.config.fallback_from,
                fallback_to=self.config.fallback_to,
                error_type=str(error_info["error_type"]),
                retry_attempted=bool(retry_report["retry_attempted"]),
                retry_count=int(retry_report["retry_count"]),
                retry_candidate=retry_report["retry_candidate"]
                if retry_report["retry_candidate"] is not None
                else bool(error_info["retry_candidate"]),
                final_error_type=str(error_info["error_type"]),
                final_error_message=str(exc),
            )
            payload.update(
                {
                    **restore_audit,
                    "target_language": self.config.target_language,
                    "text_layer_summary": {
                        "cn_line_count": 0,
                    },
                    "fallback_stage": self.config.fallback_stage,
                    "runtime_fallback_enabled": self.config.runtime_fallback_enabled,
                }
            )
            self.state_manager.set_stage(
                stage_name,
                StageStatus.FAILED,
                payload=payload,
                error_message=str(exc),
            )
            raise WorkflowError("Translation stage failed.") from exc

    def _build_provider_run_report(
        self,
        *,
        execution_mode: str | None = None,
        fallback_applied: bool | None = None,
        fallback_trigger: str | None = None,
        fallback_from: str | None = None,
        fallback_to: str | None = None,
        error_type: str | None = None,
        retry_attempted: bool = False,
        retry_count: int = 0,
        retry_candidate: bool | None = None,
        final_error_type: str | None = None,
        final_error_message: str | None = None,
    ) -> dict[str, object]:
        return {
            "provider": self.config.provider_name,
            "provider_name": self.config.provider_name,
            "provider_mode": self.config.provider_mode,
            "model_name": self.config.model_name,
            "version_context": self.config.version_context,
            "execution_mode": execution_mode,
            "fallback_applied": self.config.fallback_applied if fallback_applied is None else fallback_applied,
            "fallback_reason": self.config.fallback_reason,
            "fallback_trigger": fallback_trigger,
            "fallback_from": fallback_from,
            "fallback_to": fallback_to,
            "retry_attempted": retry_attempted,
            "retry_count": retry_count,
            "error_type": error_type,
            "retry_candidate": retry_candidate,
            "final_error_type": final_error_type,
            "final_error_message": final_error_message,
        }

    def _translate_batch_with_fallback(
        self,
        chunk: list[SubtitleLine],
    ) -> tuple[SanitizedBatchResult, str, dict[str, object] | None, bool]:
        try:
            processed_batch = self._translate_with_provider(
                translator=self.translation_pipeline.translator,
                chunk=chunk,
                sanitize=True,
            )
            used_hash = self._build_batch_hash(
                chunk,
                provider_name=self.config.provider_name,
                model_name=self.config.model_name,
                version_context=self.config.version_context,
            )
            self._store_translation_cache(
                used_hash,
                chunk=chunk,
                translated_texts=processed_batch.texts,
                provider_name=self.config.provider_name,
                provider_mode=self.config.provider_mode,
                model_name=self.config.model_name,
                version_context=self.config.version_context,
                sanitizer_action_counts=processed_batch.action_counts,
            )
            return processed_batch, used_hash, None, False
        except Exception as exc:
            if not self._should_runtime_fallback(exc):
                raise
            fallback_provider = self.translation_pipeline.fallback_translator
            if fallback_provider is None:
                raise

            fallback_provider_name = self.config.fallback_to or type(fallback_provider).__name__
            fallback_model_name = None
            fallback_version_context = self._get_provider_cache_context(fallback_provider)
            fallback_hash = self._build_batch_hash(
                chunk,
                provider_name=fallback_provider_name,
                model_name=fallback_model_name,
                version_context=fallback_version_context,
            )
            cached_fallback_entry = self.cache_manager.get_entry("translation_batch", fallback_hash)
            cached_fallback_texts = self._read_cached_translations(cached_fallback_entry)
            if cached_fallback_texts is not None:
                processed_batch = self.translation_pipeline.process_batch_output(
                    chunk,
                    cached_fallback_texts,
                    sanitize=False,
                )
                fallback_cache_hit = True
            else:
                processed_batch = self._translate_with_provider(
                    translator=fallback_provider,
                    chunk=chunk,
                    sanitize=True,
                )
                self._store_translation_cache(
                    fallback_hash,
                    chunk=chunk,
                    translated_texts=processed_batch.texts,
                    provider_name=fallback_provider_name,
                    provider_mode="mock_runtime_fallback",
                    model_name=fallback_model_name,
                    version_context=fallback_version_context,
                    sanitizer_action_counts=processed_batch.action_counts,
                )
                fallback_cache_hit = False

            fallback_event = {
                "fallback_trigger": "runtime_provider_unavailable",
                "fallback_from": self.config.fallback_from or self.config.provider_name,
                "fallback_to": fallback_provider_name,
                "fallback_stage": "runtime",
            }
            return processed_batch, fallback_hash, fallback_event, fallback_cache_hit

    def _translate_with_provider(
        self,
        translator: TranslationProvider,
        chunk: list[SubtitleLine],
        sanitize: bool,
    ) -> SanitizedBatchResult:
        raw_translated_texts = translator.translate_batch(chunk)
        return self.translation_pipeline.process_batch_output(
            chunk,
            raw_translated_texts,
            sanitize=sanitize,
        )

    def _build_batch_hash(
        self,
        chunk: list[SubtitleLine],
        provider_name: str,
        model_name: str | None,
        version_context: dict[str, object],
    ) -> str:
        return self.cache_manager.build_translation_batch_hash(
            chunk,
            provider_name=provider_name,
            target_language=self.config.target_language,
            model_name=model_name,
            version_context=version_context,
        )

    def _store_translation_cache(
        self,
        cache_key: str,
        chunk: list[SubtitleLine],
        translated_texts: list[str],
        provider_name: str,
        provider_mode: str,
        model_name: str | None,
        version_context: dict[str, object],
        sanitizer_action_counts: dict[str, int],
    ) -> None:
        self.cache_manager.set_entry(
            "translation_batch",
            cache_key,
            {
                "line_count": len(chunk),
                "translated_texts": translated_texts,
                "provider_name": provider_name,
                "model_name": model_name,
                "target_language": self.config.target_language,
                "provider_mode": provider_mode,
                "version_context": version_context,
                "sanitizer_action_counts": sanitizer_action_counts,
            },
        )

    def _read_cached_translations(self, cached_entry: dict[str, object] | None) -> list[str] | None:
        if cached_entry is None:
            return None
        payload = cached_entry.get("payload", {})
        translated_texts = payload.get("translated_texts")
        if not isinstance(translated_texts, list):
            return None
        if not all(isinstance(text, str) for text in translated_texts):
            return None
        return translated_texts

    def _merge_action_counts(
        self,
        target: dict[str, int],
        source: dict[str, int],
    ) -> None:
        for action, count in source.items():
            target[action] = target.get(action, 0) + count

    def _should_runtime_fallback(self, exc: Exception) -> bool:
        return (
            self.config.runtime_fallback_enabled
            and isinstance(exc, TranslationProviderUnavailableError)
            and self.translation_pipeline.fallback_translator is not None
        )

    def _get_provider_cache_context(self, provider: TranslationProvider) -> dict[str, object]:
        context_getter = getattr(provider, "get_cache_context", None)
        if callable(context_getter):
            context = context_getter()
            if isinstance(context, dict):
                return context
        return {}

    def _reset_retry_reports(self) -> None:
        reset_provider_retry_report(self.translation_pipeline.translator)
        reset_provider_retry_report(self.translation_pipeline.fallback_translator)

    def _collect_retry_report(self) -> dict[str, object]:
        retry_report = build_retry_audit_payload()
        retry_report = merge_retry_audit_payload(
            retry_report,
            read_provider_retry_report(self.translation_pipeline.translator),
        )
        retry_report = merge_retry_audit_payload(
            retry_report,
            read_provider_retry_report(self.translation_pipeline.fallback_translator),
        )
        return retry_report

    def _resolve_source_input_hash(self, subtitle_lines: list[SubtitleLine]) -> str:
        return self.cache_manager.build_input_hash(subtitle_lines)

    def _summarize_text_layers(self, lines: list[SubtitleLine]) -> dict[str, int]:
        return {
            "cn_line_count": sum(1 for line in lines if bool(line.cn_text.strip())),
        }
