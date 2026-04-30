from __future__ import annotations

from pathlib import Path

import pytest

from services.jobs.models import JobRecord
from services.jobs.review_actions import (
    approve_voice_selection,
    resolve_minimax_tts_model_from_voice_selection,
)
from services.jobs.service import JobService
from services.jobs.store import JobStore
from services.review_state import (
    REVIEW_STATUS_PENDING,
    VOICE_SELECTION_REVIEW_STAGE,
    ReviewStateManager,
)


def _job_record(
    *,
    job_id: str = "job_voice_model",
    tts_model: str = "speech-2.8-turbo",
) -> JobRecord:
    return JobRecord.from_dict(
        {
            "job_id": job_id,
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://youtube.example/watch?v=model",
            "output_target": "editor",
            "speakers": "auto",
            "status": "waiting_for_review",
            "current_stage": "voice_selection_review",
            "progress_message": "waiting",
            "created_at": "2026-04-29T00:00:00+00:00",
            "updated_at": "2026-04-29T00:00:00+00:00",
            "service_mode": "studio",
            "tts_provider": "minimax",
            "tts_model": tts_model,
            "requires_review": True,
        }
    )


def test_resolve_minimax_tts_model_from_ui_speaker_choices() -> None:
    assert resolve_minimax_tts_model_from_voice_selection(
        [
            {"speaker_id": "speaker_a", "tts_provider": "minimax", "minimax_model": "turbo"},
            {"speaker_id": "speaker_b", "tts_provider": "minimax", "minimax_model": "hd"},
        ]
    ) == "speech-2.8-hd"
    assert resolve_minimax_tts_model_from_voice_selection(
        [{"speaker_id": "speaker_a", "tts_provider": "minimax"}]
    ) == "speech-2.8-turbo"
    assert resolve_minimax_tts_model_from_voice_selection(
        [{"speaker_id": "speaker_a", "tts_provider": "cosyvoice"}]
    ) is None


def test_approve_voice_selection_persists_minimax_model_in_review_state(tmp_path: Path) -> None:
    manager = ReviewStateManager(tmp_path / "review_state.json")
    manager.set_stage(
        VOICE_SELECTION_REVIEW_STAGE,
        status=REVIEW_STATUS_PENDING,
        payload={"speakers": [{"speaker_id": "speaker_a", "speaker_name": "A"}]},
        activate=True,
    )

    approve_voice_selection(
        project_dir=tmp_path,
        speakers=[
            {
                "speaker_id": "speaker_a",
                "voice_id": "Chinese_Male_1",
                "voice_source": "catalog",
                "tts_provider": "minimax",
                "minimax_model": "hd",
            }
        ],
    )

    stage = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE)
    speaker = stage["payload"]["speakers"][0]
    assert speaker["tts_provider"] == "minimax"
    assert speaker["minimax_model"] == "hd"


def test_job_service_persists_ui_selected_minimax_model_before_continue(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    store.save_job(_job_record(tts_model="speech-2.8-turbo"))
    service = JobService(store=store, runner=object())  # update path does not touch runner

    updated = service.update_tts_model_from_voice_selection(
        "job_voice_model",
        "speech-2.8-hd",
    )

    assert updated.tts_model == "speech-2.8-hd"
    assert store.require_job("job_voice_model").tts_model == "speech-2.8-hd"


def test_job_service_rejects_unknown_minimax_model(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    store.save_job(_job_record())
    service = JobService(store=store, runner=object())

    with pytest.raises(ValueError, match="Unsupported MiniMax TTS model"):
        service.update_tts_model_from_voice_selection("job_voice_model", "speech-3")
