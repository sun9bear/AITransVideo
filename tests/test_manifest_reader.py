import json
from pathlib import Path

from services.manifest_reader import (
    load_manifest_artifact_index,
    load_manifest_payload,
    resolve_manifest_artifact_path,
)


def test_load_manifest_artifact_index_filters_blank_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_index": {
                    "editor.dubbed_audio_complete": " output/final_mix.wav ",
                    "": "output/ignored.wav",
                    "publish.dubbed_video": "   ",
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_manifest_payload(project_dir=tmp_path) == {
        "artifact_index": {
            "editor.dubbed_audio_complete": " output/final_mix.wav ",
            "": "output/ignored.wav",
            "publish.dubbed_video": "   ",
        }
    }
    assert load_manifest_artifact_index(project_dir=tmp_path) == {
        "editor.dubbed_audio_complete": "output/final_mix.wav",
    }


def test_resolve_manifest_artifact_path_supports_relative_paths(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / "dubbed_audio_complete.wav"
    artifact_path.write_bytes(b"audio")

    resolved = resolve_manifest_artifact_path(
        tmp_path,
        "editor.dubbed_audio_complete",
        artifact_index={
            "editor.dubbed_audio_complete": "output/dubbed_audio_complete.wav",
        },
    )

    assert resolved == artifact_path.resolve(strict=False)
