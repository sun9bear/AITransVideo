"""r2_publisher_lib.r2_publisher contract tests.

Coverage (plan 2026-05-07 §4.4 + CodeX 4 P1-B follow-up):

- manifest.json missing / unreadable / artifact_index empty → entire
  eligible set marked ``failed`` (P2.1 invariant; do NOT silently
  degrade to skipped_missing or parity will let cleanup delete the
  on-disk source).
- service_mode=express → only EAGER_PUSH_TO_R2_KEYS_EXPRESS pushed
  (publish.dubbed_video, no studio extras).
- service_mode=studio → full studio set pushed; subtitles_en absent
  on disk records ``skipped_missing`` for THAT key only, others land
  ``pushed`` / ``already_present``.
- jianying conditional push: ``jianying_draft_zip_path`` is honored
  via direct path, NOT via manifest.artifact_index. Without the
  P1-B fix this would record skipped_missing and the registry would
  freeze the bad decision.
- edit_generation is stamped into the R2 key shape ``g{N}/``.
- HEAD-hit short-circuits PUT (idempotency).

No live boto3 / DB. ``r2_client`` helpers are monkeypatched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO / "gateway"
SRC_DIR = REPO / "src"
for _p in (str(GATEWAY_DIR), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---- Fakes ------------------------------------------------------------------


class FakeR2:
    """Records HEAD/PUT calls. Configurable per-test."""

    def __init__(self) -> None:
        self.head_calls: list[str] = []
        self.upload_calls: list[tuple[Path, str, str]] = []
        # head_returns: keyed per-r2-key. Default False.
        self.head_returns: dict[str, bool] = {}
        self.put_exc: Exception | None = None

    def head_artifact(self, key: str) -> bool:
        self.head_calls.append(key)
        return self.head_returns.get(key, False)

    def upload_artifact(self, local_path: Path, key: str, content_type: str = "video/mp4") -> None:
        self.upload_calls.append((Path(local_path), key, content_type))
        if self.put_exc is not None:
            raise self.put_exc


@pytest.fixture
def fake_r2(monkeypatch):
    fake = FakeR2()
    # Import lazily so each test's monkeypatch installs cleanly even if
    # a previous test already imported r2_client.
    import storage.r2_client as r2_client
    monkeypatch.setattr(r2_client, "head_artifact", fake.head_artifact)
    monkeypatch.setattr(r2_client, "upload_artifact", fake.upload_artifact)
    return fake


def _make_project_with_manifest(
    tmp_path: Path, *, artifact_files: dict[str, str]
) -> Path:
    """Materialise a project_dir with a manifest pointing to real files.

    artifact_files maps artifact_key → relative path. The relative path's
    parent is created and a tiny placeholder file written so
    resolve_manifest_artifact_path returns a real Path.
    """
    project_dir = tmp_path / "proj_test"
    project_dir.mkdir()
    artifact_index: dict[str, str] = {}
    for key, rel in artifact_files.items():
        full = project_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"x" * 8)
        artifact_index[key] = rel
    manifest = {"artifact_index": artifact_index}
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    return project_dir


# ---- P2.1: manifest-strict gate ---------------------------------------------


def test_manifest_missing_marks_entire_batch_failed(fake_r2, tmp_path):
    """If manifest.json doesn't exist, every eligible key is recorded as
    ``failed`` (NOT skipped_missing). Stage B parity then refuses to
    consider this job clean — the on-disk project must stay until an
    operator looks at it. This is the P2.1 invariant.
    """
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = tmp_path / "proj_no_manifest"
    project_dir.mkdir()
    # No manifest.json written.

    result = publish_artifacts(
        job_id="job_x",
        service_mode="studio",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="video",
    )
    states = {e.artifact_key: e.state for e in result.entries}
    # Every studio-eager key recorded (not just one).
    assert "publish.dubbed_video" in states
    assert "editor.subtitles" in states
    assert all(s == "failed" for s in states.values()), states
    assert all(
        "manifest" in (e.error or "").lower()
        for e in result.entries
    ), [(e.artifact_key, e.error) for e in result.entries]
    assert result.all_ok is False
    assert fake_r2.upload_calls == [], "PUT must not happen on manifest miss"


def test_manifest_empty_artifact_index_marks_failed(fake_r2, tmp_path):
    """An empty artifact_index is not the same as 'no artifacts' — it's
    a corrupted / partial write. Treat as failed batch."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = tmp_path / "proj_empty_idx"
    project_dir.mkdir()
    (project_dir / "manifest.json").write_text(
        json.dumps({"artifact_index": {}}), encoding="utf-8",
    )

    result = publish_artifacts(
        job_id="job_x",
        service_mode="studio",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="video",
    )
    assert all(e.state == "failed" for e in result.entries)


