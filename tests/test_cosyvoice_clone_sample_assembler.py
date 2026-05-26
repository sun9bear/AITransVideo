"""Phase 4.2 A.2b: ``cosyvoice_clone.sample_assembler`` 4 层 ownership 守卫。

每条 case 覆盖一个 typed exception，证明任一 ownership 边界破坏 →
**绝不**调 ``concat_segments_to_wav``、**绝不**返字节流。

测试用 in-memory SQLite + 真 ``Job`` ORM row + 临时 ``project_dir`` /
``transcript.json`` / ``audio/`` 目录。``concat_segments_to_wav`` 用
monkeypatch 拦截 → 不真跑 ffmpeg。

Codex 2026-05-26 v4-followup §1.2 / §4.1 P1.3 review 强调：4 层全过才
能拼字节。SegmentNotFoundError / SpeakerOwnershipViolation 必须挡掉跨
speaker / 跨 job 借声音的攻击向量。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


# Inject gateway/ + src/ on sys.path（conftest.py 应已加，但 safety net）
REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "gateway", REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from cosyvoice_clone import sample_assembler  # type: ignore[import-not-found]
from cosyvoice_clone.sample_assembler import (  # type: ignore[import-not-found]
    EmptySegmentsError,
    JobNotFoundError,
    JobOwnershipViolation,
    NoProjectDirError,
    NoSourceAudioError,
    SampleAssemblyError,
    SegmentNotFoundError,
    SpeakerOwnershipViolation,
    TranscriptNotFoundError,
    TranscriptParseError,
    assemble_sample_from_job_segments,
)


# ---------------------------------------------------------------------------
# Fake DB session + user/job rows — pure mocks, no SQLite (UserVoice / 等用
# JSONB 列在 SQLite 上 create_all 会挂)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUser:
    id: Any
    role: str = "user"


@dataclass
class _FakeJob:
    job_id: str
    user_id: Any
    project_dir: str | None


class _FakeScalarResult:
    """Mimic ``Result.scalar_one_or_none()``."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeAsyncSession:
    """Mock for ``AsyncSession``: 支持 ``add_all`` / ``commit`` (no-op except
    for storing Job rows) + ``execute(select(Job).where(Job.job_id == X))``。

    SQLite 不能 ``create_all`` 完整 schema（``JSONB`` 在 SQLite 不支持），
    所以测试用纯 mock。``add_all`` 的 User 行被忽略（4 层 ownership 用
    传入的 ``_FakeUser`` 而非 DB lookup）。
    """

    def __init__(self):
        self._jobs: dict[str, _FakeJob] = {}

    def add_all(self, items):
        for item in items:
            if isinstance(item, _FakeJob):
                self._jobs[item.job_id] = item
            # ``_FakeUser`` 直接被忽略 —— assembler 只查 Job 表，user 通过
            # 参数注入

    async def commit(self):
        pass

    async def execute(self, stmt):
        # 提取 ``Select.whereclause`` 的右值（被比较的 job_id 字符串）
        target_job_id = None
        try:
            crit = stmt.whereclause
            target_job_id = crit.right.value
        except Exception:
            pass
        return _FakeScalarResult(self._jobs.get(target_job_id))


def _make_user(role: str = "user") -> _FakeUser:
    return _FakeUser(id=uuid.uuid4(), role=role)


def _make_job_row(user_id, project_dir: Path | None = None) -> _FakeJob:
    return _FakeJob(
        job_id=f"job-{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        project_dir=str(project_dir) if project_dir else None,
    )


@pytest.fixture
def db_session():
    """Empty mock async session. 各 test 在 ``async def _run()`` 里通过
    ``db_session.add_all([job])`` 显式补 Job 行（user 被忽略）。"""
    return _FakeAsyncSession()


def _write_transcript(project_dir: Path, lines: list[dict]) -> None:
    """写 ``project_dir/transcript/transcript.json``。
    支持 ``[{...}, ...]`` 或 ``{"lines": [...]}`` 两种 root 形态 —— 这里
    选 ``{"lines": ...}`` 与 voice_selection_api 既有约定一致。
    """
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "transcript.json").write_text(
        json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8"
    )


