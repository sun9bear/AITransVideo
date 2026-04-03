from __future__ import annotations

from pathlib import Path

from .config_helpers import (
    _find_translation_model_label,
    _load_prompt_templates,
    _load_selected_translation_model_alias,
    _normalize_optional_text,
    build_provider_key_options,
    build_route_visualization,
    build_translation_model_options,
)
from .constants import SPEAKER_OPTIONS, WEB_UI_TITLE
from .project_resolver import _build_results_snapshot
from .voice_library import _build_voice_library_snapshot


def _get_cosyvoice_endpoint_modes() -> tuple[str, str]:
    """Return (runtime_mode, offline_mode) for snapshot."""
    try:
        from services.tts.cosyvoice_endpoint_config import (
            get_offline_endpoint_mode,
            get_runtime_endpoint_mode,
        )
        return get_runtime_endpoint_mode(), get_offline_endpoint_mode()
    except Exception:
        return "international", "mainland"


def build_web_ui_snapshot(
    *,
    manager: object,
) -> dict[str, object]:
    selected_alias = _load_selected_translation_model_alias(manager.config_path)  # type: ignore[union-attr]
    prompt_templates = _load_prompt_templates(
        manager.config_path  # type: ignore[union-attr]
    )
    job_snapshot = manager.snapshot()  # type: ignore[union-attr]
    results_snapshot = _build_results_snapshot(
        project_root=manager.project_root,  # type: ignore[union-attr]
        job_snapshot=job_snapshot,
    )
    project_dir_value = _normalize_optional_text(results_snapshot.get("project_dir"))
    voice_library_snapshot = _build_voice_library_snapshot(
        project_root=manager.project_root,  # type: ignore[union-attr]
        config_path=manager.config_path,  # type: ignore[union-attr]
        project_dir=(
            Path(project_dir_value).expanduser().resolve(strict=False)
            if project_dir_value is not None
            else None
        ),
        transcript_items=list(results_snapshot.get("transcript_review", {}).get("items", []))
        if isinstance(results_snapshot.get("transcript_review"), dict)
        else [],
        job_tts_provider=_normalize_optional_text(job_snapshot.get("tts_provider") if isinstance(job_snapshot, dict) else getattr(job_snapshot, "tts_provider", None)),
        job_service_mode=_normalize_optional_text(job_snapshot.get("service_mode") if isinstance(job_snapshot, dict) else getattr(job_snapshot, "service_mode", None)),
    )
    results_snapshot["voice_library"] = voice_library_snapshot
    return {
        "meta": {
            "title": WEB_UI_TITLE,
            "config_path": str(manager.config_path),  # type: ignore[union-attr]
            "project_root": str(manager.project_root),  # type: ignore[union-attr]
        },
        "settings": {
            "speaker_options": list(SPEAKER_OPTIONS),
            "translation_model_options": build_translation_model_options(
                config_path=manager.config_path  # type: ignore[union-attr]
            ),
            "s3_translate_route": build_route_visualization(
                "s3_translate",
                config_path=manager.config_path,  # type: ignore[union-attr]
            ),
            "provider_key_options": build_provider_key_options(
                config_path=manager.config_path  # type: ignore[union-attr]
            ),
            "selected_translation_model": selected_alias,
            "selected_translation_model_label": _find_translation_model_label(
                selected_alias,
                config_path=manager.config_path,  # type: ignore[union-attr]
            ),
            "speaker_infer_prompt_template": prompt_templates["s2_infer"]["template"],
            "speaker_infer_prompt_source": prompt_templates["s2_infer"]["source"],
            "translation_prompt_template": prompt_templates["s3_translate"]["template"],
            "translation_prompt_source": prompt_templates["s3_translate"]["source"],
            "rewrite_prompt_template": prompt_templates["s5_rewrite"]["template"],
            "rewrite_prompt_source": prompt_templates["s5_rewrite"]["source"],
            "cosyvoice_runtime_endpoint_mode": _get_cosyvoice_endpoint_modes()[0],
            "cosyvoice_offline_endpoint_mode": _get_cosyvoice_endpoint_modes()[1],
        },
        "job": job_snapshot,
        "results": results_snapshot,
    }
