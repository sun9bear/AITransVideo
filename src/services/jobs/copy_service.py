"""Filesystem helpers for copy_as_new (T1-8).

Plan ref: §3.5 / §7.8 / D27 / D28 / D34

Responsibilities:

- ``hardlink_baseline_audio`` — for every wav in
  ``<source>/editor/tts_segments/*.wav``, create an ``os.link`` at the
  matching path under ``<target>/editor/tts_segments/``. Inode is shared;
  deleting either end doesn't free the storage until both are gone.
- ``apply_draft_segment`` — SAFELY replace a (possibly hardlinked) wav at
  the target with the contents of a draft. **Must NOT** ``open(target, 'wb')``
  because that would mutate the shared inode, corrupting the source's
  baseline audio (§3.5 invariant). Instead: unlink the target first, then
  move the draft in. This is the single chokepoint enforced by AST guard
  ``test_no_raw_open_wb_on_shared_paths`` (plan §16.4).
- ``write_audio_safely`` — generic "produce a wav at this path" helper
  with the same safety posture; callers give us bytes, we atomically
  place them. Uses temp file + ``os.replace`` so there is no window in
  which the target is half-written.
- ``prepare_copy_project_dir`` — Phase A of copy_as_new (§7.8):
  1. ``shutil.copy2`` transcript.json / segments.json / manifest.json
  2. hardlink all baseline tts_segments wavs
  3. apply editing/segments.json edits → overwrite target segments.json
  4. apply editing/voice_map.json overrides → merge into target segments.json
  5. apply editing/tts_segments_draft → replace target wavs via
     ``apply_draft_segment`` (one per draft)
  Raises on any failure; caller is responsible for ``rollback_prepared_target``
  which performs best-effort cleanup so the source stays untouched.
- ``rollback_prepared_target`` — rm -rf the target project_dir. Used by
  T1-9 commit's Phase A failure branch.

What's NOT here:

- Creating the new Job row (Gateway's concern — T1-9 Phase A step 5).
- Submitting the new job to the runner (``runner_extensions``).
- Deleting source ``editor/editing/`` + resetting source status (Phase B,
  T1-9 runs after Phase A returns successfully).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from core.enums import StageStatus
from services.jobs.models import (
    STAGE_ALIGNMENT,
    STAGE_LEGACY_PROCESS_OUTPUT,
)
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

__all__ = [
    "CopyPreparationError",
    "apply_draft_segment",
    "hardlink_baseline_audio",
    "hardlink_media_artifacts",
    "prepare_copy_project_dir",
    "prune_project_state_payload",
    "rollback_prepared_target",
    "write_audio_safely",
]


class CopyPreparationError(Exception):
    """Raised when Phase A of copy_as_new cannot complete. Callers treat
    this as "source project_dir is untouched; target may be half-built —
    run rollback_prepared_target"."""


# ---------------------------------------------------------------------------
# Individual file helpers
# ---------------------------------------------------------------------------


def hardlink_baseline_audio(source_dir: str | Path, target_dir: str | Path) -> list[str]:
    """For every wav in ``source_dir/editor/tts_segments/``, create an
    ``os.link`` at the matching path under ``target_dir/editor/tts_segments/``.

    Returns the list of segment IDs that were hardlinked. Creates target
    directories as needed. Existing files at the target path are NOT
    replaced (caller should have a fresh target); raises FileExistsError.

    Linux/macOS: ``os.link`` is supported by all common filesystems.
    Windows NTFS supports it via CreateHardLinkW — this project runs on
    Linux in production (per plan §Linux 主机 decision). If future
    platforms need COW instead, swap this function.
    """
    src_audio = Path(source_dir) / "editor" / "tts_segments"
    dst_audio = Path(target_dir) / "editor" / "tts_segments"
    dst_audio.mkdir(parents=True, exist_ok=True)

    if not src_audio.is_dir():
        return []

    linked: list[str] = []
    for src_wav in sorted(src_audio.glob("*.wav")):
        segment_id = src_wav.stem
        dst_wav = dst_audio / src_wav.name
        os.link(src_wav, dst_wav)  # raises FileExistsError if dst exists
        linked.append(segment_id)
    return linked


# Relative paths that a succeeded pipeline emits as large, immutable media
# artifacts — pipeline's S0 / audio_preparation / S1 cache-check against
# these, so copy_as_new must carry them into the target dir or the new
# job will re-run demucs / ASR from scratch. Hardlink (not copy) because
# these are single-writer single-reader and typically in the hundred-MB
# range; copying would blow out disk and runtime for no gain.
_MEDIA_HARDLINK_RELS: tuple[str, ...] = (
    "video/original.mp4",
    "audio/original.wav",
    # Canonical demucs output name — see
    # ``services.audio.separator.speech_filename``.
    "audio/speech_for_asr.wav",
    "audio/ambient.wav",
)