def _write_source_audio(project_dir: Path, name: str = "speech_for_asr.wav") -> Path:
    """写一个假 WAV header；不会被读，只判存在性。"""
    audio_dir = project_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / name
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    return path


@pytest.fixture
def mock_concat(monkeypatch, tmp_path):
    """拦截 ``concat_segments_to_wav``：写一个 fake output wav 并返回路径。

    断言它**只在 4 层 ownership 检查全过**之后才被调用。
    """
    calls: list[dict] = []

    def fake_concat(source_audio, segments, project_dir, speaker_id, *,
                    target_sample_rate_hz):
        calls.append({
            "source_audio": source_audio,
            "segments": list(segments),
            "project_dir": project_dir,
            "speaker_id": speaker_id,
            "target_sample_rate_hz": target_sample_rate_hz,
        })
        out_dir = project_dir / "speaker_audio" / speaker_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "clone_sample.wav"
        out_path.write_bytes(b"FAKE-CONCATENATED-WAV-BYTES")
        return out_path

    monkeypatch.setattr(sample_assembler, "concat_segments_to_wav", fake_concat)
    return calls


# ---------------------------------------------------------------------------
# Layer 0: empty input
# ---------------------------------------------------------------------------


def test_empty_segment_ids_raises_empty_segments_error(db_session, mock_concat):
    user = _make_user()

    async def _run():
        with pytest.raises(EmptySegmentsError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id="job-x",
                speaker_id="spk_a", segment_ids=[],
            )

    asyncio.run(_run())
    assert mock_concat == [], "concat 不应在 Layer 0 拒绝后被调用"


# ---------------------------------------------------------------------------
# Layer 1: Job ownership
# ---------------------------------------------------------------------------


