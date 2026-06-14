"""P3e §5 limit-1 — evaluate_voice_review clone_allowed_speaker_ids cap 行为测试.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3 §5。reservation 600 只覆盖
1 个克隆 → caller 传 clone_allowed_speaker_ids 限定"本任务只允许为这些 speaker
新建克隆"，不在白名单内的 no-match speaker 退 PRESET（不克隆=不漏收、不 handoff）。

**钱-critical 回归点（R1 understand 暴露）**：被 cap 的 speaker 即便 sample_seconds
≥ 阈值（fallback 到 vs_payload 全时长常 ≥10s）通过 Rule 2，也**绝不**能克隆——
所以 cap 必须是**显式** per-speaker 信号，不能靠"截断样本→Rule 2 失败"实现。
本测试用 sample_seconds=20（远超阈值）的 capped speaker 钉死这点。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


def _speaker(sid, *, sample_seconds=20.0):
    from services.smart.auto_voice_review import VoiceReviewSpeakerInput
    return VoiceReviewSpeakerInput(
        speaker_id=sid,
        speaker_name=sid,
        sample_seconds=sample_seconds,
        source_audio_path=Path(f"/fake/audio/{sid}.wav"),
    )


def _id_factory(prefix="dec"):
    i = [0]
    def factory():
        i[0] += 1
        return f"{prefix}_{i[0]:03d}"
    return factory


def test_cap_routes_non_allowed_speaker_to_preset_not_clone():
    """🔥 钱-核心：clone_allowed={a} → speaker a 克隆、speaker b（sample 20s，
    本会通过 Rule 2）被 cap 退 PRESET（reason clone_capped_by_reservation_limit），
    **绝不调 provider**。fake.calls 只含 a → 一 reservation 只 1 次付费克隆。"""
    from services.smart.auto_voice_review import (
        evaluate_voice_review, VoiceReviewChoice, VoiceReviewOutcome,
    )
    from tests.fakes import FakeCloneProvider

    fake = FakeCloneProvider()
    result = evaluate_voice_review(
        main_speakers=[_speaker("a", sample_seconds=20.0), _speaker("b", sample_seconds=20.0)],
        smart_consent={"auto_voice_clone": True},
        clone_provider=fake,
        voice_library_quota_remaining=100,
        smart_decision_id_factory=_id_factory(),
        admin_clone_enabled=True,
        clone_allowed_speaker_ids={"a"},
    )
    assert result.outcome is VoiceReviewOutcome.AUTO_APPROVED
    by_sid = {d.speaker_id: d for d in result.decisions}
    assert by_sid["a"].choice is VoiceReviewChoice.CLONED
    assert by_sid["b"].choice is VoiceReviewChoice.PRESET
    assert by_sid["b"].reason_code == "clone_capped_by_reservation_limit"
    # provider 只为 a 调用一次（b 被 cap，零付费克隆）
    cloned_sids = [c.get("speaker_id") if isinstance(c, dict) else getattr(c, "speaker_id", None) for c in fake.calls]
    assert "b" not in cloned_sids
    assert len([c for c in fake.calls]) == 1


def test_cap_none_allows_all_speakers_to_clone():
    """🔥 回归：clone_allowed=None（默认，flag off / 无 reservation）→ 不限制，
    两 speaker 都克隆（既有多说话人行为字节级不变）。"""
    from services.smart.auto_voice_review import (
        evaluate_voice_review, VoiceReviewChoice,
    )
    from tests.fakes import FakeCloneProvider

    fake = FakeCloneProvider()
    result = evaluate_voice_review(
        main_speakers=[_speaker("a"), _speaker("b")],
        smart_consent={"auto_voice_clone": True},
        clone_provider=fake,
        voice_library_quota_remaining=100,
        smart_decision_id_factory=_id_factory(),
        admin_clone_enabled=True,
        clone_allowed_speaker_ids=None,
    )
    assert all(d.choice is VoiceReviewChoice.CLONED for d in result.decisions)
    assert len(fake.calls) == 2


def test_cap_does_not_dethrone_strong_reuse():
    """cap 只限"新克隆"，不夺已有强匹配复用：speaker a 有强匹配 → REUSED
    （即便 a 不在 clone_allowed）；speaker b 无匹配且不在白名单 → PRESET capped。"""
    from services.smart.auto_voice_review import (
        evaluate_voice_review, VoiceReviewChoice, VoiceReviewExistingMatch,
    )
    from tests.fakes import FakeCloneProvider

    fake = FakeCloneProvider()
    result = evaluate_voice_review(
        main_speakers=[_speaker("a"), _speaker("b")],
        smart_consent={"auto_voice_clone": True},
        clone_provider=fake,
        voice_library_quota_remaining=100,
        smart_decision_id_factory=_id_factory(),
        existing_voice_matches_by_speaker_id={
            "a": VoiceReviewExistingMatch(voice_id="reuse_a"),
        },
        admin_clone_enabled=True,
        clone_allowed_speaker_ids=set(),  # 空集 → 没人能新克隆
    )
    by_sid = {d.speaker_id: d for d in result.decisions}
    assert by_sid["a"].choice is VoiceReviewChoice.REUSED  # 复用不受 cap 影响
    assert by_sid["b"].choice is VoiceReviewChoice.PRESET
    assert by_sid["b"].reason_code == "clone_capped_by_reservation_limit"
    assert fake.calls == []  # 零付费克隆


def test_cap_empty_set_blocks_all_new_clone():
    """clone_allowed=set()（空）→ 所有 no-match speaker 全 PRESET capped，
    零 provider 调用（防"空集被当 None"误放行）。"""
    from services.smart.auto_voice_review import (
        evaluate_voice_review, VoiceReviewChoice,
    )
    from tests.fakes import FakeCloneProvider

    fake = FakeCloneProvider()
    result = evaluate_voice_review(
        main_speakers=[_speaker("a"), _speaker("b")],
        smart_consent={"auto_voice_clone": True},
        clone_provider=fake,
        voice_library_quota_remaining=100,
        smart_decision_id_factory=_id_factory(),
        admin_clone_enabled=True,
        clone_allowed_speaker_ids=set(),
    )
    assert all(d.choice is VoiceReviewChoice.PRESET for d in result.decisions)
    assert all(d.reason_code == "clone_capped_by_reservation_limit" for d in result.decisions)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# source-scan：process.py limit-1 接线契约
# ---------------------------------------------------------------------------

_PROCESS_PY = _SRC / "pipeline" / "process.py"


def _proc_src() -> str:
    return _PROCESS_PY.read_text(encoding="utf-8")


def test_process_computes_clone_allowed_only_when_reservation_present():
    """limit-1 只在 reservation 收紧（requires_reservation AND reservation_id）
    时激活；默认 _smart_clone_allowed_speaker_ids=None（不限制，既有行为）。"""
    src = _proc_src()
    assert "_smart_clone_allowed_speaker_ids: set[str] | None = None" in src
    flat = " ".join(src.split())
    assert "if _smart_requires_reservation and _smart_clone_reservation_id:" in flat


def test_process_truncates_requiring_clone_stable_order():
    """limit-1 截断 requiring_clone 到 [:1]，用 vs_payload 稳定顺序（非 set 迭代序）。"""
    src = _proc_src()
    assert "_smart_ordered_requiring[:1]" in src
    flat = " ".join(src.split())
    assert 'vs_payload.get("speakers")' in flat


def test_process_passes_clone_allowed_to_evaluate():
    """evaluate_voice_review 调用必须传 clone_allowed_speaker_ids。"""
    src = _proc_src()
    assert "clone_allowed_speaker_ids=_smart_clone_allowed_speaker_ids" in src
