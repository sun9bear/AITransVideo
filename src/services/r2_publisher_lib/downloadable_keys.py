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
proactively uploads to R2. They are a strict subset of the download
keys per service mode, with two intentional exclusions:

  - ``editor.tts_segments_zip``       — dynamically generated zip,
                                        not a manifest artifact
  - ``editor.publish.dubbed_video_poster`` — poster is a stream-only
                                        target, never reachable via
                                        ``/download/{key}``

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
EAGER_PUSH_TO_R2_KEYS_STUDIO: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "editor.dubbed_audio_complete",
    "editor.subtitles",
    "editor.subtitles_en",
    "editor.subtitles_bilingual",
})
EAGER_PUSH_TO_R2_KEYS_EXPRESS: frozenset[str] = frozenset({
    "publish.dubbed_video",
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
