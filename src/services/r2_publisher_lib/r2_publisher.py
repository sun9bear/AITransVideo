"""Proactively push downloadable artifacts to R2.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §4.4

Design rules
------------

- **Idempotent.** HEAD-then-PUT skips already-present R2 keys. Repeated
  invocations on the same job converge to the same registry shape.
- **Failure local.** A single failing key is recorded as
  ``state="failed"`` and the loop continues; other keys are unaffected.
- **service_mode aware.** Express jobs only push the keys their
  download surface exposes (avoids paying R2 PUT cost for objects no
  one can fetch).
- **Manifest-strict (v4 P2.1 fix).** If ``manifest.json`` itself is
  missing / unreadable / has an empty ``artifact_index``, *every*
  expected entry is recorded as ``state="failed"``. This is the
  invariant the Stage B parity gate relies on — a missing manifest
  must NOT be confused with "all keys legitimately not generated".
- **No DB writes.** The caller (``r2_artifact_sweeper._run_publish``)
  owns the transaction boundary and decides how to merge the
  PublishResult back into the Job row.
- **No services.jobs imports.** Module lives in r2_publisher_lib so
  Gateway can call it without dragging the pydub-laden
  ``services.jobs`` package into its process.

Conditional artifacts
---------------------

``editor.jianying_draft_zip`` is in the user-facing Studio download
allowlist but **not** in EAGER_PUSH_TO_R2_KEYS_STUDIO. The user
generates Jianying drafts on demand via a separate endpoint, and
``JobRecord.jianying_draft_zip_path`` is null until they click. The
sweeper passes ``has_jianying_draft=True`` only for jobs whose JSON
record has the path set, so we add jianying to the eligible set then
and not before. (Express jobs never have jianying.)

``push_keys`` lets the sweeper request a delta push (e.g. "this job
already has 5 entries, but jianying just appeared, push only that
one"). When ``push_keys`` is None, we push the full eligible set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from services.r2_publisher_lib.downloadable_keys import (
    content_type_for,
    eager_push_keys_for,
)

logger = logging.getLogger(__name__)


EntryState = Literal["pushed", "already_present", "skipped_missing", "failed"]


@dataclass
class ArtifactRegistryEntry:
    """One row in ``Job.r2_artifacts`` JSONB array.

    Per-state field rules:

    - ``pushed`` / ``already_present``: r2_key, filename, content_type,
      size, source_mtime_ns are all populated. ``error`` is None.
    - ``skipped_missing``: only the bookkeeping fields (artifact_key,
      edit_generation, state, pushed_at) are set. The download path
      treats this entry as "this job legitimately does not have this
      artifact" and falls through to a 404, never to lazy upload.
    - ``failed``: ``error`` carries the short reason. The sweeper sets
      ``r2_push_retry_after = now+5min`` so this job re-enters the
      candidate set on the next pass.
    """

    artifact_key: str
    edit_generation: int
    state: EntryState
    r2_key: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    source_mtime_ns: int | None = None
    error: str | None = None
    pushed_at: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "artifact_key": self.artifact_key,
            "edit_generation": self.edit_generation,
            "state": self.state,
            "pushed_at": self.pushed_at,
        }
        for f in (
            "r2_key",
            "filename",
            "content_type",
            "size",
            "source_mtime_ns",
            "error",
        ):
            v = getattr(self, f)
            if v is not None:
                d[f] = v
        return d


@dataclass
class PublishResult:
    entries: list[ArtifactRegistryEntry] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """True iff no entry is in ``failed`` state.

        ``skipped_missing`` is not failure — it represents a legitimate
        gap (e.g. a Studio job whose subtitles_en wasn't generated).
        Stage B parity treats it the same as a successful push.
        """
        return not any(e.state == "failed" for e in self.entries)


def _resolve_eligible(
    *,
    service_mode: str | None,
    has_jianying_draft: bool,
    push_keys: frozenset[str] | None,
) -> frozenset[str]:
    if push_keys is not None:
        # Caller explicitly scoped the push (delta mode).
        return push_keys
    keys = eager_push_keys_for(service_mode)
    if has_jianying_draft:
        keys = keys | frozenset({"editor.jianying_draft_zip"})
    return keys


def _make_failed_batch(
    keys: Iterable[str],
    edit_generation: int,
    error: str,
    now_iso: str,
) -> list[ArtifactRegistryEntry]:
    return [
        ArtifactRegistryEntry(
            artifact_key=k,
            edit_generation=edit_generation,
            state="failed",
            error=error,
            pushed_at=now_iso,
        )
        for k in sorted(keys)
    ]


def _filename_for(artifact_key: str, base: str, local_path: Path) -> str:
    """Pick a friendly filename for the user's Save-As dialog.

    Mirrors what ``gateway/job_intercept.py:_derive_download_filename``
    does for ``publish.dubbed_video`` but extends it to the other
    artifact keys with stable suffixes so multi-language SRT downloads
    don't all save as ``my_video.srt``.
    """
    name = (base or local_path.stem).strip() or "download"
    if artifact_key == "publish.dubbed_video":
        return f"{name}.mp4"
    if artifact_key == "publish.dubbed_video_poster":
        return f"{name}_poster.jpg"
    if artifact_key == "editor.dubbed_audio_complete":
        return f"{name}.wav"
    if artifact_key == "editor.subtitles":
        return f"{name}_zh.srt"
    if artifact_key == "editor.subtitles_en":
        return f"{name}_en.srt"
    if artifact_key == "editor.subtitles_bilingual":
        return f"{name}_bilingual.srt"
    if artifact_key == "editor.jianying_draft_zip":
        # 2026-05-11 production bug fix:
        # The jianying zip on disk is named "{title}_{YYYY-MM-DD}.zip"
        # by ``jianying_draft_writer._resolve_zip_basename``. Critically,
        # the writer also uses ``Path(zip_path).stem`` as the INTERNAL
        # folder name embedded in draft_content.json material paths
        # (line 399 in jianying_draft_writer.py).
        #
        # 剪映 expects to find materials at
        # ``{drafts_root}/{zip_stem}/materials/dubbed_audio.wav``. If
        # the downloaded zip filename doesn't match the zip's internal
        # folder stem, the user unzips into ``{title}_jianying/`` while
        # the material paths inside still point to
        # ``{title}_{YYYY-MM-DD}/materials/...`` → 剪映 reports
        # "媒体丢失".
        #
        # Honour the on-disk filename verbatim — it was carefully
        # constructed to match the internal folder stem.
        return local_path.name
    return f"{name}{local_path.suffix or ''}"


def publish_artifacts(
    *,
    job_id: str,
    service_mode: str | None,
    edit_generation: int,
    project_dir: Path,
    base_filename: str,
    has_jianying_draft: bool = False,
    jianying_draft_zip_path: str | None = None,
    push_keys: frozenset[str] | None = None,
) -> PublishResult:
    """Push (a subset of) downloadable artifacts to R2.

    Parameters
    ----------
    job_id
        Used in the R2 object key (``jobs/{job_id}/g{N}/...``).
    service_mode
        ``"express"`` restricts the eligible set; everything else (None
        included) takes the Studio set. Mirrors ``_is_express_job``.
    edit_generation
        Stamped into the R2 object key so an overwrite lands on a
        physically distinct path. Reads ``Job.edit_generation``; pass
        0 for fresh jobs.
    project_dir
        The job's project directory. We expect ``manifest.json`` at
        the root and resolve artifact paths through
        ``services.manifest_reader`` for keys that live in the
        artifact_index.
    base_filename
        User-visible name used to derive Save-As filenames per
        artifact (e.g. ``"my_video"`` → ``"my_video.mp4"``).
    has_jianying_draft
        Caller (sweeper) reads ``JobRecord.jianying_draft_zip_path``
        from JSON store and passes True iff non-null. We never look at
        the value here — the sweeper owns that signal.
    jianying_draft_zip_path
        Absolute (or project-relative) path to the on-demand-generated
        Jianying draft zip. **Required** when ``has_jianying_draft`` is
        True or ``"editor.jianying_draft_zip" in push_keys``. The
        manifest does NOT contain this artifact — the runner generates
        it after publish (see CodeX 4 P1-B / project_resolver) — so we
        cannot resolve it via ``resolve_manifest_artifact_path``. Caller
        sources this from ``JobRecord.jianying_draft_zip_path``.
    push_keys
        ``None`` = push full eligible set. ``frozenset(...)`` = push
        only these keys (delta mode, e.g. just-generated jianying).

    Returns
    -------
    PublishResult
        ``entries`` always covers every key in the resolved eligible
        set. The caller is responsible for either replacing
        ``Job.r2_artifacts`` wholesale (full push) or merging entries
        into the existing array (delta push).
    """
    # Imports kept inside the function so importing this module costs
    # nothing — boto3 / manifest_reader are only paid for by callers
    # that actually run the publisher.
    from services.manifest_reader import (
        load_manifest_artifact_index,
        load_manifest_payload,
        resolve_manifest_artifact_path,
    )
    from storage import r2_client
    from storage.backend_router import r2_key_for

    result = PublishResult()
    now_iso = datetime.now(timezone.utc).isoformat()

    eligible = _resolve_eligible(
        service_mode=service_mode,
        has_jianying_draft=has_jianying_draft,
        push_keys=push_keys,
    )
    if not eligible:
        return result

    # ---- v4 P2.1: manifest-strict gate ----
    # A vanished project_dir / manifest is a critical signal — we MUST
    # not silently mark every key as skipped_missing because Stage B
    # parity treats skipped_missing as "OK to clean up local". That
    # would let the cleanup step delete the on-disk project for a job
    # whose R2 copy doesn't exist either.
    if not project_dir.is_dir():
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "project_dir missing", now_iso,
        ))
        return result

    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "manifest.json missing", now_iso,
        ))
        return result

    payload = load_manifest_payload(project_dir=project_dir)
    if payload is None:
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "manifest unreadable / not json", now_iso,
        ))
        return result

    artifact_index = load_manifest_artifact_index(manifest_payload=payload)
    if not artifact_index:
        result.entries.extend(_make_failed_batch(
            eligible, edit_generation, "manifest artifact_index empty", now_iso,
        ))
        return result

    # ---- per-key processing ----
    for key in sorted(eligible):
        # P1-B (CodeX 4): jianying draft zip is NOT in the manifest's
        # artifact_index. The runner writes it post-publish (see
        # services.web_ui.project_resolver convention) and stores the
        # absolute path in JobRecord.jianying_draft_zip_path. Caller
        # passes that value as ``jianying_draft_zip_path`` and we use it
        # directly. If we tried manifest resolution here we'd record
        # skipped_missing → registry would lock in that decision and
        # the sweeper would never come back, leaving cleanup free to
        # delete the on-disk zip.
        if key == "editor.jianying_draft_zip":
            local_path = (
                Path(jianying_draft_zip_path)
                if jianying_draft_zip_path
                else None
            )
            # Allow a project-relative fallback if the JSON store carries
            # a relative path (some legacy rows did).
            if local_path is not None and not local_path.is_absolute():
                local_path = (project_dir / local_path).resolve(strict=False)
        else:
            try:
                local_path = resolve_manifest_artifact_path(
                    project_dir, key, artifact_index=artifact_index,
                )
            except Exception as exc:  # defensive — manifest_reader catches most
                result.entries.append(ArtifactRegistryEntry(
                    artifact_key=key,
                    edit_generation=edit_generation,
                    state="failed",
                    error=f"resolve: {exc}",
                    pushed_at=now_iso,
                ))
                continue

        if local_path is None or not local_path.exists():
            # Manifest is valid (we just gated on it above) but this
            # particular artifact wasn't generated — e.g. a Studio
            # job that didn't produce subtitles_en, or a Jianying zip
            # whose JSON path got cleared by an overwrite.
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key,
                edit_generation=edit_generation,
                state="skipped_missing",
                pushed_at=now_iso,
            ))
            continue

        ctype = content_type_for(key)
        r2_key = r2_key_for(
            job_id,
            key,
            local_path=local_path,
            edit_generation=edit_generation,
        )
        filename = _filename_for(key, base_filename, local_path)

        # HEAD then PUT. r2_client lazy-imports boto3 so this is the
        # first place in the publisher path that costs anything.
        try:
            already = r2_client.head_artifact(r2_key)
            if not already:
                r2_client.upload_artifact(local_path, r2_key, content_type=ctype)
                state: EntryState = "pushed"
            else:
                state = "already_present"
        except Exception as exc:
            logger.warning(
                "publish_artifacts: PUT/HEAD failed job=%s key=%s (%s)",
                job_id, key, exc,
            )
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key,
                edit_generation=edit_generation,
                state="failed",
                error=str(exc),
                pushed_at=now_iso,
            ))
            continue

        try:
            stat = local_path.stat()
        except OSError as exc:
            # Race: file disappeared between resolve and stat. Treat
            # as failure rather than skipped_missing — the upload may
            # have succeeded but we can't certify the local snapshot.
            result.entries.append(ArtifactRegistryEntry(
                artifact_key=key,
                edit_generation=edit_generation,
                state="failed",
                error=f"stat: {exc}",
                pushed_at=now_iso,
            ))
            continue

        result.entries.append(ArtifactRegistryEntry(
            artifact_key=key,
            edit_generation=edit_generation,
            state=state,
            r2_key=r2_key,
            filename=filename,
            content_type=ctype,
            size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            pushed_at=now_iso,
        ))

    return result
