"""Express reservation HTTP client（spec §6.1 ``reservation_client``）。

调 PR2-C 的 gateway internal endpoints（reserve / consume / release）。

**边界（Codex PR2-E）**：走 HTTP（``urllib.request``，与 ``process.py`` 现有
internal 调用同款 stdlib，**不引入 requests 依赖**），**绝不** import gateway
service。env：``AVT_GATEWAY_URL``（默认 ``http://127.0.0.1:8880``）+
``AVT_INTERNAL_API_KEY`` → ``X-Internal-Key`` header。

返回 typed dataclass，**不**因 HTTP 4xx/5xx 抛异常（deny_reason / error 在
body 里）；仅网络层错误（连不上 / 超时）转 ``error='transport_error'``。
PR2-F 把这些函数装配进 ``auto_clone`` 的注入式 client。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_RESERVE_PATH = "/api/internal/express-auto-clone-reservations/reserve"
_CONSUME_PATH = "/api/internal/express-auto-clone-reservations/{rid}/consume"
_RELEASE_PATH = "/api/internal/express-auto-clone-reservations/{rid}/release"
_DEFAULT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ReserveResult:
    """reserve 结果。``ok`` 仅在 200 reserved 时为 True。"""

    ok: bool
    http_status: int
    reservation_id: str | None = None
    deny_reason: str | None = None  # daily_cap_exceeded / active_temp_cap_exceeded
    error: str | None = None        # user_not_found / admin_settings_unavailable / invalid_* / transport_error
    idempotent_hit: bool = False


@dataclass(frozen=True)
class TransitionResult:
    """consume / release 结果。"""

    ok: bool
    http_status: int
    status: str | None = None         # consumed / released / ...
    conflict_reason: str | None = None
    error: str | None = None          # transport_error / voice_id_required


def _gateway_base() -> str:
    return os.environ.get("AVT_GATEWAY_URL", "http://127.0.0.1:8880").rstrip("/")


def _safe_json(raw: str) -> dict:
    """解析 body 为 dict。空 / 非 JSON / 非 dict（array/string/number）→ ``{}``。

    **绝不抛 JSONDecodeError**（Codex E-fix item 3）：malformed 200 body 不能
    让裸异常穿出 client；上层凭 ``{}`` 判定 malformed_* 并走安全分支。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _post_json(path: str, payload: dict, *, timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[int, dict]:
    """POST JSON → (status, body_dict)。4xx/5xx 也返回 (status, body)，
    不 raise（body 里有 deny_reason / error）。malformed/空 body → ``{}``（不裸抛
    JSONDecodeError）。仅网络层错误抛 OSError/URLError，由 caller 转 transport_error。"""
    url = f"{_gateway_base()}{path}"
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    if key:
        headers["X-Internal-Key"] = key
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
            return status, _safe_json(raw)
    except urllib.error.HTTPError as exc:
        # 409 / 404 / 503 / 400：gateway 返回 JSON body，提取出来
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""
        return int(exc.code), _safe_json(raw)


def reserve(
    *, user_id, job_id, speaker_id, target_model, is_anonymous: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> ReserveResult:
    """预占一个 auto-clone 名额。

    ``is_anonymous=True``（plan 2026-06-14 §3.4）：匿名/快捷 CosyVoice 克隆。
    endpoint 据此用 ``anonymous_clone_daily_global_cap`` / ``anonymous_clone_active_cap``
    全局 cap（owner=sentinel user，per-sentinel cap 天然 = 全局）而非登录态
    per-user express cap。默认 False = 登录态 express auto-clone（行为不变）。
    """
    payload = {
        "user_id": str(user_id),
        "job_id": str(job_id),
        "speaker_id": str(speaker_id),
        "target_model": str(target_model),
        "is_anonymous": bool(is_anonymous),
    }
    try:
        status, body = _post_json(_RESERVE_PATH, payload, timeout=timeout)
    except OSError as exc:  # URLError/ConnectionError/TimeoutError 都是 OSError 子类
        logger.warning("express reserve transport error: %s", exc)
        return ReserveResult(ok=False, http_status=0, error="transport_error")
    if status == 200:
        # 成功必须带 reservation_id —— 否则没有可 consume/release 的句柄，
        # 视为 malformed（Codex E-fix item 1）：ok=False 阻止越过成本闸。
        if body.get("ok") and body.get("reservation_id"):
            return ReserveResult(
                ok=True,
                http_status=200,
                reservation_id=body.get("reservation_id"),
                idempotent_hit=bool(body.get("idempotent_hit")),
            )
        return ReserveResult(ok=False, http_status=200, error="malformed_reserve_response")
    if status == 409:
        return ReserveResult(ok=False, http_status=409, deny_reason=body.get("deny_reason"))
    return ReserveResult(
        ok=False, http_status=status, error=body.get("error") or "reserve_failed"
    )


def consume(
    reservation_id, *, voice_id, timeout: float = _DEFAULT_TIMEOUT_S
) -> TransitionResult:
    path = _CONSUME_PATH.format(rid=str(reservation_id))
    try:
        status, body = _post_json(path, {"voice_id": str(voice_id)}, timeout=timeout)
    except OSError as exc:  # URLError/ConnectionError/TimeoutError 都是 OSError 子类
        logger.warning("express consume transport error: %s", exc)
        return TransitionResult(ok=False, http_status=0, error="transport_error")
    if status == 200 and body.get("ok"):
        return TransitionResult(ok=True, http_status=200, status=body.get("status"))
    return TransitionResult(
        ok=False,
        http_status=status,
        status=body.get("status"),
        conflict_reason=body.get("conflict_reason"),
        error=body.get("error"),
    )


def release(
    reservation_id, *, reason, timeout: float = _DEFAULT_TIMEOUT_S
) -> TransitionResult:
    path = _RELEASE_PATH.format(rid=str(reservation_id))
    try:
        status, body = _post_json(path, {"reason": str(reason)}, timeout=timeout)
    except OSError as exc:  # URLError/ConnectionError/TimeoutError 都是 OSError 子类
        logger.warning("express release transport error: %s", exc)
        return TransitionResult(ok=False, http_status=0, error="transport_error")
    if status == 200 and body.get("ok"):
        return TransitionResult(ok=True, http_status=200, status=body.get("status"))
    return TransitionResult(
        ok=False,
        http_status=status,
        status=body.get("status"),
        conflict_reason=body.get("conflict_reason"),
        error=body.get("error"),
    )


__all__ = ["ReserveResult", "TransitionResult", "reserve", "consume", "release"]
