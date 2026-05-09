"""Voice profile inference for editing-mode speakers (Task 5).

Plan §Task 5: Fire-and-forget Pass 3-style inference triggered when an
editing-mode speaker first gets a segment assigned. Re-uses
transcript_reviewer.review_pass3_voice_profiles(mode='studio') —
admin-configured S2 Pass 3 LLM. Pure LLM call, no TTS / no clone.
"""
from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import pytest

from services.jobs.editing_speakers import (
    create_speaker, load_speakers,
)


def _bootstrap_project_with_segments(
    tmp_path: Path, *, with_speaker_a_segment: bool = True,
) -> Path:
    project = tmp_path / "project_xyz"
    edit_dir = project / "editor" / "editing"
    edit_dir.mkdir(parents=True)
    (project / "audio").mkdir()
    audio = project / "audio" / "original.wav"
    # 占位字节；review_pass3_voice_profiles 我们 mock 掉
    audio.write_bytes(b"RIFFmock-wav")
    segments = []
    if with_speaker_a_segment:
        segments.append({
            "segment_id": "seg_1",
            "speaker_id": "speaker_a",
            "start_ms": 0, "end_ms": 5000,
            "source_text": "hello world",
        })
    (edit_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False), "utf-8"
    )
    return project


def test_inference_uses_studio_mode(tmp_path: Path) -> None:
    """D3 contract: must call review_pass3_voice_profiles(mode='studio')
    so the admin-configured S2 Pass 3 LLM is used."""
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        return_value={"speaker_a": {"voice_description": "warm"}},
    ) as mock_p3:
        infer_voice_profile_for_speaker(project, "speaker_a")
    assert mock_p3.call_args.kwargs["mode"] == "studio"


def test_inference_failure_is_fail_soft(tmp_path: Path) -> None:
    """D5: any exception → status='failed', does NOT raise (fire-and-forget)."""
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        side_effect=RuntimeError("LLM down"),
    ):
        infer_voice_profile_for_speaker(project, "speaker_a")  # MUST NOT raise
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "failed"
    assert "LLM down" in (sp.profile_error or "")


def test_inference_success_writes_profile_and_status_ready(tmp_path: Path) -> None:
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import infer_voice_profile_for_speaker
    with patch(
        "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
        return_value={"speaker_a": {"voice_description": "warm", "gender": "male"}},
    ):
        infer_voice_profile_for_speaker(project, "speaker_a")
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "ready"
    assert sp.voice_profile == {"voice_description": "warm", "gender": "male"}
    assert sp.profile_error is None


def test_maybe_trigger_idempotent_skips_when_not_pending(
    tmp_path: Path, monkeypatch
) -> None:
    """D4 idempotency: status != 'pending_segments' → no executor.submit。
    防止用户连点 PATCH 烧 LLM 配额。"""
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import (
        maybe_trigger_inference, _update_speaker_status,
    )
    _update_speaker_status(project, "speaker_a", status="inferring")

    submit_calls: list = []
    class _DummyExecutor:
        def submit(self, fn, *args, **kw):
            submit_calls.append((fn, args, kw))
            f = Future(); f.set_result(None); return f
    from services.jobs import editing_voice_profile as evp
    monkeypatch.setattr(evp, "_executor", _DummyExecutor())
    maybe_trigger_inference(project, "speaker_a")
    assert submit_calls == []  # 不再触发


def test_maybe_trigger_fires_once_when_pending(
    tmp_path: Path, monkeypatch
) -> None:
    project = _bootstrap_project_with_segments(tmp_path)
    create_speaker(project, display_name="C", baseline_speakers=[])
    from services.jobs.editing_voice_profile import maybe_trigger_inference
    submit_calls: list = []
    class _SyncExecutor:
        def submit(self, fn, *args, **kw):
            submit_calls.append((fn, args, kw))
            with patch(
                "services.jobs.editing_voice_profile.review_pass3_voice_profiles",
                return_value={"speaker_a": {"voice_description": "x"}},
            ):
                fn(*args, **kw)
            f = Future(); f.set_result(None); return f
    from services.jobs import editing_voice_profile as evp
    monkeypatch.setattr(evp, "_executor", _SyncExecutor())
    maybe_trigger_inference(project, "speaker_a")
    assert len(submit_calls) == 1
    sp = next(s for s in load_speakers(project) if s.speaker_id == "speaker_a")
    assert sp.profile_status == "ready"


def test_no_paid_api_imports() -> None:
    """Hard guard (CLAUDE.md): NO TTS / clone module imports here."""
    import ast
    src = Path("src/services/jobs/editing_voice_profile.py").read_text("utf-8")
    tree = ast.parse(src)
    forbidden = ("tts_generator", "voice_clone", "minimax_clone",
                 "voice_clone_router")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (getattr(node, "module", None) or "")
            for alias in getattr(node, "names", []):
                full = f"{mod}.{alias.name}".strip(".")
                for f in forbidden:
                    assert f not in full, f"forbidden import: {full}"


def test_gather_inference_inputs_uses_original_wav(tmp_path: Path) -> None:
    """source_audio_path 主路径必须是 <project_dir>/audio/original.wav
    (与 src/pipeline/process.py:1401/1463/3323 一致)。"""
    project = _bootstrap_project_with_segments(tmp_path)
    from services.jobs.editing_voice_profile import _gather_inference_inputs
    lines, src_audio, speakers_meta = _gather_inference_inputs(
        project, "speaker_a"
    )
    assert src_audio == project / "audio" / "original.wav"
    assert len(lines) == 1
    assert lines[0]["speaker_id"] == "speaker_a"
    assert speakers_meta == {"speaker_a": {}}
