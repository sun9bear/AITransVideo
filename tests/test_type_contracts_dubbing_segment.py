"""契约测试：DubbingSegment 字段直接访问 + TypedDict 形状不变量（TU-07）。

固化"DubbingSegment 声明字段恒存在"这一不变量——getattr(segment, "X", default)
的 default 在 slots dataclass 上是死代码（字段恒被构造期填默认值），因此可安全改为
segment.X 直接访问。本测试在改任何调用代码之前先把契约钉死，防止后续回归。
"""

from __future__ import annotations

import dataclasses
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# --- stub database for gateway import（与 test_gateway_job_policy 一致）---
_gw = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gw not in sys.path:
    sys.path.insert(0, _gw)
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

from job_intercept import compute_job_policy  # noqa: E402

from src.services.gemini.translator import DubbingSegment  # noqa: E402
from src.services.tts.tts_generator import _read_job_field  # noqa: E402

# DubbingSegment 的必填字段（无默认值），用于构造最小合法实例。
_REQUIRED_FIELDS = (
    "segment_id",
    "speaker_id",
    "display_name",
    "voice_id",
    "start_ms",
    "end_ms",
    "target_duration_ms",
    "source_text",
    "cn_text",
)


def _minimal_segment(**overrides) -> DubbingSegment:
    """最小合法 DubbingSegment（仅必填字段，其余走声明默认值）。"""
    defaults = dict(
        segment_id=1,
        speaker_id="A",
        display_name="Speaker A",
        voice_id="",
        start_ms=0,
        end_ms=1000,
        target_duration_ms=1000,
        source_text="hello",
        cn_text="你好",
    )
    defaults.update(overrides)
    return DubbingSegment(**defaults)


def _make_user(role="user", plan_code="free"):
    return SimpleNamespace(
        id="u1",
        email="t@t.com",
        display_name="T",
        role=role,
        plan_code=plan_code,
        free_jobs_quota_total=5,
        free_jobs_quota_used=0,
    )


# ── 1. DubbingSegment slots 契约 ──────────────────────────────────────


