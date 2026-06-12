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


# === Phase 2a free tier (gate #5) ===
#
# Free mirrors EXPRESS's restriction (watermarked finished video + poster only;
# NO audio / subtitles / drafts / post-edit artifacts) but uses SEPARATE
# constants and an EXPLICIT branch in all three gate functions — never the
# Studio default — so a free user cannot smuggle gated artifacts via
# /stream/audio or the R2 eager-push set. Kept DISTINCT from EXPRESS (not
# aliased) so a Phase 2b paid unlock can open free independently.
FREE_ALLOWED_DOWNLOAD_KEYS: frozenset[str] = frozenset({
    "publish.dubbed_video",
})
FREE_ALLOWED_STREAM_KINDS: frozenset[str] = frozenset({"video", "poster"})
EAGER_PUSH_TO_R2_KEYS_FREE: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
})


def effective_policy_mode(service_mode: str | None, anonymous_preview) -> str | None:
    """策略档单点解析（plan 2026-06-12 anonymous-express-preview §C）。

    ``anonymous_preview`` 真值 → 恒返回 ``"anonymous_preview"``（最严档：
    恒水印 / 零下载 / 仅 stream video / 不进 R2）。否则原样透传
    ``service_mode``——非匿名任务的策略行为零变化。

    背景：匿名 express 任务的 ``service_mode == "express"``，直接拿
    service_mode 查策略表会命中 express 档（放行成片下载、R2 redirect）。
    所有 mode→策略 判定点必须先经本函数（AST 守卫：
    tests/test_anonymous_express_t3_policy_fail_closed.py 钉死 §C 文件
    不得新增绕过 helper 的 service_mode 字面量比较）。

    八点清单（§C）：① 水印 free_watermark_text_for ② download_keys_for
    ③ stream_kinds_for ④ Job API 下载门 ⑤ Job API stream 门
    ⑥ Gateway R2 redirect（download/stream）⑦ Job API artifacts 列表
    ⑧ R2 sweeper（既有 is_anonymous_preview 短路）。
    """
    if anonymous_preview:
        return "anonymous_preview"
    return service_mode


def download_keys_for(service_mode: str | None) -> frozenset[str]:
    """Return the download-permission set for the given service_mode.

    Default (unknown / None) = Studio. Express and free are the restrictive
    branches (free = Phase 2a gate #5); express matches
    src/services/jobs/api.py:_is_express_job (literal ``"express"``).

    ``anonymous_preview`` (APF P0): stream-only, no download keys — the
    anonymous preview funnel is explicitly stream-only (AD-6).  Kept as an
    explicit branch (never the Studio default) so a future configuration
    change cannot accidentally grant download access via a mode fallthrough.
    """
    if service_mode == "express":
        return EXPRESS_ALLOWED_DOWNLOAD_KEYS
    if service_mode == "free":
        return FREE_ALLOWED_DOWNLOAD_KEYS
    if service_mode == "anonymous_preview":
        # AD-6: anonymous previews are stream-only; no download permitted.
        return frozenset()
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

    ``anonymous_preview`` (APF P0): only ``video`` — no audio, no poster
    download.  Explicit branch (not aliased to free) so future per-mode
    unlock can open them independently.
    """
    if service_mode == "express":
        return EXPRESS_ALLOWED_STREAM_KINDS
    if service_mode == "free":
        return FREE_ALLOWED_STREAM_KINDS
    if service_mode == "anonymous_preview":
        # AD-6 / AD-9: anonymous preview allows streaming video only.
        return frozenset({"video"})
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

    ``anonymous_preview`` (APF P0): empty set — preview artifacts are
    served stream-only via Job API proxy (AD-6); no eager R2 push.
    Explicit branch so the sweeper never silently applies the Studio
    default to anonymous jobs.
    """
    if service_mode == "express":
        return EAGER_PUSH_TO_R2_KEYS_EXPRESS
    if service_mode == "free":
        return EAGER_PUSH_TO_R2_KEYS_FREE
    if service_mode == "anonymous_preview":
        # AD-6: no R2 push for anonymous preview — local stream-only.
        return frozenset()
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
