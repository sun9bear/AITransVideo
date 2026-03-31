from services.assemblyai.transcriber import TranscriptLine
import services.transcript_reviewer as transcript_reviewer


def _line(
    index: int,
    start_ms: int,
    end_ms: int,
    speaker_id: str,
    text: str,
) -> TranscriptLine:
    return TranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_label=speaker_id.replace("speaker_", "").upper(),
        source_text=text,
    )


def _interview_speakers() -> dict[str, dict[str, str]]:
    return {
        "speaker_a": {
            "name": "Host",
            "role": "host",
            "style": "professional interviewer",
            "voice_description": "clear interviewer voice",
        },
        "speaker_b": {
            "name": "Guest",
            "role": "guest",
            "style": "serious guest",
            "voice_description": "low thoughtful voice",
        },
    }


def test_short_backchannel_is_reassigned_to_host() -> None:
    lines = [
        _line(1, 0, 1_200, "speaker_a", "What was your worst trade?"),
        _line(2, 1_200, 1_700, "speaker_b", "Yes."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 1
    assert adjusted[1].speaker_id == "speaker_a"


def test_answer_continuation_requires_actual_continuation_signal() -> None:
    lines = [
        _line(1, 0, 4_500, "speaker_b", "I think people learn more from their mistakes."),
        _line(2, 4_500, 7_800, "speaker_b", "That sounds strange."),
    ]

    assert transcript_reviewer._is_answer_continuation(  # noqa: SLF001
        lines=lines,
        position=1,
        host_speaker="speaker_a",
        guest_speaker="speaker_b",
    ) is False


def test_named_utterance_stays_conservative() -> None:
    lines = [
        _line(1, 0, 1_000, "speaker_a", "What happened next?"),
        _line(2, 1_000, 1_900, "speaker_b", "Thanks, Ron."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 0
    assert adjusted[1].speaker_id == "speaker_b"


def test_long_ambiguous_sentence_keeps_original_speaker() -> None:
    lines = [
        _line(1, 0, 4_500, "speaker_a", "What was that like for you?"),
        _line(2, 4_500, 7_600, "speaker_b", "Thank you, Charlotte. I let her do that."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 0
    assert adjusted[1].speaker_id == "speaker_b"