def test_job_not_found_raises_job_not_found(db_session, mock_concat):
    user = _make_user()

    async def _run():
        with pytest.raises(JobNotFoundError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user,
                source_job_id="non-existent-job",
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_cross_user_job_raises_job_ownership_violation(db_session, mock_concat, tmp_path):
    """攻击者用自己账户但传别人的 job_id → 403 不是 404（避免泄漏存在性）。"""
    owner = _make_user()
    attacker = _make_user()
    job = _make_job_row(user_id=owner.id, project_dir=tmp_path / "owner_proj")

    async def _run():
        db_session.add_all([owner, attacker, job])
        await db_session.commit()
        with pytest.raises(JobOwnershipViolation):
            await assemble_sample_from_job_segments(
                db=db_session, user=attacker,
                source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_admin_can_bypass_cross_user_ownership(db_session, mock_concat, tmp_path):
    """``user.role == 'admin'`` → 跨用户 job 可访问（与 materials_api 一致）。"""
    owner = _make_user()
    admin = _make_user(role="admin")
    project_dir = tmp_path / "owner_proj"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=owner.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([owner, admin, job])
        await db_session.commit()
        result_bytes = await assemble_sample_from_job_segments(
            db=db_session, user=admin,
            source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1],
        )
        return result_bytes

    out = asyncio.run(_run())
    assert out == b"FAKE-CONCATENATED-WAV-BYTES"
    assert len(mock_concat) == 1  # admin override 后正常调用 concat


# ---------------------------------------------------------------------------
# Layer 2: project_dir
# ---------------------------------------------------------------------------


def test_job_with_no_project_dir_raises_no_project_dir(db_session, mock_concat):
    user = _make_user()
    job = _make_job_row(user_id=user.id, project_dir=None)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(NoProjectDirError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


# ---------------------------------------------------------------------------
# Layer 3: transcript
# ---------------------------------------------------------------------------


def test_missing_transcript_raises_transcript_not_found(db_session, mock_concat, tmp_path):
    user = _make_user()
    project_dir = tmp_path / "proj_no_transcript"
    project_dir.mkdir(parents=True)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(TranscriptNotFoundError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_malformed_transcript_json_raises_transcript_parse_error(
    db_session, mock_concat, tmp_path,
):
    user = _make_user()
    project_dir = tmp_path / "proj_bad_transcript"
    project_dir.mkdir(parents=True)
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir()
    (transcript_dir / "transcript.json").write_text("{not valid json", encoding="utf-8")
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(TranscriptParseError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_transcript_root_other_than_list_or_dict_raises_parse_error(
    db_session, mock_concat, tmp_path,
):
    user = _make_user()
    project_dir = tmp_path / "proj_weird_transcript"
    project_dir.mkdir(parents=True)
    transcript_dir = project_dir / "transcript"
    transcript_dir.mkdir()
    (transcript_dir / "transcript.json").write_text('"just a string"', encoding="utf-8")
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(TranscriptParseError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


# ---------------------------------------------------------------------------
# Layer 4: segment ownership — CORE SECURITY GUARDS
# ---------------------------------------------------------------------------


def test_segment_id_not_in_transcript_raises_segment_not_found(
    db_session, mock_concat, tmp_path,
):
    """攻击者用伪造 segment_id → 403 segment_not_found，**不打 ffmpeg**。"""
    user = _make_user()
    project_dir = tmp_path / "proj_seg_not_found"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(SegmentNotFoundError) as exc_info:
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1, 99999],  # 99999 doesn't exist
            )
        assert exc_info.value.offending_segment_id == 99999

    asyncio.run(_run())
    assert mock_concat == []


def test_cross_speaker_segment_raises_speaker_ownership_violation(
    db_session, mock_concat, tmp_path,
):
    """**核心安全测试**：``speaker_id=A`` 但段实际属于 ``speaker_id=B`` →
    403 segment_ownership_violation。直接挡跨 speaker 借声音攻击。"""
    user = _make_user()
    project_dir = tmp_path / "proj_cross_speaker"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
        {"index": 2, "speaker_id": "spk_b", "start_ms": 2000, "end_ms": 4000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        # 声称为 spk_a 但段 2 属于 spk_b
        with pytest.raises(SpeakerOwnershipViolation) as exc_info:
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1, 2],
            )
        assert exc_info.value.offending_segment_id == 2
        assert exc_info.value.expected_speaker == "spk_a"
        assert exc_info.value.actual_speaker == "spk_b"

    asyncio.run(_run())
    assert mock_concat == []


def test_single_cross_speaker_segment_also_blocked(
    db_session, mock_concat, tmp_path,
):
    """变体：单段就跨 speaker，也必须被挡。"""
    user = _make_user()
    project_dir = tmp_path / "proj_single_cross"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 5, "speaker_id": "spk_b", "start_ms": 0, "end_ms": 3000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(SpeakerOwnershipViolation):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[5],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_transcript_root_list_form_accepted(
    db_session, mock_concat, tmp_path,
):
    """transcript 可以是 ``[{...}, ...]`` 直接 list 形态 —— 与 voice_selection_api
    既有约定一致。"""
    user = _make_user()
    project_dir = tmp_path / "proj_list_transcript"
    project_dir.mkdir(parents=True)
    (project_dir / "transcript").mkdir()
    (project_dir / "transcript" / "transcript.json").write_text(
        json.dumps([
            {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
        ]),
        encoding="utf-8",
    )
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        return await assemble_sample_from_job_segments(
            db=db_session, user=user, source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1],
        )

    result = asyncio.run(_run())
    assert result == b"FAKE-CONCATENATED-WAV-BYTES"
    assert len(mock_concat) == 1


# ---------------------------------------------------------------------------
# Layer 5: source audio
# ---------------------------------------------------------------------------


def test_no_source_audio_raises_no_source_audio(db_session, mock_concat, tmp_path):
    user = _make_user()
    project_dir = tmp_path / "proj_no_audio"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
    ])
    # 故意不写 audio/
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        with pytest.raises(NoSourceAudioError):
            await assemble_sample_from_job_segments(
                db=db_session, user=user, source_job_id=job.job_id,
                speaker_id="spk_a", segment_ids=[1],
            )

    asyncio.run(_run())
    assert mock_concat == []


