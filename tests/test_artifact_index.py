from pathlib import Path

import pytest

from core.artifact_index import ArtifactIndex


def test_artifact_index_register_get_require_and_to_dict() -> None:
    index = ArtifactIndex()

    registered_path = index.register("audio.dubbed_full", Path("output") / "dubbed.wav")

    assert registered_path == str(Path("output") / "dubbed.wav")
    assert index.get("audio.dubbed_full") == str(Path("output") / "dubbed.wav")
    assert index.require("audio.dubbed_full") == str(Path("output") / "dubbed.wav")
    assert index.to_dict() == {"audio.dubbed_full": str(Path("output") / "dubbed.wav")}


def test_artifact_index_require_raises_for_missing_key() -> None:
    index = ArtifactIndex()

    with pytest.raises(KeyError, match="Artifact not found: source.original_audio"):
        index.require("source.original_audio")


def test_artifact_index_rejects_blank_key_and_path() -> None:
    index = ArtifactIndex()

    with pytest.raises(ValueError, match="Artifact key is required"):
        index.register("   ", "x.wav")

    with pytest.raises(ValueError, match="Artifact path is required"):
        index.register("audio.dubbed_full", "   ")
