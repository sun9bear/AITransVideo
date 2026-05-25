"""Voice metadata → mainland worker 路由决策。

plan §分发决策字段 的运行时 fork 规则::

    if voice.requires_worker:
        use_mainland_worker_client()
    else:
        use_direct_provider()

本模块只做"该不该走 worker"的判断，不负责构造请求 / 调 client。让
TTS generator / segment_regenerate 这类调用方保持只依赖一个布尔决策。

约束（plan §分发决策字段）：

- ❌ 不要只看 ``provider == "cosyvoice_voice_clone"``。未来可能存在非
  克隆但 mainland-only 的音色（例如豆包 ICL 2.0 的国内限定预设音色），
  也可能存在其他 provider 的国内限定资源。
- ✅ 看 ``region_constraint`` / ``requires_worker`` 这两个显式语义字段。

兼容性：

- ``voice`` 既可以是 dataclass / dict / 任意有相应 attribute 的对象 —
  这里通过 duck-typed access 同时支持，方便在不同调用方（user voice
  library JSON dict、内存中的 dataclass、ORM row）平滑接入。
- 缺字段时按"保守默认"返回 False（不走 worker），这是因为现存音色都
  是直接 provider 路径，加 worker 是新能力，未标注的旧记录不应被误
  路由。
"""
from __future__ import annotations

import logging
from typing import Any

from services.mainland_worker.types import (
    REGION_CONSTRAINT_MAINLAND_ONLY,
)


logger = logging.getLogger(__name__)


def _get_attr_or_key(voice: Any, name: str, default: Any = None) -> Any:
    """Duck-typed access：先试 attribute，再试 dict key。"""
    if voice is None:
        return default
    # attribute
    val = getattr(voice, name, None)
    if val is not None:
        return val
    # dict
    if isinstance(voice, dict):
        return voice.get(name, default)
    return default


def should_use_worker(voice: Any) -> bool:
    """根据 voice metadata 判断是否走 mainland worker。

    决策表（plan §分发决策字段）：

    +-----------------------+-----------------------+-------+
    | region_constraint     | requires_worker       | 结果  |
    +=======================+=======================+=======+
    | "mainland_only"       | True / 缺字段         | True  |
    | "mainland_only"       | False（显式覆盖）     | False |
    | "overseas_ok" / 缺字段| True（显式覆盖）      | True  |
    | "overseas_ok" / 缺字段| False / 缺字段        | False |
    +-----------------------+-----------------------+-------+

    显式 ``requires_worker`` 永远优先；只有当它缺失时才从
    ``region_constraint == "mainland_only"`` 派生。

    Parameters
    ----------
    voice : Any
        Voice metadata。可以是 dataclass、dict、ORM row，只要支持
        ``getattr(v, 'requires_worker')`` 或 ``v['requires_worker']``
        其一即可。
    """
    if voice is None:
        return False

    # 显式 requires_worker（True / False）总是 wins
    explicit = _get_attr_or_key(voice, "requires_worker", default=None)
    if isinstance(explicit, bool):
        return explicit

    # 否则按 region_constraint 派生
    region = _get_attr_or_key(voice, "region_constraint", default=None)
    if region == REGION_CONSTRAINT_MAINLAND_ONLY:
        return True

    return False


def derive_requires_worker(region_constraint: str | None) -> bool:
    """``region_constraint`` → ``requires_worker`` 的标准派生规则。

    Phase 4 把 voice 写入 user voice library 时用这个函数算 ``requires_worker``，
    确保 metadata 落库前两个字段一致。
    """
    return region_constraint == REGION_CONSTRAINT_MAINLAND_ONLY
