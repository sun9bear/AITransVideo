"""Unit tests for TU-06 shared helpers.

Covers src/utils/coerce.py, src/utils/json_helpers.py, src/utils/error_payload.py
and the DRY-06 unpack_rerank_result() in services/tts/voice_reranker.py.

All tests are pure Python — no network, no paid providers, no external services
(tmp_path fixture is the only filesystem touch). See TU-06 invariant 2.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from utils.coerce import (
    coerce_bool,
    coerce_int,
    coerce_optional_int,
    normalize_optional_text,
)
from utils.error_payload import NON_RETRYABLE_CODES, RETRYABLE_CODES, ErrorPayload
from utils.json_helpers import to_jsonable, write_json


class TestNormalizeOptionalText:
    def test_none_returns_none(self) -> None:
        assert normalize_optional_text(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_optional_text("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_optional_text("   \t\n") is None

    def test_strips_and_returns(self) -> None:
        assert normalize_optional_text("  hello  ") == "hello"

    def test_non_string_coerced_via_str(self) -> None:
        assert normalize_optional_text(123) == "123"


class TestCoerceBool:
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_true_literals(self, value: str) -> None:
        assert coerce_bool(value, default=False) is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off"])
    def test_false_literals(self, value: str) -> None:
        assert coerce_bool(value, default=True) is False

    def test_bool_passthrough(self) -> None:
        assert coerce_bool(True, default=False) is True
        assert coerce_bool(False, default=True) is False

    def test_unknown_returns_default(self) -> None:
        assert coerce_bool("maybe", default=True) is True
        assert coerce_bool("maybe", default=False) is False

    def test_none_returns_default(self) -> None:
        assert coerce_bool(None, default=True) is True


class TestCoerceInt:
    def test_int_passthrough(self) -> None:
        assert coerce_int(42, default=0) == 42

    def test_str_int(self) -> None:
        assert coerce_int("42", default=0) == 42

    def test_float_truncated(self) -> None:
        # int(3.9) truncates toward zero
        assert coerce_int(3.9, default=0) == 3

    def test_invalid_returns_default(self) -> None:
        assert coerce_int("abc", default=7) == 7
        assert coerce_int("3.9", default=7) == 7  # str float is not a valid int


class TestCoerceOptionalInt:
    def test_none_returns_none(self) -> None:
        assert coerce_optional_int(None) is None

    def test_valid_returns_int(self) -> None:
        assert coerce_optional_int("5") == 5
        assert coerce_optional_int(5) == 5

    def test_invalid_returns_none(self) -> None:
        assert coerce_optional_int("abc") is None


@dataclass
class _SampleDC:
    a: int
    b: str


@dataclass
class _DCWithPath:
    p: Path


class _PlainObj:
    def __init__(self) -> None:
        self.public = "v"
        self._private = "hidden"


class TestToJsonable:
    @pytest.mark.parametrize("value", [None, "s", 1, 1.5, True, False])
    def test_primitives_passthrough(self, value: object) -> None:
        assert to_jsonable(value) == value

    def test_path_to_str(self) -> None:
        assert to_jsonable(Path("a") / "b") == str(Path("a") / "b")

    def test_nested_dict(self) -> None:
        assert to_jsonable({1: {"x": Path("p")}}) == {"1": {"x": str(Path("p"))}}

    def test_list_and_tuple(self) -> None:
        assert to_jsonable([1, Path("p")]) == [1, str(Path("p"))]
        assert to_jsonable((1, 2)) == [1, 2]

    def test_dataclass_via_asdict(self) -> None:
        assert to_jsonable(_SampleDC(a=1, b="x")) == {"a": 1, "b": "x"}

    def test_dataclass_with_nested_path_recurses(self) -> None:
        # Regression (TU-06 CodeX P2): a dataclass holding a Path must serialize
        # to a str, not leak a Path that json.dumps would reject.
        assert to_jsonable(_DCWithPath(p=Path("x") / "y")) == {"p": str(Path("x") / "y")}

    def test_object_with_dunder_dict(self) -> None:
        assert to_jsonable(_PlainObj()) == {"public": "v"}

    def test_private_attrs_excluded(self) -> None:
        assert "_private" not in to_jsonable(_PlainObj())


class TestWriteJson:
    def test_roundtrip_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        write_json(path, {"k": "中文", "n": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"k": "中文", "n": 1}

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c.json"
        write_json(path, {"ok": True})
        assert path.exists()

    def test_path_values_serialized_as_str(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        write_json(path, {"p": Path("x") / "y"})
        assert json.loads(path.read_text(encoding="utf-8"))["p"] == str(Path("x") / "y")


class TestErrorPayload:
    def test_to_dict_omits_empty_detail(self) -> None:
        # Mirrors the gateway wire convention: empty detail is not serialized
        # (frontend branches on `'detail' in payload`).
        d = ErrorPayload("job_not_found", "任务不存在").to_dict()
        assert set(d) == {"error_code", "message", "retryable", "user_action"}
        assert "detail" not in d

    def test_to_dict_includes_nonempty_detail(self) -> None:
        d = ErrorPayload("x", "y", detail={"k": "v"}).to_dict()
        assert d["detail"] == {"k": "v"}
        assert set(d) == {"error_code", "message", "retryable", "user_action", "detail"}

    def test_defaults_are_safe(self) -> None:
        p = ErrorPayload("x", "y")
        assert p.retryable is False
        assert p.detail == {}
        assert p.user_action == ""

    def test_default_detail_is_independent(self) -> None:
        # default_factory must not share a single mutable dict across instances
        assert ErrorPayload("a", "b").detail is not ErrorPayload("c", "d").detail

    def test_frozen_immutability(self) -> None:
        p = ErrorPayload("x", "y")
        with pytest.raises(FrozenInstanceError):
            p.error_code = "z"  # type: ignore[misc]

    def test_retryable_codes_disjoint_from_non_retryable(self) -> None:
        assert RETRYABLE_CODES.isdisjoint(NON_RETRYABLE_CODES)

    def test_uses_real_shipped_insufficient_credits_code(self) -> None:
        # @codex P2: must match the gateway-emitted code (frontend keys on it),
        # not the fictional ``credit_insufficient``.
        assert "insufficient_credits" in NON_RETRYABLE_CODES
        assert "credit_insufficient" not in NON_RETRYABLE_CODES


class TestUnpackRerankResult:
    def test_basic_extraction(self) -> None:
        from services.tts.voice_reranker import unpack_rerank_result

        scored = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
        best_vid, best_score, remaining, confidence = unpack_rerank_result(scored)
        assert best_vid == "a"
        assert best_score == 0.9
        assert remaining == ("b", "c")
        assert isinstance(confidence, str)

    def test_backup_limit_default_five(self) -> None:
        from services.tts.voice_reranker import unpack_rerank_result

        scored = [(f"v{i}", 1.0 - i * 0.1) for i in range(10)]
        _, _, remaining, _ = unpack_rerank_result(scored)
        assert remaining == ("v1", "v2", "v3", "v4", "v5")

    def test_backup_limit_custom(self) -> None:
        from services.tts.voice_reranker import unpack_rerank_result

        scored = [(f"v{i}", 1.0 - i * 0.1) for i in range(10)]
        _, _, remaining, _ = unpack_rerank_result(scored, backup_limit=2)
        assert remaining == ("v1", "v2")

    def test_short_list_truncates(self) -> None:
        from services.tts.voice_reranker import unpack_rerank_result

        best_vid, _, remaining, _ = unpack_rerank_result([("solo", 0.5)])
        assert best_vid == "solo"
        assert remaining == ()
