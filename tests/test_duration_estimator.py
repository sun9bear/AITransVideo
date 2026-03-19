from services.tts.duration_estimator import TTSDurationEstimator


def test_duration_estimator_estimates_duration_from_character_count() -> None:
    estimator = TTSDurationEstimator(chars_per_second=4.5)

    estimated_ms = estimator.estimate_duration_ms("大家好这是一个测试")

    assert estimated_ms == 2000


def test_duration_estimator_calibrates_chars_per_second() -> None:
    estimator = TTSDurationEstimator(chars_per_second=4.5)

    chars_per_second = estimator.calibrate(
        [
            ("大家好", 1_000),
            ("测试", 500),
            ("欢迎", 500),
        ]
    )

    assert round(chars_per_second, 2) == 3.50
    assert estimator.chars_per_second == chars_per_second


def test_duration_estimator_returns_zero_for_empty_text() -> None:
    estimator = TTSDurationEstimator(chars_per_second=4.5)

    assert estimator.estimate_duration_ms("  ，。！？ ") == 0
