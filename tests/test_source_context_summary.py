import json
from pathlib import Path

from services.manifest_reader import load_manifest_payload
from services.source_context_summary import (
    build_empty_source_context_summary,
    build_source_context_summary,
    extract_source_context_from_manifest_payload,
)


def test_extract_source_context_from_manifest_payload_reads_canonical_fields() -> None:
    payload = {
        "source_info": {
            "source_kind": "youtube_url",
            "source_url": "https://www.youtube.com/watch?v=demo",
            "metadata": {
                "video_title": "Canonical Demo Title",
            },
        }
    }

    assert extract_source_context_from_manifest_payload(payload) == {
        "source_kind": "youtube_url",
        "locator": "https://www.youtube.com/watch?v=demo",
        "video_title": "Canonical Demo Title",
    }


def test_build_source_context_summary_applies_stage_and_locator_fallbacks(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({}), encoding="utf-8")

    summary = build_source_context_summary(
        manifest_path=str(manifest_path),
        stage_snapshot={
            "media_understanding": {
                "payload": {
                    "source_kind": "local_video",
                }
            }
        },
        fallback_locator=str(tmp_path / "video.mp4"),
    )

    assert summary == {
        "source_kind": "local_video",
        "locator": str(tmp_path / "video.mp4"),
        "video_title": None,
    }
    assert build_empty_source_context_summary() == {
        "source_kind": None,
        "locator": None,
        "video_title": None,
    }
    assert load_manifest_payload(manifest_path=manifest_path) == {}


def test_build_source_context_summary_reads_manifest_by_project_dir(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_info": {
                    "source_kind": "local_video",
                    "locator": str(tmp_path / "nested" / "source.mp4"),
                    "metadata": {
                        "video_title": "Project Dir Manifest",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_manifest_payload(project_dir=tmp_path) == {
        "source_info": {
            "source_kind": "local_video",
            "locator": str(tmp_path / "nested" / "source.mp4"),
            "metadata": {
                "video_title": "Project Dir Manifest",
            },
        }
    }
    assert build_source_context_summary(manifest_payload=load_manifest_payload(project_dir=tmp_path)) == {
        "source_kind": "local_video",
        "locator": str(tmp_path / "nested" / "source.mp4"),
        "video_title": "Project Dir Manifest",
    }
