"""Dispatch 决策测试（plan §分发决策字段）。

覆盖：
- requires_worker 显式 True/False 永远 wins
- requires_worker 缺字段时从 region_constraint 派生
- 同时支持 dict / dataclass / 任意 object
- voice=None 返 False
"""
from __future__ import annotations

from dataclasses import dataclass

from services.mainland_worker.dispatch import (
    derive_requires_worker,
    should_use_worker,
)
from services.mainland_worker.types import (
    REGION_CONSTRAINT_MAINLAND_ONLY,
    REGION_CONSTRAINT_OVERSEAS_OK,
)


@dataclass
class FakeVoice:
    """Stand-in for voice library row（dataclass 形态）。"""
    voice_id: str
    requires_worker: bool | None = None
    region_constraint: str | None = None


# ---------------------------------------------------------------------------
# Truth table
# ---------------------------------------------------------------------------

def test_explicit_requires_worker_true_wins() -> None:
    # 即使 region_constraint=overseas_ok，显式 True 仍然走 worker
    voice = FakeVoice(
        voice_id="v1",
        requires_worker=True,
        region_constraint=REGION_CONSTRAINT_OVERSEAS_OK,
    )
    assert should_use_worker(voice) is True


def test_explicit_requires_worker_false_wins() -> None:
    # 即使 region_constraint=mainland_only，显式 False 也不走 worker
    voice = FakeVoice(
        voice_id="v1",
        requires_worker=False,
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
    )
    assert should_use_worker(voice) is False


def test_derive_from_region_constraint_mainland_only() -> None:
    voice = FakeVoice(
        voice_id="v1",
        requires_worker=None,
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
    )
    assert should_use_worker(voice) is True


def test_derive_from_region_constraint_overseas_ok() -> None:
    voice = FakeVoice(
        voice_id="v1",
        requires_worker=None,
        region_constraint=REGION_CONSTRAINT_OVERSEAS_OK,
    )
    assert should_use_worker(voice) is False


def test_no_fields_returns_false() -> None:
    voice = FakeVoice(voice_id="v1")
    assert should_use_worker(voice) is False


def test_none_voice_returns_false() -> None:
    assert should_use_worker(None) is False


# ---------------------------------------------------------------------------
# Container 兼容
# ---------------------------------------------------------------------------

def test_works_with_dict() -> None:
    voice = {
        "voice_id": "v1",
        "requires_worker": True,
        "region_constraint": REGION_CONSTRAINT_MAINLAND_ONLY,
    }
    assert should_use_worker(voice) is True


def test_dict_missing_requires_worker_derives_from_region() -> None:
    voice = {
        "voice_id": "v1",
        "region_constraint": REGION_CONSTRAINT_MAINLAND_ONLY,
    }
    assert should_use_worker(voice) is True


def test_works_with_simplenamespace() -> None:
    from types import SimpleNamespace
    voice = SimpleNamespace(
        voice_id="v1",
        requires_worker=True,
        region_constraint=None,
    )
    assert should_use_worker(voice) is True


# ---------------------------------------------------------------------------
# derive_requires_worker
# ---------------------------------------------------------------------------

def test_derive_requires_worker_true_for_mainland_only() -> None:
    assert derive_requires_worker(REGION_CONSTRAINT_MAINLAND_ONLY) is True


def test_derive_requires_worker_false_for_overseas_ok() -> None:
    assert derive_requires_worker(REGION_CONSTRAINT_OVERSEAS_OK) is False


def test_derive_requires_worker_false_for_none() -> None:
    assert derive_requires_worker(None) is False


def test_derive_requires_worker_false_for_unknown_string() -> None:
    assert derive_requires_worker("future_region") is False


# ---------------------------------------------------------------------------
# Regression: 不能只看 provider == "cosyvoice_voice_clone"（plan §警告）
# ---------------------------------------------------------------------------

def test_dispatch_does_not_use_provider_name_as_signal() -> None:
    """plan §分发决策字段 警告：未来可能有非克隆但 mainland-only 的音色。

    所以只看 provider 是危险的。这里反向验证：哪怕 provider 不是
    cosyvoice_voice_clone，requires_worker=True 仍然走 worker。
    """
    voice = {
        "voice_id": "v1",
        "provider": "doubao_icl_voice_clone",  # 假想的未来 provider
        "requires_worker": True,
    }
    assert should_use_worker(voice) is True
