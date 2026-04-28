from __future__ import annotations

from services.tts import minimax_voice_selector


def test_minimax_no_gender_fallback_includes_backup_voices(monkeypatch) -> None:
    monkeypatch.setattr(
        minimax_voice_selector,
        "_load_minimax_pool",
        lambda: [
            {"voice_id": "Wise_Woman", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_1", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_2", "language": "中文-普通话", "gender": "male"},
            {"voice_id": "voice_3", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_4", "language": "中文-普通话", "gender": "male"},
            {"voice_id": "voice_5", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_6", "language": "中文-普通话", "gender": "female"},
        ],
    )

    result = minimax_voice_selector.select_minimax_voice_match(gender=None)

    assert result.voice_id == "Wise_Woman"
    assert result.backup_voices == ("voice_1", "voice_2", "voice_3", "voice_4", "voice_5")


def test_minimax_no_candidates_fallback_includes_backup_voices(monkeypatch) -> None:
    monkeypatch.setattr(
        minimax_voice_selector,
        "_load_minimax_pool",
        lambda: [
            {"voice_id": "Wise_Woman", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_1", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_2", "language": "中文-普通话", "gender": "female"},
        ],
    )

    result = minimax_voice_selector.select_minimax_voice_match(gender="alien")

    assert result.voice_id == "Wise_Woman"
    assert result.backup_voices == ("voice_1", "voice_2")


def test_minimax_unknown_gender_fallback_includes_backup_voices(monkeypatch) -> None:
    monkeypatch.setattr(
        minimax_voice_selector,
        "_load_minimax_pool",
        lambda: [
            {"voice_id": "Wise_Woman", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_1", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_2", "language": "中文-普通话", "gender": "male"},
            {"voice_id": "voice_3", "language": "中文-普通话", "gender": "female"},
            {"voice_id": "voice_4", "language": "中文-普通话", "gender": "male"},
            {"voice_id": "voice_5", "language": "中文-普通话", "gender": "female"},
        ],
    )

    result = minimax_voice_selector.select_minimax_voice_match(gender="unknown")

    assert result.voice_id == "Wise_Woman"
    assert result.backup_voices == ("voice_1", "voice_2", "voice_3", "voice_4", "voice_5")
