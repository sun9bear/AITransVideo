from pathlib import Path

import pytest

from core.exceptions import IngestionError
from modules.ingestion.models import SubtitleSeed
from modules.ingestion.normalizer import SubtitleNormalizer
from modules.ingestion.srt_loader import SRTSubtitleLoader


def test_srt_loader_normalizes_local_srt(tmp_path: Path) -> None:
    srt_path = tmp_path / "sample.srt"
    srt_path.write_text(
        "1\n"
        "00:00:00,000 --> 00:00:01,200\n"
        "Hello there.\n\n"
        "2\n"
        "00:00:01,300 --> 00:00:02,000\n"
        "General Kenobi.\n",
        encoding="utf-8",
    )
    loader = SRTSubtitleLoader()

    lines = loader.load(str(srt_path), default_speaker_id="speaker_narrator", default_speaker_name="Narrator")

    assert len(lines) == 2
    assert lines[0].index == 1
    assert lines[0].start_ms == 0
    assert lines[0].end_ms == 1_200
    assert lines[0].speaker_id == "speaker_narrator"
    assert lines[0].speaker_name == "Narrator"
    assert lines[0].en_text == "Hello there."
    assert lines[0].cn_text == ""


def test_normalizer_rejects_missing_speaker_id_even_if_name_exists() -> None:
    normalizer = SubtitleNormalizer()

    with pytest.raises(IngestionError, match="speaker_id is required"):
        normalizer.normalize(
            [
                SubtitleSeed(
                    index=1,
                    start_ms=0,
                    end_ms=1_000,
                    en_text="Hello",
                    speaker_id="",
                    speaker_name="Narrator",
                )
            ]
        )


def test_normalizer_preserves_speaker_id_and_name_separately() -> None:
    normalizer = SubtitleNormalizer()

    lines = normalizer.normalize(
        [
            SubtitleSeed(0, 800, "Line A", "speaker_a", "Host", index=1),
            SubtitleSeed(900, 1_600, "Line B", "speaker_b", "Host", index=2),
        ]
    )

    assert lines[0].speaker_id == "speaker_a"
    assert lines[1].speaker_id == "speaker_b"
    assert lines[0].speaker_name == lines[1].speaker_name == "Host"
