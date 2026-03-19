import pytest

from core.exceptions import IngestionError
from modules.ingestion.intake import AuthoritativeIntakeBuilder, AuthoritativeIntakeRequest
from modules.media_understanding.models import (
    AttributedTranscriptLine,
    MediaSourceKind,
    TranscriptLine,
)


def test_authoritative_intake_builder_normalizes_transcript_request() -> None:
    builder = AuthoritativeIntakeBuilder()

    media_source = builder.build(
        AuthoritativeIntakeRequest(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Hello world.")],
            metadata={"language_hint": "en"},
        )
    )

    assert media_source.kind == MediaSourceKind.TRANSCRIPT
    assert media_source.locator is None
    assert [line.source_text for line in media_source.transcript_lines] == ["Hello world."]
    assert media_source.attributed_lines == []
    assert media_source.metadata == {"language_hint": "en"}


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        (MediaSourceKind.LOCAL_SRT, "  D:/tmp/sample.srt  "),
        (MediaSourceKind.LOCAL_AUDIO, "  D:/tmp/sample.wav  "),
        (MediaSourceKind.LOCAL_VIDEO, "  D:/tmp/sample.mp4  "),
    ],
)
def test_authoritative_intake_builder_normalizes_local_file_requests(
    kind: MediaSourceKind,
    locator: str,
) -> None:
    builder = AuthoritativeIntakeBuilder()

    media_source = builder.build(
        AuthoritativeIntakeRequest(
            kind=kind,
            locator=locator,
            metadata={"source_label": "demo"},
        )
    )

    assert media_source.kind == kind
    assert media_source.locator == locator.strip()
    assert media_source.transcript_lines == []
    assert media_source.attributed_lines == []
    assert media_source.metadata == {"source_label": "demo"}


def test_authoritative_intake_builder_normalizes_attributed_transcript_request() -> None:
    builder = AuthoritativeIntakeBuilder()

    media_source = builder.build(
        AuthoritativeIntakeRequest(
            kind=MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
            attributed_lines=[
                AttributedTranscriptLine(
                    index=1,
                    start_ms=0,
                    end_ms=900,
                    speaker_id="speaker_host",
                    speaker_name="Host",
                    source_text="Welcome back.",
                )
            ],
        )
    )

    assert media_source.kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT
    assert media_source.locator is None
    assert media_source.transcript_lines == []
    assert [line.speaker_id for line in media_source.attributed_lines] == ["speaker_host"]


def test_authoritative_intake_builder_rejects_missing_payload_for_source_kind() -> None:
    builder = AuthoritativeIntakeBuilder()

    with pytest.raises(IngestionError, match="requires transcript_lines"):
        builder.build(AuthoritativeIntakeRequest(kind=MediaSourceKind.TRANSCRIPT))

    with pytest.raises(IngestionError, match="requires attributed_lines"):
        builder.build(AuthoritativeIntakeRequest(kind=MediaSourceKind.ATTRIBUTED_TRANSCRIPT))

    with pytest.raises(IngestionError, match="requires locator"):
        builder.build(AuthoritativeIntakeRequest(kind=MediaSourceKind.LOCAL_AUDIO))


def test_authoritative_intake_builder_rejects_non_authoritative_source_kind() -> None:
    builder = AuthoritativeIntakeBuilder()

    with pytest.raises(IngestionError, match="only supports authoritative source kinds"):
        builder.build(
            AuthoritativeIntakeRequest(
                kind=MediaSourceKind.YOUTUBE_URL,
                locator="https://youtube.com/watch?v=demo",
            )
        )
