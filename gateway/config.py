"""Gateway configuration loaded from environment variables."""

from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    """All gateway configuration comes from environment variables."""

    # Upstream services
    job_api_upstream: str = "http://127.0.0.1:8877"
    jobs_dir: str = Field(
        default="/opt/aivideotrans/app/jobs",
        validation_alias="AIVIDEOTRANS_JOBS_DIR",
    )
    runtime_logs_dir: str = Field(
        default="/opt/aivideotrans/data/runtime_logs",
        validation_alias="AIVIDEOTRANS_RUNTIME_LOGS_DIR",
    )

    # Gateway server
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8880

    # Deployment environment ("dev" / "staging" / "production").
    # Read from AVT_ENV. Used by startup validators (see startup_checks.py);
    # production mode refuses to start with auth_required=False.
    env: str = "dev"

    # Database (Step 2)
    pg_password: str = ""
    database_url: str = ""

    # Internal API key (T4) — guards /api/internal/* endpoints.
    # Set via AVT_INTERNAL_API_KEY. Startup (validate_internal_api_key) refuses
    # to run if unset/too short; per-request dependency _require_internal_access
    # re-reads this at request time so tests can monkeypatch it.
    internal_api_key: str = ""

    # Auth (Step 2)
    auth_required: bool = True
    session_expire_days: int = 7
    session_cookie_name: str = "avt_session"

    # CORS — set via env var AVT_CORS_ORIGINS (comma-separated)
    # e.g. AVT_CORS_ORIGINS="https://aivideotrans.site,https://www.aivideotrans.site"
    cors_origins: str = "https://aivideotrans.site"

    # --- Phone auth (Task 3) ---
    # Default to "fake" so local dev, tests, and preview builds do not require any
    # real SMS or captcha vendor credentials. The real-provider path is intentionally
    # out of scope for Task 3.
    sms_provider: str = "fake"
    captcha_provider: str = "fake"
    geetest_api_server: str = "http://gcaptcha4.geetest.com"
    geetest_register_captcha_id: str = ""
    geetest_register_captcha_key: str = ""
    geetest_login_captcha_id: str = ""
    geetest_login_captcha_key: str = ""

    # OTP lifetime. Kept deliberately short.
    phone_code_ttl_seconds: int = 300  # 5 minutes
    phone_code_length: int = 6

    # Rate limits for `/auth/phone/send-code`. All thresholds are per window.
    # Phone-based limits block aggressive retries against a single number; IP-based
    # limits blunt bulk-spray attacks from a single origin.
    phone_send_code_window_seconds: int = 60
    phone_send_code_max_per_phone_window: int = 1  # one code per phone per minute
    phone_send_code_hour_window_seconds: int = 3600
    phone_send_code_max_per_phone_hour: int = 5
    phone_send_code_max_per_ip_hour: int = 20

    # Public email registration switch. Phone-first registration stays the
    # default UX, but email can be offered as a secondary path. When false,
    # `POST /auth/register` refuses to create email accounts (returns 403).
    email_registration_enabled: bool = True

    # --- Email auth (registration verification + password reset) ---
    # Default to "fake" so tests/local development do not depend on a live
    # external mail provider. Production can set AVT_EMAIL_AUTH_PROVIDER=resend
    # and reuse notifications.send_email / RESEND_API_KEY.
    email_auth_provider: str = "fake"
    email_code_ttl_seconds: int = 900  # 15 minutes
    email_code_length: int = 6
    email_send_code_window_seconds: int = 60
    email_send_code_max_per_email_window: int = 1
    email_send_code_hour_window_seconds: int = 3600
    email_send_code_max_per_email_hour: int = 5
    email_send_code_max_per_ip_hour: int = 20

    # --- Studio post-edit workflow (plan 2026-04-18 D29) ---
    # Backend gate for the editing endpoints (enter-edit / editing/cancel /
    # editing/commit). Disabled by default so Phase 0 can ship without
    # exposing the T1-1 skeleton to production users. Flip to True once
    # the full Phase 1 flow is ready for dogfooding. Mirrors the frontend
    # flag NEXT_PUBLIC_ENABLE_POST_EDIT which gates the UI entry points.
    enable_post_edit: bool = False

    # --- MiMo free-tier (Phase 2a, plan 2026-05-29) ---
    # Backend gate for service_mode="free" job creation. Disabled by
    # default so the Phase 2a flow lands behind-flag with no public entry.
    # Mirrors the frontend flag NEXT_PUBLIC_ENABLE_FREE_TIER.
    # ⚠️ LAUNCH GATE: public free tier ALSO needs consent/legal sign-off
    # (design §5.3, 《民法典》1023 voice rights). This flag alone does NOT
    # authorize opening it to the public.
    enable_free_tier: bool = False

    # --- Smart Auto Pipeline kill switch (P2 launch blocker #1) ---
    # Layer 1 of the two-layer kill switch. False (default) means the
    # gateway refuses to create smart jobs AND strips "smart" from every
    # user's allowed_service_modes (regardless of plan). Required AND'd
    # with AdminSettings.smart_mode_enabled (Layer 2 — admin hot-flip).
    #
    # Operations:
    #   - Long-term close: docker-compose.yml AVT_ENABLE_SMART_MODE="false"
    #     + gateway recreate. Always-off after deploy.
    #   - Emergency stop without redeploy: flip AdminSettings.smart_mode_enabled
    #     to False via admin UI — takes effect within mtime poll window.
    #
    # Spec: docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3 +
    #       docs/plans/2026-05-24-smart-auto-pipeline-rebaseline.md §3.1
    enable_smart_mode: bool = False

    # --- Phase 2 R2 download backend (plan 2026-04-23) ---
    # Pluggable artifact-download target. "local" (default) keeps the historic
    # gateway → Job API byte-passthrough. "r2" redirects the user with HTTP 302
    # to a short-lived Cloudflare R2 presigned URL. Any R2 error — missing
    # config / HEAD failure / upload timeout / signing exception — auto-falls
    # back to local so users never see a failure (see gateway/storage/
    # backend_router.py). Phase 2 only covers the ``publish.dubbed_video``
    # artifact key; other artifacts keep local path unconditionally.
    download_redirect_backend: Literal["local", "r2"] = "local"

    # R2 credentials & bucket. Env var names intentionally do NOT carry the
    # AVT_ prefix — they follow the upstream Cloudflare-R2 plan convention
    # (§10.1 of 2026-04-21-cloudflare-r2-deployment-plan.md) and match what
    # the existing scripts/phase0_probes/ tooling expects. `validation_alias`
    # bypasses the class-level AVT_ prefix for these four fields.
    r2_endpoint: str = Field(default="", validation_alias="R2_ENDPOINT")
    r2_access_key_id: str = Field(default="", validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(default="", validation_alias="R2_SECRET_ACCESS_KEY")
    r2_artifacts_bucket: str = Field(default="avt-artifacts", validation_alias="R2_ARTIFACTS_BUCKET")

    # Presigned URL TTL in seconds. Deliberately tight (120s = 2 min) so that
    # URL leakage has a very small replay window. User download clicks
    # follow the 302 immediately; slow network clients on the CF edge still
    # have plenty of headroom because the URL only needs to be *accepted*
    # during the TTL, not the full body transferred.
    r2_presigned_expires_s: int = 120

    # Plan 2026-05-07 §11 Stage C / CodeX P2 follow-up (2026-05-12):
    # ``<video src=...>`` players issue multiple Range requests over the
    # full playback window (pause / resume / seek may re-fetch minutes
    # apart). 120s would 403 mid-playback on any video > 2 min. Stream
    # presign uses this larger budget (default 30 min) so a typical
    # workspace play / pause / scrub session stays in one signature
    # window. URLs still aren't permanent — leak window is bounded —
    # and the object key path component itself is opaque
    # (``jobs/{job_id}/g{N}/...``) so it doesn't enumerate.
    #
    # CodeX nit follow-up (2026-05-13): env var name intentionally
    # follows the ``AVT_`` prefix like ``r2_presigned_expires_s`` and
    # ``r2_upload_timeout_s``. Read as ``AVT_R2_STREAM_PRESIGNED_EXPIRES_S``.
    # Only the four R2 *credential* fields (endpoint / key id / secret /
    # bucket) skip the prefix because they match the upstream
    # Cloudflare-R2 plan + ``scripts/phase0_probes/`` convention.
    r2_stream_presigned_expires_s: int = 1800

    # Upload timeout when lazily pushing a never-seen-in-R2 artifact. If the
    # upload cannot complete inside this budget, the router gives up and
    # falls back to local. Kept tight to avoid holding user download
    # requests for too long on a bad day.
    r2_upload_timeout_s: int = 60

    # --- Pan backup (plan 2026-05-13 design / 2026-05-14 implementation) ---
    # Primary feature flag. OFF: all /admin/pan/* endpoints return 404,
    # scanner does not enqueue, OAuth Web Flow rejected at startup gate.
    enable_pan_backup: bool = False
    # 30d-auto-archive sub-flag. Independent of main flag — turn main flag ON
    # first + manual smoke for 1 week, THEN flip this so 30d cron starts.
    pan_auto_archive_enabled: bool = False
    pan_auto_archive_days: int = 30                  # threshold for auto-archive
    pan_auto_archive_hour_bjt: int = 3               # cron trigger hour (BJT)
    pan_auto_archive_max_per_run: int = 5            # per-cron enqueue cap
    pan_auto_archive_dry_run: bool = True            # log candidates only, no enqueue
    pan_orphan_cleanup_weekday: int = 5              # 0=Mon ... 5=Sat
    # 2026-06-01 production fix. Baidu PCS superfile2 API caps partseq at
    # 2048 (errno 31299 "Invalid param part_id" beyond that). At 4 MB
    # chunk size that's a 8 GB single-upload ceiling — Boris Cherny
    # (8.61 GB) and c31b (11 GB) both hit it. Bump default to 16 MB:
    # 16 MB * 2048 partseq = 32 GB single-upload ceiling, covers every
    # project we've seen. Baidu docs cap single chunk at 16 MB for
    # standard API, so this is the safe top end of the published range.
    # Trade-off: each chunk takes ~4x as long to upload, but chunk
    # count drops by ~4x — total wall-clock essentially unchanged,
    # and fewer round-trips mean fewer odds of hitting transient 5xx.
    pan_upload_chunk_bytes: int = 16 * 1024 * 1024   # Baidu Pan 16MB chunk size
    pan_task_stale_hours: int = 4                    # heartbeat staleness threshold

    # 2026-05-26 postmortem P0b v2 (Codex 2nd-round). Global serialization
    # of pan_backup uses pg_try_advisory_lock + backoff polling, NOT
    # blocking pg_advisory_lock. Reason: blocking holds a DB conn during
    # the wait; with pool_size=5 + max_overflow=10 = 15 max conns and the
    # batch API accepting up to 100 jobs, 14 waiters could starve the
    # entire pool. Poll-based waits release the conn between attempts.
    #
    # Default 14400s = 4 hours, MATCHING ``pan_task_stale_hours``.
    # Codex P0b v3 raised this from 1800s (30 min) — too short:
    # realistic backups are 10-30 min and a 15-job batch under serial
    # execution stretches to 2-7 hours of total queue time. With 4h
    # cap, ~8-12 queued backups complete before tail timeout.
    #
    # Codex P0c nit on the previous wording: if a single backup hangs
    # >4h, stale_reaper reaps the BackupRecord row (status='failed')
    # so the user-facing row is unstuck, but the PG session holding
    # the global advisory lock belongs to the wedged executor and
    # only releases when that connection itself dies (server-side
    # idle timeout or container restart). Waiters do NOT immediately
    # get a slot — they keep polling until the dead session is gone.
    # Operationally a permanently-wedged executor still needs a
    # ``docker restart aivideotrans-gateway`` to free the lock fast.
    # Long-term: queue-layer serial consumer (one worker pulling
    # from a queue) instead of N parallel waiters.
    pan_backup_global_lock_timeout_s: int = 14400    # 4 hours max wait
    pan_backup_global_lock_poll_base_s: float = 2.0  # initial poll interval
    pan_backup_global_lock_poll_max_s: float = 30.0  # capped exponential

    # 2026-05-26 postmortem P0c (Codex 2nd-round). Pan backup tar staging.
    # Empty default → falls back to tempfile.gettempdir() → /tmp inside
    # gateway container → host overlay → host root partition. That was
    # the direct vector for the 2026-05-26 disk-full incident.
    # Production sets AVT_PAN_TMP_DIR to a data-disk bind-mount path
    # (e.g. /opt/aivideotrans/data/_pan_tmp); docker-compose volumes
    # block mounts the host path so tar lives on the SSD partition
    # sized for project data, NOT the container overlay.
    pan_tmp_dir: str = ""

    # Free-space preflight ratio: tar.gz needs at least this multiple
    # of project_dir size in the tmp filesystem before tar build starts.
    # 1.5: tar.gz is typically 50-80% of project_dir (compressed video +
    # WAV + JSON), plus headroom for concurrent writes. Operators with
    # WAV-heavy projects (which gzip poorly) might tune higher. Check
    # runs INSIDE the per-job slot lock, so failure localizes to one
    # job — others queued behind keep waiting.
    pan_tmp_free_space_ratio: float = 1.5

    # Baidu Pan OAuth credentials (env names automatically prefixed AVT_).
    baidu_pan_appkey: str = ""
    baidu_pan_appsecret: str = ""
    baidu_pan_redirect_uri: str = ""

    # Fernet key for encrypting pan_credentials tokens at rest (32B base64).
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Loss of key = total token data loss (re-authorize required).
    # See design spec §13: backup primary in 1Password + physical paper.
    pan_token_encryption_key: str = ""

    # --- Mainland Voice Worker（plan 2026-05-24 Phase 1.5 接 Gateway 配置层）---
    # 武汉 ECS 上 mainland_worker 的对外入口。Gateway 通过 HMAC 调用，
    # secret 仅在 env 中存活，不进 admin settings、不进 API response、不进日志。
    #
    # 启用条件：``enabled=true`` 且 url / hmac_key_id / hmac_secret 三者齐备。
    # 任一缺失：``validate_mainland_voice_worker_config()`` 会 CRITICAL log
    # 并把 ``enabled`` 降级为 False（fail-graceful，不阻塞 gateway 启动）。
    #
    # ``hmac_secret`` 用 pydantic SecretStr 让 repr / 序列化时自动 mask；
    # 但当前 pydantic 版本（2.11）的 BaseSettings + env_prefix + SecretStr 组合
    # 在测试 monkeypatch 下行为有 corner case，所以这里仍用 str 但
    # 通过 ``__repr__`` 永远不直接打印整个 settings 对象 +
    # ``test_gateway_logs_redaction``（已有）覆盖兜底；新增守卫专门确保
    # secret 不进 admin endpoint response。
    mainland_voice_worker_enabled: bool = False
    mainland_voice_worker_url: str = ""
    mainland_voice_worker_hmac_key_id: str = ""
    mainland_voice_worker_hmac_secret: str = ""

    # --- Phase 4.1 CosyVoice clone sample uploader backend ---
    # 决定 sample bytes 上传到哪个对象存储拿 short-TTL URL 给 DashScope。
    # 默认 ``local_fs_stub`` 仅用于本地开发（写 file:// URL，DashScope 跨境
    # 不可达）。Codex 2026-05-25 C.2 二轮 review 要求 endpoint 在
    # ``cosyvoice_clone_worker_enabled=True`` 且 backend 仍是 stub 时直接 503，
    # **不读样本 / 不转码 / 不调付费 worker**。生产部署前必须改 env
    # ``AVT_COSYVOICE_SAMPLE_UPLOADER=aliyun_oss``（实现 Phase 4.1.x 补）。
    cosyvoice_sample_uploader: Literal["local_fs_stub", "aliyun_oss"] = "local_fs_stub"
    # 本地 stub 写入目录（仅 ``local_fs_stub`` backend 用）；生产部署可忽略。
    cosyvoice_sample_local_dir: str = ""
    # 阿里云 OSS uploader 配置（仅 ``cosyvoice_sample_uploader=aliyun_oss`` 用）。
    # 使用 OSS S3-compatible API；endpoint 支持官方 S3 endpoint
    # ``https://s3.oss-{region}.aliyuncs.com`` 或已绑定证书的 CNAME endpoint。
    cosyvoice_oss_endpoint: str = ""
    cosyvoice_oss_bucket: str = ""
    cosyvoice_oss_access_key_id: str = ""
    cosyvoice_oss_access_key_secret: str = ""
    cosyvoice_oss_region: str = "cn-beijing"
    cosyvoice_oss_key_prefix: str = "cosyvoice/clone-samples"
    cosyvoice_oss_connect_timeout_s: int = 10
    cosyvoice_oss_read_timeout_s: int = 30

    # --- APF 匿名 Free 预览 (plan 2026-06-10 T1) ---
    # 双端 feature flag 默认关；生产零影响。
    # 开启条件：enable_anonymous_preview=True AND anonymous_preview_hash_secret≥32字节。
    # 启动校验：validate_anonymous_preview_config() — secret 缺失/过短 → CRITICAL+降级
    # （不崩容器）。
    # 同时也需要 AVT_ENABLE_FREE_TIER=true（匿名 surface 不绕过 free tier 总开关，AD-7）。
    enable_anonymous_preview: bool = False

    # 对匿名用户**广告/交付的预览长度**（秒）——/limits ``preview_seconds`` + admission
    # preview_duration 钳值。实际 teaser 文件恒裁 180s（build_intake_probe_fn 硬编码），
    # 故建议保持 180。**不再**作源上传时长上限（2026-06-16 解耦，见下 *_max_source_seconds）。
    anonymous_preview_max_seconds: int = 180

    # 匿名**源视频上传**时长上限（秒）——与上面的预览长度**解耦**（2026-06-16）：源可比
    # 预览长得多（上传长视频、只预览前 ~180s teaser）。intake 据此拒超长源（拒绝→前端
    # "视频时长超限，请更换视频再上传"）。默认 1800s=30min；admin 可调上限见 admin_settings
    # ``_APF_LIMIT_BOUNDS``（30min 默认、最高 180min=10800s）。
    anonymous_preview_max_source_seconds: int = 1800

    # 上传文件大小上限（字节）。远小于登录用户 2GB，防带宽白嫖。
    anonymous_preview_max_upload_bytes: int = 200 * 1024 * 1024  # 200 MB

    # 风控四 key 每日上限（AD-5）：
    #   global  — 全局每日总预览数
    #   per_ip  — 同 IP 每日（受信代理提取）
    #   per_device — 同匿名 session token 每日（即"每设备 1 次/天"）
    #   per_source — 同源文件 sha256 每日（防重复提交相同文件）
    anonymous_preview_cap_global_per_day: int = 500
    anonymous_preview_cap_per_ip: int = 3
    anonymous_preview_cap_per_device: int = 1
    anonymous_preview_cap_per_source: int = 1

    # HMAC-SHA256 密钥，用于对 scope_key (IP/device/source) 做不可逆 hash
    # 再存入 anonymous_preview_daily_usage 表（表内无 raw IP 列，AD-5 F14）。
    # 必须 ≥32 字节才有效；缺失或过短时 validate_anonymous_preview_config()
    # 强制降级 enable_anonymous_preview=False。
    # 生成：python -c "import secrets; print(secrets.token_urlsafe(32))"
    anonymous_preview_hash_secret: str = ""

    model_config = {"env_prefix": "AVT_", "populate_by_name": True}


def resolve_database_url(raw: GatewaySettings) -> str:
    """Resolve final database URL or raise if no credentials provided.

    Pure function — does NOT mutate raw or trigger at import time.
    Caller must invoke this explicitly (typically at app startup).

    Precedence: explicit raw.database_url → pg_password → refuse fallback.
    """
    if raw.database_url:
        return raw.database_url
    if raw.pg_password:
        encoded = quote_plus(raw.pg_password)
        return f"postgresql+asyncpg://avt:{encoded}@127.0.0.1:5432/aivideotrans"
    raise RuntimeError(
        "Gateway startup refused: neither AVT_PG_PASSWORD nor AVT_DATABASE_URL is set. "
        "Refusing to fall back to default 'avt:avt' credentials. "
        "Set AVT_PG_PASSWORD (preferred) or AVT_DATABASE_URL explicitly."
    )


settings = GatewaySettings()
# NOTE: database_url is NOT populated here. gateway/main.py is responsible
# for calling resolve_database_url(settings) explicitly at startup. Import of
# this module must not raise on missing creds, so tests can import config
# in a clean env without side effects.