# ---- service_mode filtering -------------------------------------------------


def test_express_mode_only_publishes_dubbed_video(fake_r2, tmp_path):
    """Express download surface has exactly one key. Pushing studio
    extras for an Express job would be a write-amplification waste +
    surface a key the user can't reach via /download."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
        "editor.subtitles": "subs/zh.srt",
    })

    result = publish_artifacts(
        job_id="job_e",
        service_mode="express",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="vid",
    )
    keys = {e.artifact_key for e in result.entries}
    assert keys == {"publish.dubbed_video"}, keys
    assert result.entries[0].state == "pushed"
    # No subtitles upload despite manifest listing one
    assert all("subtitles" not in k for _, k, _ in fake_r2.upload_calls)


def test_studio_full_set_with_one_key_missing_records_skipped_missing(
    fake_r2, tmp_path,
):
    """Manifest is valid + has the entry, but the file itself doesn't
    exist (Studio job legitimately didn't generate subtitles_en).
    Single-key skipped_missing — does NOT fail the others."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
        "editor.dubbed_audio_complete": "audio/dubbed.wav",
        "editor.subtitles": "subs/zh.srt",
        # Note: subtitles_en intentionally absent from manifest+disk.
        "editor.subtitles_bilingual": "subs/bi.srt",
    })

    result = publish_artifacts(
        job_id="job_s",
        service_mode="studio",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="vid",
    )
    by_key = {e.artifact_key: e.state for e in result.entries}
    assert by_key["publish.dubbed_video"] == "pushed"
    assert by_key["editor.subtitles"] == "pushed"
    assert by_key["editor.subtitles_en"] == "skipped_missing"
    # Skipped_missing is NOT failure — overall result is OK.
    assert result.all_ok is True


# ---- P1-B (CodeX 4): jianying via direct path -------------------------------


def test_jianying_pushed_via_direct_path_not_manifest(fake_r2, tmp_path):
    """Jianying zip is written by the runner POST-publish, not via
    manifest.artifact_index. Caller passes ``jianying_draft_zip_path``
    sourced from JobRecord; publisher uses it directly.

    Without the P1-B fix the publisher would call
    resolve_manifest_artifact_path('editor.jianying_draft_zip') →
    None → record skipped_missing → registry freezes that decision
    → cleanup deletes the on-disk zip → user loses core deliverable.
    """
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    # Manifest only has the regular Studio artifacts; jianying zip lives
    # at a path the runner picked, NOT in artifact_index.
    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
        "editor.dubbed_audio_complete": "audio/dubbed.wav",
        "editor.subtitles": "subs/zh.srt",
        "editor.subtitles_en": "subs/en.srt",
        "editor.subtitles_bilingual": "subs/bi.srt",
    })
    jianying_zip = project_dir / "jianying" / "exports" / "draft_v1.zip"
    jianying_zip.parent.mkdir(parents=True)
    jianying_zip.write_bytes(b"PK\x03\x04" + b"\0" * 32)

    result = publish_artifacts(
        job_id="job_j",
        service_mode="studio",
        edit_generation=2,
        project_dir=project_dir,
        base_filename="vid",
        has_jianying_draft=True,
        jianying_draft_zip_path=str(jianying_zip),
    )
    by_key = {e.artifact_key: e for e in result.entries}
    assert "editor.jianying_draft_zip" in by_key
    jianying_entry = by_key["editor.jianying_draft_zip"]
    assert jianying_entry.state == "pushed", (
        f"expected pushed, got {jianying_entry.state} "
        f"(error={jianying_entry.error}). P1-B regression: jianying must "
        f"resolve via direct path, not manifest."
    )
    # R2 key carries the generation prefix and the .zip suffix.
    assert jianying_entry.r2_key == "jobs/job_j/g2/editor.jianying_draft_zip.zip"
    # PUT actually issued.
    uploaded_keys = [k for _, k, _ in fake_r2.upload_calls]
    assert "jobs/job_j/g2/editor.jianying_draft_zip.zip" in uploaded_keys
    # content_type derived as application/zip.
    jianying_uploads = [
        (p, k, ct) for (p, k, ct) in fake_r2.upload_calls
        if k.endswith("editor.jianying_draft_zip.zip")
    ]
    assert jianying_uploads, "jianying upload missing"
    assert jianying_uploads[0][2] == "application/zip"


