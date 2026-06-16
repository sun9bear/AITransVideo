"""Startup-time validation helpers for the gateway.

Pure functions with no import-time side effects (no DB, no FastAPI, no network).
Designed to be called from gateway/main.py's `lifespan` startup block and to
be directly unit-testable without stubbing `database`, `auth`, etc.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_KNOWN_ENVS = {"dev", "test", "staging", "prod", "production"}
_PRODUCTION_ENVS = {"prod", "production"}


def validate_environment_name(env: str) -> str:
    """Validate and normalize AVT_ENV.

    Unknown environment names are dangerous because feature gates often default
    to development semantics. Fail at startup instead of letting typos such as
    ``prd`` silently bypass production-only guards.
    """
    normalized = (env or "").strip().lower()
    if normalized not in _KNOWN_ENVS:
        allowed = ", ".join(sorted(_KNOWN_ENVS))
        raise RuntimeError(
            f"Gateway startup refused: AVT_ENV must be one of {{{allowed}}}; "
            f"got {env!r}."
        )
    return normalized


def validate_production_safety(env: str, auth_required: bool) -> None:
    """Pure check: refuse to start if production mode has auth disabled.

    Standalone function so tests can call directly without reloading
    gateway.main (which triggers FastAPI app re-construction side effects).
    """
    normalized_env = validate_environment_name(env)
    if normalized_env in _PRODUCTION_ENVS and not auth_required:
        raise RuntimeError(
            f"Refusing to start: AVT_ENV={normalized_env} requires AVT_AUTH_REQUIRED=true. "
            "Disabling auth in production would expose all jobs to anonymous access."
        )


def validate_internal_api_key(key: str) -> None:
    """Refuse to start if AVT_INTERNAL_API_KEY is unset or too short (T4).

    Without a key, internal endpoints fail closed (the request-time check
    returns 503), but that's noisy. Force operators to set it explicitly
    so misconfigured deploys surface at startup, not at first 503.

    Minimum 16 chars. 32+ random chars recommended (see .env.example).
    """
    if not key or len(key) < 16:
        raise RuntimeError(
            "Gateway startup refused: AVT_INTERNAL_API_KEY must be set "
            "(minimum 16 chars, recommended: 32+ random chars). "
            "Generate: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
        )


def is_startup_recovery_schema_missing_error(exc: BaseException) -> bool:
    """Return whether a startup recovery failure is the expected pre-migration case.

    The stale-task recovery hooks run during Gateway startup. In a fresh local
    environment, or before a migration is applied, the queue tables may not
    exist yet. That case should be visible as a warning, while unrelated
    recovery failures should be logged with full exception details.

    This is intentionally string-based because startup may see different DB
    driver exception types. The safe failure direction is to return False: an
    unrecognized schema-missing error is logged with logger.exception instead
    of being silently swallowed.
    """
    text = f"{type(exc).__module__}.{type(exc).__name__}: {exc}".lower()
    if "undefinedtable" in text or "no such table" in text:
        return True
    if "doesn't exist" in text and "table" in text:
        return True
    return "relation" in text and "does not exist" in text


def validate_r2_backend(
    backend: str,
    r2_endpoint: str,
    r2_access_key_id: str,
    r2_secret_access_key: str,
) -> str:
    """Check R2 config consistency and return the effective backend name.

    Phase 2 R2 download backend (plan 2026-04-23).

    Contract:
      - When backend == "local" (default): returns "local" unconditionally,
        even if R2 credentials are missing. "local" is the always-safe path.
      - When backend == "r2" AND all three credentials are non-empty:
        returns "r2".
      - When backend == "r2" but any credential is missing: logs CRITICAL
        and DOWNGRADES to "local" instead of raising. Rationale: the
        gateway must keep serving downloads — a misconfigured R2 flag
        should never take the service down. Ops notice the CRITICAL log.

    The returned string should replace ``settings.download_redirect_backend``
    on the live settings object (or a per-process copy) so that request-time
    code reads the effective (not configured) backend. This is the single
    source of truth for "is R2 really on".

    Returns:
        "local" or "r2" — the effective backend after safety downgrade.
    """
    backend = (backend or "").strip().lower()
    if backend not in ("local", "r2"):
        logger.critical(
            "AVT_DOWNLOAD_REDIRECT_BACKEND=%r is not one of {local, r2}; "
            "downgrading to local.",
            backend,
        )
        return "local"

    if backend == "local":
        return "local"

    # backend == "r2"
    missing = [
        name
        for name, value in (
            ("R2_ENDPOINT", r2_endpoint),
            ("R2_ACCESS_KEY_ID", r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", r2_secret_access_key),
        )
        if not value
    ]
    if missing:
        logger.critical(
            "AVT_DOWNLOAD_REDIRECT_BACKEND=r2 but required credential(s) missing: %s. "
            "Downgrading to local. Downloads will continue via the legacy "
            "gateway -> Job API byte passthrough.",
            ", ".join(missing),
        )
        return "local"

    logger.info(
        "Phase 2 R2 download backend ENABLED (endpoint=%s, bucket inferred from settings).",
        r2_endpoint,
    )
    return "r2"


def validate_mainland_voice_worker_config(
    enabled: bool,
    url: str,
    hmac_key_id: str,
    hmac_secret: str,
) -> bool:
    """检查 mainland_voice_worker 配置一致性，返回 effective ``enabled``。

    plan 2026-05-24 Phase 1.5 — 接 Gateway 配置层。语义对齐 R2 backend：

    - ``enabled=False``（默认）：直接返 False，无需任何 secret。
    - ``enabled=True`` 且 url / hmac_key_id / hmac_secret **三者齐备**：
      返 True，gateway 可以构造 ``MainlandWorkerClient``。
    - ``enabled=True`` 但任一 secret 缺失：CRITICAL log + **降级返 False**，
      不抛异常。理由：mainland_voice_worker 是子能力，gateway 主路径
      不应该因为可选 worker 配错就启动失败。

    日志安全：CRITICAL 消息只打 url + hmac_key_id 的存在性，**永远不打
    secret 实体**。
    """
    if not enabled:
        return False

    missing: list[str] = []
    if not url:
        missing.append("AVT_MAINLAND_VOICE_WORKER_URL")
    if not hmac_key_id:
        missing.append("AVT_MAINLAND_VOICE_WORKER_HMAC_KEY_ID")
    if not hmac_secret:
        missing.append("AVT_MAINLAND_VOICE_WORKER_HMAC_SECRET")

    if missing:
        logger.critical(
            "AVT_MAINLAND_VOICE_WORKER_ENABLED=true but required config missing: %s. "
            "Downgrading mainland_voice_worker to DISABLED. "
            "Set the missing env var(s) and recreate the gateway container to enable.",
            ", ".join(missing),
        )
        return False

    logger.info(
        "Mainland voice worker ENABLED (url=%s, key_id=%s)",
        url,
        hmac_key_id,
        # 注意：故意不传 hmac_secret，避免任何日志路径打印 secret
    )
    return True


def validate_pan_backup_config(settings) -> None:
    """Validate pan backup env if feature enabled. CRITICAL at startup.

    Plan 2026-05-13 design §5.3 / 2026-05-14 impl T2.2.

    If AVT_ENABLE_PAN_BACKUP=false (default): no-op. Otherwise, all 4 of
    appkey / appsecret / redirect_uri / Fernet key must be set AND the Fernet
    key must decode as a valid 32-byte base64-encoded key.

    Raises:
        RuntimeError with actionable message naming the missing env var(s).
    """
    if not settings.enable_pan_backup:
        return

    required = [
        ("AVT_BAIDU_PAN_APPKEY", settings.baidu_pan_appkey),
        ("AVT_BAIDU_PAN_APPSECRET", settings.baidu_pan_appsecret),
        ("AVT_BAIDU_PAN_REDIRECT_URI", settings.baidu_pan_redirect_uri),
        ("AVT_PAN_TOKEN_ENCRYPTION_KEY", settings.pan_token_encryption_key),
    ]
    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            f"AVT_ENABLE_PAN_BACKUP=true but required env vars missing: {missing}. "
            f"Either set them in .env or AVT_ENABLE_PAN_BACKUP=false."
        )

    # Verify Fernet key is a real 32-byte url-safe base64 key
    try:
        from cryptography.fernet import Fernet
        Fernet(settings.pan_token_encryption_key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"AVT_PAN_TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}. "
            f"Generate one with: python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\""
        )


def validate_anonymous_preview_config(settings) -> None:
    """Validate anonymous preview config and downgrade flag if secret is missing/short.

    Plan 2026-06-10 APF T1. Mirrors validate_mainland_voice_worker_config pattern:
    degrading, not raising — gateway must keep serving other requests.

    Contract:
      - enable_anonymous_preview=False (default): no-op.
      - enable_anonymous_preview=True AND hash_secret >= 32 bytes: no-op (valid).
      - enable_anonymous_preview=True but hash_secret missing or < 32 bytes:
        log CRITICAL and FORCE settings.enable_anonymous_preview = False.
        Rationale: without a server-side HMAC key, scope_key hashes are trivially
        reversible (no secret = deterministic hash = IP enumerable from hash).
        The feature must not run without proper key hygiene.

    Side effect: mutates settings.enable_anonymous_preview on downgrade.
    Caller (gateway/main.py lifespan) should call this after loading settings.
    """
    if not settings.enable_anonymous_preview:
        return

    secret = settings.anonymous_preview_hash_secret or ""
    if len(secret) < 32:
        logger.critical(
            "AVT_ENABLE_ANONYMOUS_PREVIEW=true but AVT_ANONYMOUS_PREVIEW_HASH_SECRET "
            "is missing or too short (got %d bytes, need ≥32). "
            "Downgrading enable_anonymous_preview to False. "
            "Generate a secret with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
            len(secret),
        )
        settings.enable_anonymous_preview = False
        return

    logger.info(
        "Anonymous preview ENABLED (max_seconds=%d, cap_global=%d/day, "
        "cap_ip=%d/day, cap_device=%d/day, cap_source=%d/day)",
        settings.anonymous_preview_max_seconds,
        settings.anonymous_preview_cap_global_per_day,
        settings.anonymous_preview_cap_per_ip,
        settings.anonymous_preview_cap_per_device,
        settings.anonymous_preview_cap_per_source,
    )