# Stage names that must be reset to PENDING when a copy_as_new /
# overwrite commit lands. Upstream stages keep DONE so the pipeline's
# per-stage cache-check (file-existence based) still skips re-running
# them on the new inputs.
_STAGES_TO_PRUNE: frozenset[str] = frozenset({
    STAGE_ALIGNMENT,
    STAGE_LEGACY_PROCESS_OUTPUT,
})


def _empty_stage_entry() -> dict[str, Any]:
    # Mirrors ``StateManager._empty_stage`` — duplicated here to avoid
    # instantiating a StateManager (which would require a path) just to
    # build one dict. If that method's shape ever changes, this must too.
    return {
        "status": StageStatus.PENDING.value,
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "error_message": None,
        "payload": {},
    }


def prune_project_state_payload(
    payload: dict[str, Any],
    *,
    new_project_id: str,
) -> dict[str, Any]:
    """Return a deep-copied project_state dict with ``project_id`` replaced
    and every stage in ``_STAGES_TO_PRUNE`` reset to PENDING.

    Unknown stages pass through verbatim — fail-closed if a future
    pipeline version introduces a new post-edit stage (visible DONE
    surfaces the oversight rather than silently erasing it).
    """
    out = copy.deepcopy(payload)
    out["project_id"] = new_project_id
    stages = out.get("stages")
    if isinstance(stages, dict):
        for stage_name in list(stages.keys()):
            if stage_name in _STAGES_TO_PRUNE and isinstance(stages[stage_name], dict):
                stages[stage_name] = _empty_stage_entry()
    return out


# Field-name suffixes that always hold filesystem paths in DubbingSegment /
# pipeline JSON. Only values under these keys get the source→target
# substring rewrite, so a stray cn_text / error message that happens to
# contain the source dir as a substring stays untouched.
_PATH_KEY_SUFFIXES: tuple[str, ...] = ("_path", "_paths", "_dir", "_dirs")


def _copy_json_with_path_rewrite(
    src_file: Path,
    dst_file: Path,
    *,
    source_dir: Path,
    target_dir: Path,
) -> None:
    """Copy a JSON file from source to target, rewriting any embedded
    absolute paths that point at ``source_dir`` to the equivalent path
    under ``target_dir``.

    Replaces the older ``shutil.copy2`` call sites inside
    ``prepare_copy_project_dir``. The 2026-04-19 incident: a file-level
    verbatim copy left source paths inside target's
    download_metadata.json / project_state.json / editor/manifest.json.
    γ resume's output stdout then surfaced those paths, which
    ``ProcessJobRunner._record_line`` parsed and used to silently overwrite
    ``JobRecord.project_dir`` to source — a subsequent enter_editing /
    commit loop then operated on source while the UI pointed at the copy.

    Rewriting is delegated to ``_rewrite_project_dir_paths`` which only
    mutates values under keys suffixed ``_path`` / ``_dir``, so safe to
    apply blindly to any JSON dict/list tree (review_state.json,
    manifest.json, project_state.json, etc.).

    Non-JSON / unreadable files raise — callers should have already
    checked with ``is_file()``. dst parent dirs are auto-created.
    """
    payload = json.loads(src_file.read_text(encoding="utf-8"))
    rewritten = _rewrite_project_dir_paths(
        payload, source_dir=source_dir, target_dir=target_dir,
    )
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(dst_file), rewritten)