def test_jianying_download_filename_matches_on_disk_stem(fake_r2, tmp_path):
    """2026-05-11 production bug: jianying zip's Save-As filename was
    being generated as ``{base_filename}_jianying.zip`` while the
    on-disk zip and its INTERNAL material-path folder were named
    ``{title}_{YYYY-MM-DD}.zip`` / ``{title}_{YYYY-MM-DD}/`` by
    ``jianying_draft_writer._resolve_zip_basename``.

    Mismatch consequence: user downloads file as
    ``{title}_jianying.zip``, Windows unzips into
    ``{title}_jianying/``, but draft_content.json materials reference
    ``{title}_{YYYY-MM-DD}/materials/dubbed_audio.wav`` →
    剪映 reports "媒体丢失" on every open.

    Contract: the Save-As filename for jianying.zip MUST equal
    ``local_path.name`` so the unzipped folder stem matches what the
    JSON's material paths expect."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
        "editor.dubbed_audio_complete": "audio/dubbed.wav",
        "editor.subtitles": "subs/zh.srt",
        "editor.subtitles_en": "subs/en.srt",
        "editor.subtitles_bilingual": "subs/bi.srt",
    })
    # The runner names the zip exactly as _resolve_zip_basename does.
    jianying_zip = project_dir / "jianying" / "exports" / "黄仁勋演讲_2026-05-11.zip"
    jianying_zip.parent.mkdir(parents=True)
    jianying_zip.write_bytes(b"PK\x03\x04" + b"\0" * 32)

    result = publish_artifacts(
        job_id="job_j",
        service_mode="studio",
        edit_generation=1,
        project_dir=project_dir,
        base_filename="黄仁勋演讲",  # title without date — what publish stage typically passes
        has_jianying_draft=True,
        jianying_draft_zip_path=str(jianying_zip),
    )
    by_key = {e.artifact_key: e for e in result.entries}
    j = by_key["editor.jianying_draft_zip"]
    assert j.state == "pushed", j.error
    assert j.filename == "黄仁勋演讲_2026-05-11.zip", (
        f"jianying zip Save-As filename must equal local_path.name to "
        f"preserve the folder-stem invariant 剪映 needs to find materials. "
        f"Got: {j.filename!r}. Expected: '黄仁勋演讲_2026-05-11.zip'."
    )
    # Negative: must NOT use the old `_jianying.zip` suffix
    assert not j.filename.endswith("_jianying.zip"), (
        f"regression: jianying Save-As filename reverted to "
        f"`_jianying.zip` suffix ({j.filename!r}); breaks 剪映 material "
        f"resolution because folder stem inside the zip is dated."
    )


def test_filename_for_non_jianying_keys_unchanged(tmp_path):
    """Unit-level sanity: only jianying.zip's filename rule changed.
    Other artifact keys keep their old templated suffixes."""
    from services.r2_publisher_lib.r2_publisher import _filename_for

    p = tmp_path / "any.bin"
    p.write_bytes(b"")
    assert _filename_for("publish.dubbed_video", "myvid", p) == "myvid.mp4"
    assert _filename_for("publish.dubbed_video_poster", "myvid", p) == "myvid_poster.jpg"
    assert _filename_for("editor.dubbed_audio_complete", "myvid", p) == "myvid.wav"
    assert _filename_for("editor.subtitles", "myvid", p) == "myvid_zh.srt"
    assert _filename_for("editor.subtitles_en", "myvid", p) == "myvid_en.srt"
    assert _filename_for("editor.subtitles_bilingual", "myvid", p) == "myvid_bilingual.srt"


def test_jianying_delta_push_only_touches_jianying(fake_r2, tmp_path):
    """``push_keys={editor.jianying_draft_zip}`` runs ONLY the jianying
    branch. None of the studio-extras' R2 keys appear in HEAD/PUT
    calls. This is the delta path the sweeper takes when an existing
    registry is missing only the jianying entry."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
        "editor.subtitles": "subs/zh.srt",
    })
    jianying_zip = project_dir / "jianying" / "exports" / "j.zip"
    jianying_zip.parent.mkdir(parents=True)
    jianying_zip.write_bytes(b"PK")

    result = publish_artifacts(
        job_id="job_d",
        service_mode="studio",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="vid",
        has_jianying_draft=True,
        jianying_draft_zip_path=str(jianying_zip),
        push_keys=frozenset({"editor.jianying_draft_zip"}),
    )
    keys = {e.artifact_key for e in result.entries}
    assert keys == {"editor.jianying_draft_zip"}, keys
    # No HEAD on publish.dubbed_video etc.
    assert all("editor.jianying_draft_zip" in k for k in fake_r2.head_calls)


