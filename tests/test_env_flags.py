from __future__ import annotations

from utils.env_flags import env_flag, env_int


def test_env_flag_defaults_to_false_for_missing_or_invalid(monkeypatch) -> None:
    monkeypatch.delenv("AVT_TEST_FLAG", raising=False)
    assert env_flag("AVT_TEST_FLAG") is False

    monkeypatch.setenv("AVT_TEST_FLAG", "maybe")
    assert env_flag("AVT_TEST_FLAG") is False
    assert env_flag("AVT_TEST_FLAG", default=True) is True


def test_env_int_uses_default_for_missing_invalid_or_out_of_range(monkeypatch) -> None:
    monkeypatch.delenv("AVT_TEST_INT", raising=False)
    assert env_int("AVT_TEST_INT", default=32, min_value=1, max_value=200) == 32

    monkeypatch.setenv("AVT_TEST_INT", "abc")
    assert env_int("AVT_TEST_INT", default=32, min_value=1, max_value=200) == 32

    monkeypatch.setenv("AVT_TEST_INT", "0")
    assert env_int("AVT_TEST_INT", default=32, min_value=1, max_value=200) == 32

    monkeypatch.setenv("AVT_TEST_INT", "201")
    assert env_int("AVT_TEST_INT", default=32, min_value=1, max_value=200) == 32

    monkeypatch.setenv("AVT_TEST_INT", "48")
    assert env_int("AVT_TEST_INT", default=32, min_value=1, max_value=200) == 48
