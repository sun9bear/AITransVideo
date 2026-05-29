"""Phase 2a Task 5 (gate #5) — the download / stream / eager-push TRIPLE gate must
EXPLICITLY restrict ``service_mode == "free"``.

All three functions in ``downloadable_keys`` default unknown/None -> Studio
(full), with ``express`` as the only restrictive branch. Without an explicit
free branch, a free job falls through to Studio = full access — a free user
could then reach gated artifacts via ``/stream/audio`` or the R2 eager-push set
even though ``/download`` looks limited. These assertions are RED until the free
branch lands.

Free mirrors express's restriction (watermarked video + poster only, no audio /
subtitles / drafts / post-edit) but the impl uses SEPARATE free constants so a
Phase 2b paid unlock can open free independently of express.
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from services.r2_publisher_lib.downloadable_keys import (  # noqa: E402
    download_keys_for,
    eager_push_keys_for,
    stream_kinds_for,
)

_VIDEO = "publish.dubbed_video"
_POSTER = "publish.dubbed_video_poster"


def _has_editor(keys) -> bool:
    return any(k.startswith("editor.") for k in keys)


# --- free: /download/{key} — only the (watermarked) finished video ---

def test_free_download_only_watermarked_video():
    keys = download_keys_for("free")
    assert keys == frozenset({_VIDEO})
    assert not _has_editor(keys)  # no subtitles / drafts / post-edit


# --- free: /stream/{kind} — the /stream/audio bypass ---

def test_free_stream_excludes_audio():
    kinds = stream_kinds_for("free")
    assert kinds == frozenset({"video", "poster"})
    assert "audio" not in kinds  # bypass assertion (plan Step 1)


# --- free: R2 eager-push set — the R2-prepush bypass ---

def test_free_eager_push_excludes_editor():
    keys = eager_push_keys_for("free")
    assert keys == frozenset({_VIDEO, _POSTER})
    assert not _has_editor(keys)  # bypass assertion: free eager-push has no editor.*


# --- free must be strictly more restrictive than the Studio default ---

def test_free_strictly_more_restricted_than_studio():
    assert download_keys_for("free") < download_keys_for("studio")
    assert stream_kinds_for("free") < stream_kinds_for("studio")
    assert eager_push_keys_for("free") < eager_push_keys_for("studio")


def test_free_does_not_fall_through_to_studio_default():
    # the bug the plan warns about: unknown/None -> Studio (full); free must be
    # an EXPLICIT branch, not the default.
    assert download_keys_for("free") != download_keys_for(None)
    assert stream_kinds_for("free") != stream_kinds_for(None)
    assert eager_push_keys_for("free") != eager_push_keys_for(None)


# --- regression: express / studio / unknown branches unchanged ---

def test_express_and_default_modes_unchanged():
    assert download_keys_for("express") == frozenset({_VIDEO})
    assert stream_kinds_for("express") == frozenset({"video", "poster"})
    assert eager_push_keys_for("express") == frozenset({_VIDEO, _POSTER})
    # unknown / None still defaults to Studio (full access)
    assert "audio" in stream_kinds_for(None)
    assert _has_editor(download_keys_for(None))