def test_jianying_path_missing_records_skipped_missing(fake_r2, tmp_path):
    """If jianying_draft_zip_path points at a vanished file, single-key
    skipped_missing — caller can decide whether to retry later."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
    })

    result = publish_artifacts(
        job_id="job_d",
        service_mode="studio",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="vid",
        has_jianying_draft=True,
        jianying_draft_zip_path=str(tmp_path / "no" / "such.zip"),
        push_keys=frozenset({"editor.jianying_draft_zip"}),
    )
    assert len(result.entries) == 1
    assert result.entries[0].state == "skipped_missing"
    assert result.entries[0].artifact_key == "editor.jianying_draft_zip"


# ---- edit_generation in the R2 key ------------------------------------------


def test_edit_generation_stamped_into_r2_key(fake_r2, tmp_path):
    """An overwrite-bumped job lands on a physically distinct R2 path
    so the original generation's objects don't HEAD-hit the new one."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
    })

    result = publish_artifacts(
        job_id="job_g",
        service_mode="express",
        edit_generation=3,
        project_dir=project_dir,
        base_filename="vid",
    )
    e = next(x for x in result.entries if x.artifact_key == "publish.dubbed_video")
    assert e.r2_key == "jobs/job_g/g3/publish.dubbed_video.mp4"
    assert e.state == "pushed"


# ---- HEAD-hit idempotency ---------------------------------------------------


def test_head_hit_skips_put(fake_r2, tmp_path):
    """If the object already exists in R2, the publisher records
    ``already_present`` without calling PUT. This is the steady-state
    after the first sweep — re-runs cost only a HEAD round-trip."""
    from services.r2_publisher_lib.r2_publisher import publish_artifacts

    project_dir = _make_project_with_manifest(tmp_path, artifact_files={
        "publish.dubbed_video": "video/final.mp4",
    })
    fake_r2.head_returns["jobs/job_h/g0/publish.dubbed_video.mp4"] = True

    result = publish_artifacts(
        job_id="job_h",
        service_mode="express",
        edit_generation=0,
        project_dir=project_dir,
        base_filename="vid",
    )
    e = result.entries[0]
    assert e.state == "already_present"
    assert fake_r2.upload_calls == []
