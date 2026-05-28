"""Phase 4.3a §3.1 — Express service mode consent validator.

Phase 4.3a 引入 Express 快捷版自动 CosyVoice clone canary。与 Smart MiniMax
自动 clone（``gateway/smart_consent.py``）相比，Express 的 consent 校验是
**soft skip** 语义：

- consent 缺失 / 非 dict → 视为未勾选（任务继续，跳过自动 clone）；**NOT** hard fail
- consent 格式错误（auto_voice_clone 非 bool 等）→ 同样 soft skip + reason
  落 audit JSONL；**NOT** hard fail
- consent 完整 + ``auto_voice_clone=True`` → 解锁自动 clone 路径

这与 ``validate_smart_consent`` 的 hard-fail 语义**故意不同**——Smart 是付费
入口，consent 完整性是合法计费前提；Express 自动 clone 是 Phase 4.3a canary
功能，consent 失败只是不触发可选 clone，任务用 CosyVoice 预设音色照常完成。
合理性见 spec §3.1。

字段（与 spec v0.3 §3 / §3.1.a 一致）：

- ``auto_voice_clone: bool`` 必填——用户显式勾选 = True；未勾选 / 未声明 = False
- ``client_confirmed_at: str | None`` 可选——前端勾选时刻 ISO 8601 UTC；
  **仅作辅助审计**，**不可信**（恶意客户端可伪造）

**``server_confirmed_at`` 不在本 validator 范围内**——那是 caller
（``gateway/job_intercept.py`` 在 ``auto_voice_clone=True`` 时用
``datetime.now(timezone.utc).isoformat()`` 在 validator 返回 dict 之上
追加的字段；本 validator 永远不会读 / 写它。spec v0.3 §3.1.a 明确：
``server_confirmed_at`` 由后端单一来源生成，**不**信任前端任何相关字段。

**与 ``gateway/smart_consent.py`` 边界**：

- 本模块**不**复用 ``SmartConsent`` dataclass / ``validate_smart_consent``
- Smart consent 6 字段 vs Express consent 2 字段，schema 完全不同
- ``services/express/``（pipeline 侧）只 import 本模块，绝不 import smart_consent
- 守卫测试 ``tests/test_phase43a_unchanged_smart_minimax_path.py`` 锁住
  Smart consent validator 签名字节级不变
"""
from __future__ import annotations

from typing import Any


def validate_express_consent(
    raw: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate raw express_consent payload (soft skip on any error).

    Args:
        raw: The ``express_consent`` field from job submission body
            (or None if not present).

    Returns:
        ``(parsed_dict, None)`` on success — ``parsed_dict`` contains:

            - ``auto_voice_clone: bool`` — True iff user explicitly opted in
            - ``client_confirmed_at: str | None`` — normalized client
              timestamp (stripped; empty string treated as None)

        Caller adds ``server_confirmed_at`` to the returned dict when
        ``auto_voice_clone is True`` (server-side authoritative timestamp,
        spec v0.3 §3.1.a).

        ``(None, reason)`` on parse failure — caller should soft-skip clone
        (treat as not-given) and write ``reason`` to audit JSONL so排障
        can distinguish "user did not opt in" from "client bug sent
        malformed payload".

    Failure reasons (each implies clone is skipped + audit emitted with
    distinct ``reason_code``):

    - ``express_consent_missing_or_invalid_type``: not a dict
    - ``auto_voice_clone_not_bool``: present but wrong type
    - ``client_confirmed_at_not_string``: present but wrong type
    """
    if not isinstance(raw, dict):
        return None, "express_consent_missing_or_invalid_type"

    # auto_voice_clone: absent field = explicit False (user did not opt in;
    # NOT an error condition — just means "no auto clone wanted").
    if "auto_voice_clone" in raw:
        auto_voice_clone = raw["auto_voice_clone"]
        if not isinstance(auto_voice_clone, bool):
            # Reject int (0/1) and str ("true"/"false") so accidental
            # coercion doesn't slip through. Matches validate_smart_consent
            # strict-bool style (gateway/smart_consent.py:115-120).
            return None, "auto_voice_clone_not_bool"
    else:
        auto_voice_clone = False

    # client_confirmed_at: optional, frontend-supplied. Untrusted (could
    # be forged by a malicious client). Stored as audit assist only;
    # never used in worker request / DashScope correlation.
    client_confirmed_at: str | None = None
    if "client_confirmed_at" in raw and raw["client_confirmed_at"] is not None:
        if not isinstance(raw["client_confirmed_at"], str):
            return None, "client_confirmed_at_not_string"
        normalized = raw["client_confirmed_at"].strip()
        if normalized:
            client_confirmed_at = normalized

    return {
        "auto_voice_clone": auto_voice_clone,
        "client_confirmed_at": client_confirmed_at,
    }, None


__all__ = ["validate_express_consent"]
