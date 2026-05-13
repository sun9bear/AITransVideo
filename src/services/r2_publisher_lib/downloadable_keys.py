"""Downloadable artifact key allowlists, push set, content_type table.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §4.2

Three independent allowlists (the project's existing convention,
mirroring ``src/services/jobs/api.py``):

- ``*_ALLOWED_ARTIFACT_KEYS`` — ``/artifacts`` enumeration permission
- ``*_ALLOWED_DOWNLOAD_KEYS`` — ``/download/{key}`` permission
- ``*_ALLOWED_STREAM_KINDS``  — ``/stream/{kind}`` permission

These three are **distinct surfaces** and not interchangeable. The plan
v3 P2.1 issue was conflating download with artifact (poster is an
artifact key but not a download key). We import the canonical sets
into ``services.jobs.api`` so both ``Job API`` (process-side filter)
and ``Gateway`` (download intercept) reference one source of truth.

Separately, ``EAGER_PUSH_TO_R2_KEYS_*`` are the keys the sweeper
proactively uploads to R2. They include the download keys + the
poster (Stage C, plan §11.3 C1) so ``/stream/poster`` can 302 to R2
the same way ``/stream/video`` does. One intentional exclusion:

  - ``editor.tts_segments_zip``       — dynamically generated zip,
                                        not a manifest artifact

``editor.jianying_draft_zip`` is **conditionally** pushed: only when
the job's ``JobRecord.jianying_draft_zip_path`` is non-null (i.e. the
user has explicitly generated a draft). Logic lives in
``r2_publisher.publish_artifacts``; this module just lists the key.
"""

from __future__ import annotations


# === Permissions: must mirror src/services/jobs/api.py:31-40 字面量 ===

EXPRESS_ALLOWED_ARTIFACT_KEYS: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
})
EXPRESS_ALLOWED_DOWNLOAD_KEYS: frozenset[str] = frozenset({
    "publish.dubbed_video",
})
EXPRESS_ALLOWED_STREAM_KINDS: frozenset[str] = frozenset({"video", "poster"})

STUDIO_ALLOWED_ARTIFACT_KEYS: frozenset[str] = EXPRESS_ALLOWED_ARTIFACT_KEYS | frozenset({
    "editor.dubbed_audio_complete",
    "editor.subtitles",
    "editor.subtitles_en",
    "editor.subtitles_bilingual",
    "editor.tts_segments_zip",
    "editor.jianying_draft_zip",
})
STUDIO_ALLOWED_DOWNLOAD_KEYS: frozenset[str] = EXPRESS_ALLOWED_DOWNLOAD_KEYS | frozenset({
    "editor.dubbed_audio_complete",
    "editor.subtitles",
    "editor.subtitles_en",
    "editor.subtitles_bilingual",
    "editor.jianying_draft_zip",
})


# === R2 push set (separate from download permission, see module doc) ===

# Studio set, minus jianying (handled conditionally per-job).
#
# Stage C addition (plan 2026-05-07 §11.3 C1, 2026-05-12):
# ``publish.dubbed_video_poster`` is pushed eagerly so the /stream/poster
# endpoint can 302 to R2 without requiring a lazy upload on every cleanup
# cycle. Poster is small (~few hundred KB image/jpeg) so the marginal R2
# Class A write cost is negligible; the win is that download-after-cleanup
# parity stays consistent across all media a user can see in the workspace.
EAGER_PUSH_TO_R2_KEYS_STUDIO: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
    "editor.dubbed_audio_complete",
    "editor.subtitles",
    "editor.subtitles_en",
    "editor.subtitles_bilingual",
})
EAGER_PUSH_TO_R2_KEYS_EXPRESS: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
})


def download_keys_for(service_mode: str | None) -> frozenset[str]:
    """Return the download-permission set for the given service_mode.

    Default (unknown / None) = Studio. Express is the only restrictive
    branch and it matches src/services/jobs/api.py:_is_express_job
    (compares to literal ``"express"``).
    """
    if service_mode == "express":
        return EXPRESS_ALLOWED_DOWNLOAD_KEYS
    return STUDIO_ALLOWED_DOWNLOAD_KEYS


# Plan 2026-05-07 §11.3 C3-C4 (Stage C): Gateway side stream gate. The
# Studio whitelist mirrors what /stream/{kind} in src/services/jobs/api.py
# allows; Studio gets full (video / audio / poster), Express drops audio
# (per docs/plans/2026-04-18-express-studio-output-filter-plan.md).
STUDIO_ALLOWED_STREAM_KINDS: frozenset[str] = frozenset({"video", "audio", "poster"})


def stream_kinds_for(service_mode: str | None) -> frozenset[str]:
    """Return the /stream/{kind} permission set for the given service_mode.

    Mirrors ``download_keys_for`` semantics — Gateway uses this BEFORE
    issuing a 302 to R2 so an Express user can't smuggle ``/stream/audio``
    past the Job API allowlist by reaching Gateway directly.
    """
    if service_mode == "express":
        return EXPRESS_ALLOWED_STREAM_KINDS
    return STUDIO_ALLOWED_STREAM_KINDS


# Plan 2026-05-07 §11.3 C3 (Stage C): map /stream/{kind} to the underlying
# artifact_key the R2 registry / lazy-upload lookup expects. Stays in this
# module so the kind ↔ artifact_key contract has exactly one source of
# truth (mirrors what src/services/jobs/api.py:466-474 does locally).
_STREAM_KIND_TO_ARTIFACT_KEY: dict[str, str] = {
    "video":  "publish.dubbed_video",
    "audio":  "editor.dubbed_audio_complete",
    "poster": "publish.dubbed_video_poster",
}


def artifact_key_for_stream_kind(kind: str) -> str | None:
    """Translate a /stream/{kind} segment to its artifact_key, or None if
    the kind isn't streamable. Never raises; unknown kinds fall through
    to the local byte-passthrough.
    """
    return _STREAM_KIND_TO_ARTIFACT_KEY.get(kind)


def eager_push_keys_for(service_mode: str | None) -> frozenset[str]:
    """Return the keys the sweeper proactively pushes for this service_mode.

    Caller adds ``editor.jianying_draft_zip`` separately when
    ``JobRecord.jianying_draft_zip_path`` is non-null.
    """
    if service_mode == "express":
        return EAGER_PUSH_TO_R2_KEYS_EXPRESS
    return EAGER_PUSH_TO_R2_KEYS_STUDIO


# === content_type derivation ===

# Keep the table tight — only artifact keys we actually touch. Any key
# missing from the table falls through to ``application/octet-stream``
# and the Save-As filename keeps its real extension via
# ``r2_publisher._filename_for``.
_CONTENT_TYPE_BY_KEY: dict[str, str] = {
    "publish.dubbed_video": "video/mp4",
    "publish.dubbed_video_poster": "image/jpeg",
    "editor.dubbed_audio_complete": "audio/wav",
    "editor.subtitles": "text/plain; charset=utf-8",
    "editor.subtitles_en": "text/plain; charset=utf-8",
    "editor.subtitles_bilingual": "text/plain; charset=utf-8",
    "editor.jianying_draft_zip": "application/zip",
}


def content_type_for(artifact_key: str) -> str:
    return _CONTENT_TYPE_BY_KEY.get(artifact_key, "application/octet-stream")
