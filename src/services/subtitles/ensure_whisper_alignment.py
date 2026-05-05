"""Deliverable-time helper: ensure subtitle cues are whisper-aligned.

Phase D-2 of 2026-05-04-subtitle-audio-sync-plan.

Called from Jianying-draft generation (D-3) and materials_pack
packaging (D-4) BEFORE consuming output/subtitles*.srt. Idempotent +
cache-aware:

  - Already whisper-aligned + fingerprint matches → no-op
  - Already whisper-aligned + fingerprint mismatches (audio re-TTS'd
    underneath) → regenerate
  - Proportional cues + admin enables whisper → regenerate, write SRT
  - Proportional cues + admin disabled → no-op (proportional stays)

The fingerprint is sha256 of (sorted aligned WAV path + content_hash
pairs). Mismatch detects: user edited a segment + did re-TTS; resulting
WAV has different bytes; whisper transcript at the cached path is
stale; we re-run whisper for that segment (others hit per-WAV cache).

Returns a status dict for caller logging:
  ``action``: "already_aligned" | "skipped_admin_disabled" |
              "skipped_no_segments" | "regenerated"
  ``whisper_invoked``: bool — true iff at least one whisper subprocess
                       was actually spawned during this call
  ``blocks_processed``: int — set when ``action == "regenerated"``
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Tag we look for in cue ``source`` to recognise "this cue was already
# whisper-aligned in a previous run". Mirrors the constant in
# ``modules.subtitles.cue_builder._WHISPER_ALIGNED_SOURCE``.
_WHISPER_SOURCE_PREFIX = "semantic_block_v2_whisper_aligned"


@dataclass(slots=True, frozen=True)
class EnsureStatus:
    """Status of an ``ensure_whisper_aligned_subtitles`` call.

    ``action`` is one of:
      - ``"already_aligned"`` — fast path; no whisper invocation, no
        file writes. Hit when cues already carry whisper source AND
        fingerprint matches current audio bytes.
      - ``"skipped_admin_disabled"`` — either env capability or admin
        policy is off; proportional path stays in place.
      - ``"skipped_no_segments"`` — editor/segments.json missing;
        nothing to align against.
      - ``"regenerated"`` — whisper ran, output files rewritten.
    ``whisper_invoked`` distinguishes "we actually spawned whisper" from
    "we read fully from cache". For "regenerated" the helper can't
    cheaply tell which segments hit cache vs ran fresh, so this is a
    coarse "yes the cue pipeline went through the whisper path".
    ``blocks_processed`` is set on regenerated.
    """

    action: str
    whisper_invoked: bool
    blocks_processed: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def ensure_whisper_aligned_subtitles(project_dir: str | Path) -> dict:
    """Side-effect entry: bring output/subtitle_cues.json + SRT files
    to whisper-aligned state if both gates allow and they aren't there
    already. Returns a status dict (see ``EnsureStatus``)."""
    t0 = time.monotonic()
    project_dir = Path(project_dir)

    # ---- Gate 1: admin policy + env capability ----
    # Lazy-import the cue_pipeline gate so the deliverable handlers
    # don't carry a transitive import to faster_whisper / whisper_align.
    from modules.subtitles.cue_pipeline import _whisper_align_enabled
    if not _whisper_align_enabled():
        return EnsureStatus(
            action="skipped_admin_disabled",
            whisper_invoked=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ).to_dict()

    # ---- Gate 2: input availability ----
    seg_path = project_dir / "editor" / "segments.json"
    if not seg_path.is_file():
        return EnsureStatus(
            action="skipped_no_segments",
            whisper_invoked=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ).to_dict()
    try:
        segs = json.loads(seg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "ensure_whisper: editor/segments.json unreadable (%s); skipping", exc,
        )
        return EnsureStatus(
            action="skipped_no_segments",
            whisper_invoked=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ).to_dict()
    if not isinstance(segs, list) or not segs:
        return EnsureStatus(
            action="skipped_no_segments",
            whisper_invoked=False,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        ).to_dict()

    # ---- Fast path: existing cues already whisper-aligned + matches fp ----
    cues_path = project_dir / "output" / "subtitle_cues.json"
    expected_fp = _compute_alignment_fingerprint(segs)
    if cues_path.is_file():
        try:
            payload = json.loads(cues_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            cues = payload.get("cues", [])
            stamped_fp = payload.get("alignment_fingerprint")
            all_whisper = (
                cues
                and all(
                    _WHISPER_SOURCE_PREFIX in str(c.get("source", ""))
                    for c in cues
                )
            )
            if all_whisper and stamped_fp == expected_fp:
                return EnsureStatus(
                    action="already_aligned",
                    whisper_invoked=False,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                ).to_dict()

    # ---- Slow path: regenerate via cue pipeline with whisper enabled ----
    blocks_processed = _regenerate_whisper_cues(project_dir, segs, expected_fp)
    return EnsureStatus(
        action="regenerated",
        whisper_invoked=True,
        blocks_processed=blocks_processed,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    ).to_dict()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_alignment_fingerprint(segs: list[dict]) -> str:
    """SHA256 over (segment_id, aligned_audio_path basename, file content
    sha256) tuples. Captures: segment ordering, audio path identity, and
    audio bytes — any of these changing invalidates the whisper transcript.

    Per-WAV content hashes are reused from the C5 cache layer if those
    cache files exist (they store ``content_hash`` already); else
    re-hashed. Hashing one WAV is fast (<100ms each on a typical 30s
    aligned WAV).
    """
    h = hashlib.sha256()
    for seg in sorted(segs, key=lambda s: str(s.get("segment_id", ""))):
        sid = str(seg.get("segment_id", ""))
        aligned_path = seg.get("aligned_audio_path") or ""
        h.update(sid.encode("utf-8"))
        h.update(b"\x00")
        h.update(aligned_path.encode("utf-8"))
        h.update(b"\x00")
        wav_hash = _content_hash_for_wav(aligned_path)
        h.update(wav_hash.encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()


def _content_hash_for_wav(wav_path: str) -> str:
    """Cheap content hash with the C5 cache as a fast-path source.

    If a whisper cache exists alongside the WAV, its stored
    ``content_hash`` is reused. Otherwise the file is read and hashed.
    Empty / missing path → empty hash (won't match anything).
    """
    if not wav_path:
        return ""
    p = Path(wav_path)
    if not p.is_file():
        return ""

    # Try C5 cache first (~5KB JSON read instead of full WAV hash).
    # Cache file naming: {wav_path}.whisper_<model>_<lang>.json — we
    # look for any whisper_* cache file alongside the WAV. If present,
    # its content_hash should match the current bytes (the cache was
    # invalidated automatically by run_whisper_subprocess_cached when
    # bytes changed).
    for cache_file in p.parent.glob(f"{p.name}.whisper_*.json"):
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cache, dict):
                ch = cache.get("content_hash")
                if isinstance(ch, str) and ch:
                    return ch
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    # Fallback: hash the file directly. ~50ms for a 1MB WAV.
    h = hashlib.sha256()
    try:
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError as exc:
        logger.debug("content_hash_for_wav: read failed for %s (%s)", wav_path, exc)
        return ""
    return h.hexdigest()


def _regenerate_whisper_cues(
    project_dir: Path,
    segs: list[dict],
    fingerprint: str,
) -> int:
    """Build cues with whisper enabled and write all output files.

    Mirrors what publish stage's OutputDispatcher does, scoped to
    subtitle outputs only (not the manifest, not the audio mix). Reuses
    the already-deployed Phase A/B/C plumbing — segment_text() for cue
    splitting, build_cues_with_char_times() for whisper-driven timing,
    cue_pipeline.build_subtitle_cues_for_blocks() as the orchestrator.
    """
    # Lazy imports to keep the deliverable-handler import surface small.
    from core.models import SemanticBlock, SubtitleLine
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks
    from modules.subtitles.srt_writer import (
        write_zh_srt, write_en_srt, write_bilingual_srt,
    )

    blocks: list[SemanticBlock] = []
    lines: list[SubtitleLine] = []
    for seg in segs:
        try:
            sid_int = int(str(seg.get("segment_id", "")).strip())
        except (TypeError, ValueError):
            continue
        cn_text = seg.get("cn_text") or ""
        tts_input = seg.get("tts_input_cn_text", "") or cn_text
        aligned_path = seg.get("aligned_audio_path")

        absorbed = seg.get("short_merge_absorbed_segment_ids") or []
        if not isinstance(absorbed, list):
            absorbed = []
        original_indices = [sid_int]
        for x in absorbed:
            try:
                original_indices.append(int(x))
            except (TypeError, ValueError):
                continue

        blocks.append(SemanticBlock(
            block_id=f"segment_{sid_int:03d}",
            speaker_id=str(seg.get("speaker_id") or ""),
            speaker_name=seg.get("display_name"),
            original_srt_indices=original_indices,
            first_start_ms=int(seg.get("start_ms") or 0),
            last_end_ms=int(seg.get("end_ms") or 0),
            target_duration_ms=int(seg.get("target_duration_ms") or 0),
            merged_cn_text=cn_text,
            tts_input_cn_text=tts_input,
            actual_audio_duration_ms=int(seg.get("actual_duration_ms") or 0),
            rewrite_count=int(seg.get("rewrite_count") or 0),
            tts_audio_path=str(seg.get("tts_audio_path") or "") or None,
            aligned_audio_path=str(aligned_path) if aligned_path else None,
            status="completed",
            alignment_method=seg.get("alignment_method") or "direct",
            needs_review=bool(seg.get("needs_review")),
            dubbing_mode=str(seg.get("dubbing_mode") or "dub"),
        ))
        lines.append(SubtitleLine(
            index=sid_int,
            start_ms=int(seg.get("start_ms") or 0),
            end_ms=int(seg.get("end_ms") or 0),
            speaker_id=str(seg.get("speaker_id") or ""),
            speaker_name=seg.get("display_name"),
            en_text=str(seg.get("source_text") or ""),
            cn_text=cn_text,
        ))

    result = build_subtitle_cues_for_blocks(blocks, lines)

    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    project_id = project_dir.name

    # subtitle_cues.json with the fingerprint stamped
    cues_serialized = []
    for c in result.cues:
        cues_serialized.append({
            "cue_id": c.cue_id,
            "block_id": c.block_id,
            "speaker_id": c.speaker_id,
            "speaker_name": c.speaker_name,
            "text": c.text,
            "en_text": c.en_text,
            "start_ms": c.start_ms,
            "end_ms": c.end_ms,
            "source": c.source,
            "needs_review": c.needs_review,
            "review_reason": c.review_reason,
        })
    cues_payload = {
        "schema_version": "subtitle_cues_v2",
        "project_id": project_id,
        "alignment_fingerprint": fingerprint,
        "cues": cues_serialized,
    }
    (output_dir / "subtitle_cues.json").write_text(
        json.dumps(cues_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # quality report
    issues_serialized = [
        {
            "block_id": i.block_id, "cue_id": i.cue_id,
            "code": i.code, "severity": i.severity, "message": i.message,
        }
        for i in result.report.issues
    ]
    summaries_serialized = []
    drift_count = 0
    for s in result.report.block_summaries:
        d = bool(getattr(s, "text_audio_drift", False))
        if d:
            drift_count += 1
        summaries_serialized.append({
            "block_id": s.block_id,
            "cue_count": s.cue_count,
            "text_mismatch": s.text_mismatch,
            "timing_overlap_count": s.timing_overlap_count,
            "timing_out_of_block_count": s.timing_out_of_block_count,
            "empty_cue_count": s.empty_cue_count,
            "long_unbreakable_count": s.long_unbreakable_count,
            "unknown_mixed_token_count": s.unknown_mixed_token_count,
            "short_display_duration_count": s.short_display_duration_count,
            "text_audio_drift": d,
        })
    quality_payload = {
        "schema_version": "subtitle_quality_report_v2",
        "project_id": project_id,
        "validation_status": result.report.validation_status,
        "text_audio_drift_count": drift_count,
        "issues": issues_serialized,
        "block_summaries": summaries_serialized,
    }
    (output_dir / "subtitle_quality_report.json").write_text(
        json.dumps(quality_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # SRT files
    zh_srt = write_zh_srt(result.cues)
    en_srt = write_en_srt(result.cues)
    bi_srt = write_bilingual_srt(result.cues)
    (output_dir / "subtitles_zh.srt").write_text(zh_srt, encoding="utf-8")
    (output_dir / "subtitles.srt").write_text(zh_srt, encoding="utf-8")  # alias
    (output_dir / "subtitles_en.srt").write_text(en_srt, encoding="utf-8")
    (output_dir / "subtitles_bilingual.srt").write_text(bi_srt, encoding="utf-8")

    return len(result.block_specs)


__all__ = ["ensure_whisper_aligned_subtitles", "EnsureStatus"]