def _rewrite_project_dir_paths(
    payload: Any,
    *,
    source_dir: Path,
    target_dir: Path,
    current_key: str | None = None,
) -> Any:
    """Relocate absolute paths pointing at ``source_dir`` to ``target_dir``
    inside a nested JSON payload, scoped to keys whose name ends in
    ``_path`` or ``_dir``.

    Scoping to path-flavoured keys avoids mutating arbitrary strings
    (e.g. error messages or log fragments) that merely happen to embed
    the source path as a substring.
    """
    src_str = str(source_dir)
    dst_str = str(target_dir)
    if isinstance(payload, dict):
        return {
            k: _rewrite_project_dir_paths(
                v, source_dir=source_dir, target_dir=target_dir, current_key=k,
            )
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [
            _rewrite_project_dir_paths(
                v, source_dir=source_dir, target_dir=target_dir, current_key=current_key,
            )
            for v in payload
        ]
    if (
        isinstance(payload, str)
        and current_key is not None
        and any(current_key.endswith(sfx) for sfx in _PATH_KEY_SUFFIXES)
        and src_str in payload
    ):
        return payload.replace(src_str, dst_str)
    return payload


def hardlink_media_artifacts(
    source_dir: str | Path, target_dir: str | Path,
) -> list[str]:
    """Hardlink pipeline's large immutable media artifacts from source to
    target. Silently skips files that don't exist at source (handles
    pipelines that never reached the relevant stage, e.g. early failures).

    Returns the list of relative paths actually linked. Raises on any real
    OS failure (permission / cross-filesystem) so the caller can roll back.
    """
    src_root = Path(source_dir)
    dst_root = Path(target_dir)
    linked: list[str] = []
    for rel in _MEDIA_HARDLINK_RELS:
        src = src_root / rel
        if not src.is_file():
            continue
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
        linked.append(rel)
    return linked


def write_audio_safely(target_path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target_path`` via temp file + os.replace.

    Rationale: if ``target_path`` is a hardlink to another file (common in
    copy_as_new targets), ``open(target_path, 'wb')`` would open the shared
    inode for writing and the source's baseline audio would be corrupted.
    ``os.replace`` on a fresh temp file inside the same directory creates
    a new inode for the new bytes; the hardlink relationship is broken
    cleanly.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def apply_draft_segment(
    draft_path: str | Path,
    target_baseline_path: str | Path,
) -> None:
    """Replace (possibly hardlinked) ``target_baseline_path`` with contents
    of ``draft_path``. Unlinks the target first so a shared-inode source
    never sees the new bytes.

    - ``draft_path`` is moved (``shutil.move``), not copied, so the draft
      file no longer exists after success. Caller can assume draft is gone.
    - If ``draft_path`` does not exist, raises FileNotFoundError (callers
      should have already checked via ``tts_segments_draft`` enumeration).
    - If ``target_baseline_path`` does not exist (edge case: user edited a
      segment with no baseline audio), we create it fresh — no unlink.
    """
    draft = Path(draft_path)
    target = Path(target_baseline_path)
    if not draft.is_file():
        raise FileNotFoundError(f"draft not found: {draft}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Break any hardlink relationship before installing the new content.
        # Using unlink + move (instead of open('wb') overwrite) preserves
        # the source's inode intact.
        target.unlink()
    shutil.move(str(draft), str(target))


# ---------------------------------------------------------------------------
# Full Phase A orchestration
# ---------------------------------------------------------------------------


def _apply_voice_map_to_segments(
    segments: list[dict[str, Any]],
    voice_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a new list where every segment whose segment_id is in
    voice_map has its ``tts_provider`` + ``voice_id`` overwritten.
    Segments not in voice_map pass through unchanged. Original list
    not mutated.

    Uses the canonical ``tts_provider`` field (DubbingSegment /
    editing_tts / γ loader contract). segment_id lookup normalises
    via ``str()`` so int-typed legacy ids still match voice_map keys
    (which are always str per ``load_voice_map``).

    Phase 4.2 E.1 PR #15 P1 五轮 fix (Codex 2026-05-27): propagate
    CosyVoice clone worker routing (``requires_worker`` /
    ``worker_target_model``) so copy_as_new targets carry the routing
    through. Without this, copy_as_new would write a target
    segments.json with the clone voice_id but no routing fields →
    pipeline re-TTS falls back to legacy CosyVoice → clone voice
    silently doesn't take effect in the copied project.

    Stale routing cleanup: any segment with an override has its old
    routing fields popped first, then re-added only for clone overrides.
    This handles voice swap clone→builtin (stale flag must not stick)
    AND clone→different-clone (new target_model takes effect).
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        sid = seg.get("segment_id")
        override = voice_map.get(str(sid)) if sid is not None else None
        if override:
            new_seg = dict(seg)
            new_seg["tts_provider"] = override["provider"]
            new_seg["voice_id"] = override["voice_id"]
            if override.get("tts_model_key"):
                new_seg["tts_model_key"] = override["tts_model_key"]
            new_seg.pop("provider", None)  # scrub legacy misspelling
            # E.1 P1 五轮 fix: clear stale routing first, then re-add
            # only for clone overrides. Mirror of editing_commit._apply_voice_map.
            new_seg.pop("requires_worker", None)
            new_seg.pop("worker_target_model", None)
            if override.get("requires_worker") is True:
                new_seg["requires_worker"] = True
                target_model = override.get("worker_target_model")
                if isinstance(target_model, str) and target_model.strip():
                    new_seg["worker_target_model"] = target_model.strip()
            out.append(new_seg)
        else:
            out.append(seg)
    return out


def prepare_copy_project_dir(
    source_project_dir: str | Path,
    target_project_dir: str | Path,
) -> dict[str, Any]:
    """Build the target project_dir as a Phase A preparation (plan §7.8 A1-A4).

    Returns a summary dict on success. Raises ``CopyPreparationError`` on
    any failure. Caller owns rollback in the failure path.

    The source is treated as read-only; we only ever read ``editor/...``
    files from it. Target must not already exist (caller's error if it does).
    """
    source = Path(source_project_dir)
    target = Path(target_project_dir)
    if target.exists():
        raise CopyPreparationError(
            f"target project_dir already exists: {target}; refusing to overwrite"
        )
    editing_dir = source / "editor" / "editing"
    if not editing_dir.is_dir():
        raise CopyPreparationError(
            f"source has no editor/editing/ dir: {editing_dir}; "
            "copy_as_new requires an active editing session"
        )

    try:
        # A2: baseline JSON files. These are all path-bearing — rewrite
        # source project_dir refs to target or γ resume / publish will
        # read source paths back out of them. (2026-04-19 incident: a
        # copy with un-rewritten project_state.json / download_metadata.json
        # caused ProcessJobRunner._record_line to parse source paths out
        # of pipeline stdout and overwrite JobRecord.project_dir → source.)
        (target / "editor").mkdir(parents=True, exist_ok=True)
        for name in ("transcript.json", "manifest.json"):
            src_json = source / "editor" / name
            if src_json.is_file():
                _copy_json_with_path_rewrite(
                    src_json, target / "editor" / name,
                    source_dir=source, target_dir=target,
                )

        # A3: hardlink baseline wavs + media artifacts so pipeline's
        # per-stage file-existence cache-check skips re-running S0-S4.
        linked = hardlink_baseline_audio(source, target)
        hardlink_media_artifacts(source, target)

        # A3.1: copy (not hardlink) the small JSON cache markers —
        # pipeline may rewrite download_metadata.json on restart to
        # refresh paths and we must not mutate the source through a
        # shared inode. Also rewrite embedded source paths so γ resume
        # reads target paths, not source.
        for rel in (
            "download_metadata.json",
            "manifest.json",
            "transcript/transcript.json",
            # review_state.json carries approved voice_selection_review /
            # translation_review / speaker_review payloads — without these
            # on the target, pipeline's pre-alignment review gates re-open
            # and pause at stage 7 (see process.py:1220, CodeX stopgap
            # until start_stage='alignment' is wired end-to-end).
            "review_state.json",
        ):
            src_file = source / rel
            if src_file.is_file():
                _copy_json_with_path_rewrite(
                    src_file, target / rel,
                    source_dir=source, target_dir=target,
                )

        src_state = source / "project_state.json"
        if src_state.is_file():
            state_payload = json.loads(src_state.read_text(encoding="utf-8"))
            pruned = prune_project_state_payload(
                state_payload,
                new_project_id=Path(target_project_dir).name,
            )
            # Prune only resets stage statuses; it does NOT rewrite embedded
            # absolute paths inside stage payloads (e.g.
            # audio_preparation.speech_audio_path, legacy_process_output
            # .manifest_path). Apply the same rewrite rule used for
            # translation/segments.json so γ doesn't read stale source
            # paths out of the cached state.
            pruned = _rewrite_project_dir_paths(
                pruned, source_dir=source, target_dir=target,
            )
            atomic_write_json(str(target / "project_state.json"), pruned)

        # translation/segments.json embeds absolute tts/aligned paths
        # pointing at source — rewrite to target or alignment's in-place
        # DSP would mutate source audio via the old paths.
        src_translation = source / "translation" / "segments.json"
        if src_translation.is_file():
            dst_translation = target / "translation" / "segments.json"
            dst_translation.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(src_translation.read_text(encoding="utf-8"))
            payload = _rewrite_project_dir_paths(
                payload, source_dir=source, target_dir=target,
            )
            atomic_write_json(str(dst_translation), payload)

        # A4: apply editing diff
        # A4.1 segments.json: prefer editing/segments.json, fall back to baseline
        editing_segments_file = editing_dir / "segments.json"
        baseline_segments_file = source / "editor" / "segments.json"
        if editing_segments_file.is_file():
            segments_source_file = editing_segments_file
        elif baseline_segments_file.is_file():
            segments_source_file = baseline_segments_file
        else:
            segments_source_file = None
        segments: list[dict[str, Any]] = []
        if segments_source_file is not None:
            segments = json.loads(segments_source_file.read_text(encoding="utf-8"))
            if not isinstance(segments, list):
                raise CopyPreparationError(
                    f"segments JSON is not a list: {segments_source_file}"
                )
        # A4.2 voice_map.json — merge into segments
        voice_map_file = editing_dir / "voice_map.json"
        voice_map: dict[str, dict[str, Any]] = {}
        if voice_map_file.is_file():
            raw = json.loads(voice_map_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for sid, entry in raw.items():
                    if isinstance(entry, dict):
                        normalized: dict[str, Any] = {
                            "provider": str(entry.get("provider", "")).strip(),
                            "voice_id": str(entry.get("voice_id", "")).strip(),
                        }
                        model_key = str(
                            entry.get("tts_model_key")
                            or entry.get("tts_model")
                            or entry.get("model")
                            or ""
                        ).strip()
                        if model_key:
                            normalized["tts_model_key"] = model_key
                        # E.1 P1 五轮 fix (Codex 2026-05-27): preserve
                        # CosyVoice clone worker routing through copy_as_new
                        # so target segments.json carries
                        # ``requires_worker`` / ``worker_target_model``.
                        # Same strict ``is True`` defense as editing_commit.
                        if entry.get("requires_worker") is True:
                            normalized["requires_worker"] = True
                            target_model = entry.get("worker_target_model")
                            if isinstance(target_model, str) and target_model.strip():
                                normalized["worker_target_model"] = target_model.strip()
                        voice_map[str(sid)] = normalized
        if voice_map:
            segments = _apply_voice_map_to_segments(segments, voice_map)
        # Segments may carry absolute paths (tts_audio_path / aligned_audio_path)
        # inherited from source. Rewrite to target before write so γ resume's
        # editor-segments loader hands target paths to publish.
        segments = _rewrite_project_dir_paths(
            segments, source_dir=source, target_dir=target,
        )
        # A4.3 write merged segments.json to target
        target_segments_path = target / "editor" / "segments.json"
        target_segments_path.parent.mkdir(parents=True, exist_ok=True)
        target_segments_path.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # A4.4 draft wavs → target baseline via apply_draft_segment
        # We COPY (not move) drafts because the source's editing/ dir must
        # stay intact until Phase B deletes it — moving would leave Phase B
        # with nothing to clean if commit fails between A and B.
        drafts_dir = editing_dir / "tts_segments_draft"
        applied_drafts: list[str] = []
        if drafts_dir.is_dir():
            for draft_wav in sorted(drafts_dir.glob("*.wav")):
                segment_id = draft_wav.stem
                target_wav = target / "editor" / "tts_segments" / draft_wav.name
                # Copy first (draft stays intact), then install via apply
                copy_tmp = target / "editor" / "tts_segments" / f".{draft_wav.name}.copy"
                copy_tmp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(draft_wav, copy_tmp)
                apply_draft_segment(copy_tmp, target_wav)
                applied_drafts.append(segment_id)

        # A4.5 — re-stamp tts_input_cn_text on each segment whose draft
        # was promoted in A4.4. The draft was synthesized from that
        # segment's CURRENT cn_text and just replaced the new job's
        # baseline audio, so tts_input_cn_text must reflect cn_text or
        # downstream cue-pipeline drift detection (Phase B) would falsely
        # flag this in-sync segment as text↔audio drift.
        #
        # Mirrors editing_commit._apply_editing_to_baseline's stamp logic
        # for the overwrite path. Segments WITHOUT a promoted draft
        # intentionally retain their existing tts_input_cn_text — that
        # IS the drift state when user edits text without regen-tts.
        #
        # Plan ref: 2026-05-04-subtitle-audio-sync-plan.md Phase A
        # follow-up after CodeX review (P1 finding).
        if applied_drafts:
            applied_ids = set(applied_drafts)
            for seg in segments:
                sid = str(seg.get("segment_id", ""))
                if sid in applied_ids:
                    seg["tts_input_cn_text"] = seg.get("cn_text", "")
            target_segments_path.write_text(
                json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    except CopyPreparationError:
        raise
    except Exception as exc:
        raise CopyPreparationError(
            f"copy Phase A failed: {exc}"
        ) from exc

    return {
        "linked_segment_ids": linked,
        "applied_draft_segment_ids": applied_drafts,
        "segments_count": len(segments),
    }


def rollback_prepared_target(target_project_dir: str | Path) -> None:
    """Best-effort cleanup of a half-built target from a failed Phase A.

    Uses ``ignore_errors=True`` because (a) this runs inside an error
    handler — we must not mask the primary exception by raising here;
    (b) hardlinked wavs are safe to remove (source's inode survives until
    all links are gone, and we're only unlinking the target's).
    """
    target = Path(target_project_dir)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