def test_original_wav_fallback_when_speech_for_asr_missing(
    db_session, mock_concat, tmp_path,
):
    """两种源音频按 ``speech_for_asr.wav`` 优先 ``original.wav`` 兜底的顺序找。"""
    user = _make_user()
    project_dir = tmp_path / "proj_fallback_audio"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
    ])
    _write_source_audio(project_dir, name="original.wav")  # only fallback
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        return await assemble_sample_from_job_segments(
            db=db_session, user=user, source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1],
        )

    result = asyncio.run(_run())
    assert result == b"FAKE-CONCATENATED-WAV-BYTES"
    assert mock_concat[0]["source_audio"].name == "original.wav"


# ---------------------------------------------------------------------------
# Layer 6: concat with 16kHz sample rate (A.2a contract)
# ---------------------------------------------------------------------------


def test_concat_called_with_16khz_sample_rate(db_session, mock_concat, tmp_path):
    """**A.2a × A.2b 契约**：CosyVoice 路径必须以 16000 Hz 调
    ``concat_segments_to_wav``。MiniMax 路径用 24000 不变。
    """
    user = _make_user()
    project_dir = tmp_path / "proj_16k"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 3000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        return await assemble_sample_from_job_segments(
            db=db_session, user=user, source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1],
        )

    asyncio.run(_run())
    assert len(mock_concat) == 1
    assert mock_concat[0]["target_sample_rate_hz"] == 16000


def test_concat_receives_only_claimed_speaker_segments(db_session, mock_concat, tmp_path):
    """ffmpeg concat 拿到的 segments 必须是用户声明的那批，且都属于
    声明的 speaker。即使 transcript 还有别的 speaker 的段，也不混进。"""
    user = _make_user()
    project_dir = tmp_path / "proj_filter"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
        {"index": 2, "speaker_id": "spk_b", "start_ms": 2000, "end_ms": 4000},  # 不该被选
        {"index": 3, "speaker_id": "spk_a", "start_ms": 4000, "end_ms": 6000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        return await assemble_sample_from_job_segments(
            db=db_session, user=user, source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1, 3],
        )

    asyncio.run(_run())
    passed_segments = mock_concat[0]["segments"]
    assert [s["index"] for s in passed_segments] == [1, 3]
    assert all(s["speaker_id"] == "spk_a" for s in passed_segments)


# ---------------------------------------------------------------------------
# Cleanup: temp file unlink
# ---------------------------------------------------------------------------


def test_temp_concat_file_unlinked_after_read(db_session, monkeypatch, tmp_path):
    """concat 写的临时 WAV 在字节读完后会被 unlink（best-effort cleanup）。"""
    user = _make_user()
    project_dir = tmp_path / "proj_unlink"
    project_dir.mkdir(parents=True)
    _write_transcript(project_dir, [
        {"index": 1, "speaker_id": "spk_a", "start_ms": 0, "end_ms": 2000},
    ])
    _write_source_audio(project_dir)
    job = _make_job_row(user_id=user.id, project_dir=project_dir)

    written_paths: list[Path] = []

    def fake_concat(source_audio, segments, project_dir_arg, speaker_id, *,
                    target_sample_rate_hz):
        out_dir = project_dir_arg / "speaker_audio" / speaker_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "clone_sample.wav"
        out.write_bytes(b"X" * 256)
        written_paths.append(out)
        return out

    monkeypatch.setattr(sample_assembler, "concat_segments_to_wav", fake_concat)

    async def _run():
        db_session.add_all([user, job])
        await db_session.commit()
        return await assemble_sample_from_job_segments(
            db=db_session, user=user, source_job_id=job.job_id,
            speaker_id="spk_a", segment_ids=[1],
        )

    asyncio.run(_run())

    assert len(written_paths) == 1
    assert not written_paths[0].exists(), (
        f"临时 WAV 应在读完字节后被 unlink，实际仍存在：{written_paths[0]}"
    )