class TestDubbingSegmentSlots:
    def test_slots_rejects_unknown_attribute(self):
        """slots=True: 设置未声明字段必须 AttributeError（不能静默存储）。"""
        seg = _minimal_segment()
        with pytest.raises(AttributeError):
            seg.en_text = "should fail"  # en_text 不是 DubbingSegment 字段

    def test_known_fields_directly_accessible(self):
        """本单元 getattr→直接访问涉及的所有字段，均可直接访问且恒存在。"""
        seg = _minimal_segment()
        for attr in (
            "voice_id",
            "gender",
            "age_group",
            "persona_style",
            "energy_level",
            "voice_description",
            "dubbing_mode",
            "requires_worker",
            "worker_target_model",
            "tts_provider",
            "tts_model_key",
            "selected_voice",
            "match_confidence",
            "target_chars_per_second",
            "target_duration_ms",
            "tts_input_cn_text",
            "first_pass_cn_text",
            "tts_audio_path",
            "pre_tts_contradiction",
            "pre_tts_rewrite_direction",
            "voiceclone_reference_path",
            "target_language",
        ):
            # getattr 在此是测试工具（无 default），非被测代码；字段缺失会 AttributeError。
            getattr(seg, attr)

    def test_target_language_is_declared_field(self):
        """target_language 现为 DubbingSegment 声明字段（i18n PR-A 已合入）——
        这正是 TU-07 把 getattr(segment, "target_language", None) 改为
        segment.target_language（字节等价）的依据。"""
        names = {f.name for f in dataclasses.fields(DubbingSegment)}
        assert "target_language" in names

    def test_required_fields_set_is_authoritative(self):
        """_minimal_segment 覆盖且仅覆盖 DubbingSegment 的必填字段（无默认值者）。
        新增必填字段会让此测试红 → 提示同步更新 _minimal_segment。"""
        no_default = {
            f.name
            for f in dataclasses.fields(DubbingSegment)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        assert no_default == set(_REQUIRED_FIELDS)

    def test_dataclass_fields_match_expected_set(self):
        """确认 DubbingSegment 字段集包含本单元清理涉及的所有字段。"""
        field_names = {f.name for f in dataclasses.fields(DubbingSegment)}
        required = {
            "voice_id",
            "gender",
            "age_group",
            "persona_style",
            "energy_level",
            "voice_description",
            "dubbing_mode",
            "requires_worker",
            "worker_target_model",
            "tts_provider",
            "tts_model_key",
            "selected_voice",
            "match_confidence",
            "target_chars_per_second",
            "target_duration_ms",
            "tts_input_cn_text",
            "first_pass_cn_text",
            "tts_audio_path",
            "pre_tts_contradiction",
            "pre_tts_rewrite_direction",
            "voiceclone_reference_path",
            "target_language",
        }
        missing = required - field_names
        assert not missing, f"DubbingSegment 缺字段（回归红线）: {missing}"


# ── 2. _read_job_field 双路契约 ───────────────────────────────────────


class TestReadJobField:
    def test_dict_path(self):
        assert _read_job_field({"tts_model": "speech-2.8-hd"}, "tts_model") == "speech-2.8-hd"

    def test_dict_missing_key_returns_none(self):
        assert _read_job_field({}, "tts_model") is None

    def test_object_path(self):
        rec = SimpleNamespace(tts_model="speech-2.8-turbo")
        assert _read_job_field(rec, "tts_model") == "speech-2.8-turbo"

    def test_object_missing_attr_returns_none(self):
        assert _read_job_field(SimpleNamespace(), "tts_model") is None

    def test_none_record_returns_none(self):
        assert _read_job_field(None, "tts_model") is None


# ── 3. compute_job_policy 键集 + 类型形状契约 ─────────────────────────

_EXPECTED_POLICY_KEYS = frozenset(
    {
        "service_mode",
        "tts_provider",
        "tts_model",
        "requires_review",
        "voice_clone_enabled",
        "voice_strategy",
        "plan_code_snapshot",
        "role_snapshot",
        "quality_tier",
    }
)


class TestJobPolicyShape:
    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_all_modes_return_expected_keys(self, mode):
        p = compute_job_policy(_make_user(), mode)
        missing = _EXPECTED_POLICY_KEYS - set(p.keys())
        extra = set(p.keys()) - _EXPECTED_POLICY_KEYS
        assert not missing, f"mode={mode} 缺键: {missing}"
        assert not extra, f"mode={mode} 多键（TypedDict total 应保持 flat）: {extra}"

    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_bool_fields_are_bool(self, mode):
        p = compute_job_policy(_make_user(), mode)
        assert isinstance(p["requires_review"], bool), f"mode={mode}"
        assert isinstance(p["voice_clone_enabled"], bool), f"mode={mode}"

    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_str_fields_are_str(self, mode):
        p = compute_job_policy(_make_user(), mode)
        for key in (
            "service_mode",
            "tts_provider",
            "voice_strategy",
            "plan_code_snapshot",
            "role_snapshot",
            "quality_tier",
        ):
            assert isinstance(p[key], str), f"mode={mode} key={key}"

    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_tts_model_is_str_or_none(self, mode):
        """tts_model 形状契约：str | None（本测试断言形状，两者皆接受）。

        None 值仅在 studio + volcengine（豆包 2.0 公共音色）时产生；该具体值分支
        由 tests/test_gateway_job_policy.py::test_studio_volcengine_model_none
        （monkeypatch volcengine 后断言 is None）确定性覆盖。默认 admin 配置下
        本参数化不会构造 None，故此处只锁形状不锁该值。
        """
        p = compute_job_policy(_make_user(), mode)
        assert p["tts_model"] is None or isinstance(p["tts_model"], str), f"mode={mode}"


# ── 4. 源码守卫：清理过的两个模块不得回归 getattr(segment) ────────────


class TestNoGetattrSegmentRegression:
    """TU-07 永久守卫：被清理的模块不得重新引入 getattr(segment, …)。

    若未来有人删掉某 DubbingSegment 字段并用 getattr(seg, "x", default) 兜底，
    会再次架空 slots 的类型安全（TS-02 原始问题）。本守卫在源码层挡住回归——
    与 test_legacy_cleanup_guards 同思路（契约级、source-grep）。
    """

    @pytest.mark.parametrize(
        "relpath",
        [
            "src/services/tts/tts_generator.py",
            "src/services/alignment/aligner.py",
        ],
    )
    def test_no_getattr_segment(self, relpath):
        root = __import__("pathlib").Path(__file__).resolve().parent.parent
        src = (root / relpath).read_text(encoding="utf-8")
        hits = [f"L{i}: {ln.strip()}" for i, ln in enumerate(src.splitlines(), 1) if "getattr(segment" in ln]
        assert not hits, f"{relpath} 重新引入 getattr(segment)（TU-07 回归）: {hits[:5]}"
