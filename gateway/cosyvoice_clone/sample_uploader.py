"""Sample artifact 上传抽象（Phase 4.1 C 子模块）。

CosyVoice clone API 接受 **样本 URL**（HTTP/HTTPS / OSS signed URL）。
Gateway 必须先把转码后的 sample bytes 落到一个**对 DashScope 服务端
可见**的存储后端，再把 URL 传给 worker。

后端选项（plan §OSS 路径决策 / Open Question #5）：

| 后端 | 优势 | 劣势 |
|---|---|---|
| 阿里云 OSS（生产推荐） | 中国境内 / DashScope 低延迟 / signed URL TTL 可控 | 需 OSS 凭证 + bucket 配置 |
| Cloudflare R2 | 已集成（Phase 2 R2 download backend） | DashScope 跨境拉取慢 / CF 边缘命中率不可控 |
| 武汉 ECS Nginx 临时 path | Phase -1 实测可用 | 5 Mbps 带宽 + 安全风险 |

本模块定义 ``SampleUploader`` Protocol，并提供两个内置实现：

- ``LocalFsStubUploader``：Phase 4.1 C.2 默认。把 bytes 写到 gateway
  本地临时目录、返 ``file://...`` URL。**DashScope 无法访问此 URL**，
  仅供本地开发期 e2e 测试业务路径（fail-closed 验证 / 错误码映射等）。
  **生产部署前必须替换为真实 OSS / R2 uploader。**
- ``InMemoryUploader``：测试用。bytes 暂存在 dict，``upload_and_sign()``
  返 ``mem://...`` URL。``fetch()`` 取回 bytes（仅测试 API）。

部署期替换：通过 ``GatewaySettings`` 加 env 字段 ``AVT_COSYVOICE_SAMPLE_UPLOADER``
决定使用哪个实现（``local_fs_stub`` / ``aliyun_oss``）。Phase 4.1.x 真实部署
前补 Aliyun OSS implementation（不在 C.2 范围）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


class SampleUploader(Protocol):
    """Sample bytes → DashScope-reachable URL 的抽象接口。"""

    def upload_and_sign(
        self,
        data: bytes,
        *,
        filename_hint: str = "sample.wav",
        ttl_seconds: int = 3600,
    ) -> str:
        """上传 audio bytes 到后端存储；返回短 TTL public URL。

        Parameters
        ----------
        data : bytes
            转码后的 sample（音频）字节流。
        filename_hint : str
            原文件名提示（仅用作日志 / 用户友好名，不影响 URL 路径）。
        ttl_seconds : int
            URL 有效期。生产 OSS 实现应当用此参数生成 signed URL；stub
            实现忽略。

        Returns
        -------
        str
            DashScope 可访问的 URL。
        """
        ...


class SupportsDeleteUploadedUrl(Protocol):
    """Optional cleanup hook implemented by real object-store uploaders."""

    def delete_uploaded_url(self, url: str) -> None:
        """Best-effort delete for the object behind a signed URL."""
        ...


# ---------------------------------------------------------------------------
# LocalFsStubUploader：Phase 4.1 C.2 默认实现，本地文件 + file:// URL。
# ---------------------------------------------------------------------------

@dataclass
class LocalFsStubUploader:
    """写本地 gateway 临时目录、返 ``file://...`` URL。

    **本实现仅用于本地开发** —— DashScope 服务端无法访问 ``file://``。
    真实联调必须替换为 OSS / R2 uploader（plan §OSS 路径决策 待定）。
    """
    base_dir: Path
    # 当本地 stub 接到上传时打一条 warning 提醒部署期切换 —— 避免被误用。
    _warned: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def upload_and_sign(
        self,
        data: bytes,
        *,
        filename_hint: str = "sample.wav",
        ttl_seconds: int = 3600,
    ) -> str:
        if not self._warned:
            logger.warning(
                "[sample_uploader] LocalFsStubUploader 仅用于本地开发；"
                "DashScope 无法访问 file:// URL。部署前必须切换 AVT_COSYVOICE_SAMPLE_UPLOADER 到真实 OSS 实现。"
            )
            self._warned = True

        # 用 sha256 + timestamp 生成稳定文件名，避免冲突
        digest = hashlib.sha256(data).hexdigest()[:16]
        ext = _safe_extension(filename_hint)
        fname = f"sample_{int(time.time())}_{digest}{ext}"
        path = self.base_dir / fname
        path.write_bytes(data)
        # POSIX file URL: file:///absolute/path
        return path.resolve().as_uri()


def _safe_extension(filename_hint: str) -> str:
    """从 filename_hint 提取扩展名；缺省 ``.wav``。"""
    if not filename_hint:
        return ".wav"
    ext = Path(filename_hint).suffix.lower()
    if ext in {".wav", ".mp3", ".m4a"}:
        return ext
    return ".wav"


# ---------------------------------------------------------------------------
# InMemoryUploader：单元测试用，bytes 全存内存。
# ---------------------------------------------------------------------------

@dataclass
class InMemoryUploader:
    """测试 stub：返 ``mem://<key>`` URL，``fetch(url)`` 取回 bytes。

    便于 endpoint e2e 测试断言 "uploader 收到的 bytes == audio_processor 输出"，
    不真实写盘。
    """
    _store: dict[str, bytes] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    calls: list[dict] = field(default_factory=list)

    def upload_and_sign(
        self,
        data: bytes,
        *,
        filename_hint: str = "sample.wav",
        ttl_seconds: int = 3600,
    ) -> str:
        with self._lock:
            digest = hashlib.sha256(data).hexdigest()[:16]
            key = f"{int(time.time() * 1000)}_{digest}"
            self._store[key] = data
            self.calls.append({
                "filename_hint": filename_hint,
                "ttl_seconds": ttl_seconds,
                "size": len(data),
                "digest_prefix": digest,
            })
        return f"mem://{key}"

    def fetch(self, url: str) -> bytes:
        """根据 ``mem://<key>`` URL 取回 bytes（仅测试用）。"""
        if not url.startswith("mem://"):
            raise ValueError(f"InMemoryUploader.fetch expects mem:// URL, got {url!r}")
        key = url[len("mem://"):]
        with self._lock:
            return self._store[key]


# ---------------------------------------------------------------------------
# 工厂：根据 settings 选实现（Phase 4.1.x 时扩展添加 aliyun_oss / r2 选项）
# ---------------------------------------------------------------------------

DEFAULT_LOCAL_FS_DIR = Path("/tmp/aivideotrans/cosyvoice_samples")
DEFAULT_OSS_CONTENT_TYPE = "audio/wav"
ALIYUN_OSS_REQUIRED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("AVT_COSYVOICE_OSS_ENDPOINT", "cosyvoice_oss_endpoint"),
    ("AVT_COSYVOICE_OSS_BUCKET", "cosyvoice_oss_bucket"),
    ("AVT_COSYVOICE_OSS_ACCESS_KEY_ID", "cosyvoice_oss_access_key_id"),
    ("AVT_COSYVOICE_OSS_ACCESS_KEY_SECRET", "cosyvoice_oss_access_key_secret"),
)


@dataclass
class AliyunOssUploader:
    """Upload clone samples to Alibaba Cloud OSS and return short-TTL URLs.

    The implementation uses OSS's S3-compatible API through boto3. OSS only
    supports virtual-hosted-style requests, and Python/boto3 should use the
    OSS-compatible Signature V2 mode (``signature_version="s3"``).
    """

    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    region: str = "cn-beijing"
    key_prefix: str = "cosyvoice/clone-samples"
    connect_timeout_s: int = 10
    read_timeout_s: int = 30
    content_type: str = DEFAULT_OSS_CONTENT_TYPE
    _client: object | None = field(default=None, init=False, repr=False)
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _url_to_key: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("endpoint", self.endpoint),
                ("bucket", self.bucket),
                ("access_key_id", self.access_key_id),
                ("access_key_secret", self.access_key_secret),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise ValueError(
                "AliyunOssUploader missing required config: " + ", ".join(missing)
            )
        self.endpoint = str(self.endpoint).strip().rstrip("/")
        self.bucket = str(self.bucket).strip()
        self.region = str(self.region or "cn-beijing").strip()
        self.key_prefix = _normalize_key_prefix(self.key_prefix)

    def upload_and_sign(
        self,
        data: bytes,
        *,
        filename_hint: str = "sample.wav",
        ttl_seconds: int = 3600,
    ) -> str:
        if not data:
            raise ValueError("AliyunOssUploader refuses to upload empty sample")
        ttl = int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")

        ext = _safe_extension(filename_hint)
        digest = hashlib.sha256(data).hexdigest()
        key = self._build_object_key(digest=digest, ext=ext)
        content_type = _content_type_for_ext(ext)

        client = self._get_client()
        client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        # 2026-05-26: 不要在 presign 里加 ``ResponseContentType``——阿里云 OSS 对
        # 重写响应 Content-Type 返回 400 ``Can not override response header on
        # content-type`` (EC 0017-00000902)。Object 的 Content-Type 在 PUT 时
        # 已经写好（``ContentType=content_type``），DashScope 拉取时按 metadata
        # 拿到正确头即可，无需 query-string 覆盖。
        signed_url = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
            },
            ExpiresIn=ttl,
            HttpMethod="GET",
        )
        with self._client_lock:
            self._url_to_key[signed_url] = key
        return signed_url

    def delete_uploaded_url(self, url: str) -> None:
        """Delete the object behind ``url`` after the synchronous clone call."""
        with self._client_lock:
            key = self._url_to_key.pop(url, None)
        if not key:
            key = _object_key_from_signed_url(url)
        if not key:
            raise ValueError("cannot infer OSS object key from signed URL")
        self._get_client().delete_object(Bucket=self.bucket, Key=key)

    def _build_object_key(self, *, digest: str, ext: str) -> str:
        now = datetime.now(timezone.utc)
        date_path = now.strftime("%Y/%m/%d")
        return (
            f"{self.key_prefix}/{date_path}/"
            f"{uuid.uuid4().hex}_{digest[:16]}{ext}"
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client

            import boto3  # type: ignore[import-untyped]
            from botocore.client import Config  # type: ignore[import-untyped]

            config = Config(
                signature_version="s3",
                region_name=self.region,
                connect_timeout=self.connect_timeout_s,
                read_timeout=self.read_timeout_s,
                retries={"max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "virtual"},
            )
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
                config=config,
            )
            return self._client


def missing_aliyun_oss_settings(settings: object) -> list[str]:
    """Return missing ``AVT_COSYVOICE_OSS_*`` env names for early 503 gates."""
    return [
        env_name
        for env_name, attr in ALIYUN_OSS_REQUIRED_SETTINGS
        if not str(getattr(settings, attr, "") or "").strip()
    ]


# Codex 2026-05-25 C.2 二轮 review 部署前项 #A：``GatewaySettings.cosyvoice_sample_uploader``
# 是 ``Literal["local_fs_stub", "aliyun_oss"]``。``aliyun_oss`` 已由
# Phase 4.1.x 落地，stub 仍只用于本地开发。
# 下面三个集合显式区分"配置 schema 允许的值" vs "工厂已实现的值" vs "生产可用值"：
#
# - KNOWN_BACKENDS    ：配置 schema 接受（防 unknown env 反向探测）
# - IMPLEMENTED_BACKENDS：工厂能真造出 uploader 实例
# - PRODUCTION_READY_BACKENDS：DashScope 可访问 → 真实联调可走
#
# 当前状态（Phase 4.1.x）：
#   IMPLEMENTED = {"local_fs_stub", "aliyun_oss"}
#   PROD_READY  = {"aliyun_oss"}
#
# 这让 endpoint Layer 3 可以两步 fail-closed：
#   1. stub backend → 503 sample_uploader_not_configured（明确告知运维切换）
#   2. 未实现的 backend（aliyun_oss）→ 503 sample_uploader_not_implemented
#      （防止 endpoint 在 transcode + 上传后才 500，给运维明确信号）
#
KNOWN_BACKENDS: frozenset[str] = frozenset({"local_fs_stub", "aliyun_oss"})
IMPLEMENTED_BACKENDS: frozenset[str] = frozenset({"local_fs_stub", "aliyun_oss"})
PRODUCTION_READY_BACKENDS: frozenset[str] = frozenset({"aliyun_oss"})


def build_sample_uploader_from_settings(settings: object) -> SampleUploader:
    """根据 ``GatewaySettings`` 构造合适的 uploader 实现。

    ``local_fs_stub`` 仅用于本地开发。``aliyun_oss`` 是生产实现，但必须
    配齐 ``AVT_COSYVOICE_OSS_*`` 设置。

    生产 endpoint 应该在调用此函数 **之前** 用 ``PRODUCTION_READY_BACKENDS``
    检查（``gateway/cosyvoice_clone/api.py`` Layer 3），让无 OSS 配置时
    直接 503 而不是走到这里抛 500。

    endpoint 会在读样本前用 ``missing_aliyun_oss_settings`` 做早期 503；
    工厂这里重复校验是防御式兜底。
    """
    backend = getattr(settings, "cosyvoice_sample_uploader", "local_fs_stub")
    base_dir_str = getattr(settings, "cosyvoice_sample_local_dir", None) or str(
        DEFAULT_LOCAL_FS_DIR
    )
    if backend == "local_fs_stub":
        return LocalFsStubUploader(base_dir=Path(base_dir_str))
    if backend == "aliyun_oss":
        missing = missing_aliyun_oss_settings(settings)
        if missing:
            raise ValueError(
                "cosyvoice_sample_uploader=aliyun_oss but required config missing: "
                + ", ".join(missing)
            )
        return AliyunOssUploader(
            endpoint=getattr(settings, "cosyvoice_oss_endpoint"),
            bucket=getattr(settings, "cosyvoice_oss_bucket"),
            access_key_id=getattr(settings, "cosyvoice_oss_access_key_id"),
            access_key_secret=getattr(settings, "cosyvoice_oss_access_key_secret"),
            region=getattr(settings, "cosyvoice_oss_region", "cn-beijing"),
            key_prefix=getattr(
                settings, "cosyvoice_oss_key_prefix", "cosyvoice/clone-samples"
            ),
            connect_timeout_s=int(
                getattr(settings, "cosyvoice_oss_connect_timeout_s", 10) or 10
            ),
            read_timeout_s=int(
                getattr(settings, "cosyvoice_oss_read_timeout_s", 30) or 30
            ),
        )
    raise ValueError(
        f"Unknown cosyvoice_sample_uploader={backend!r}; "
        f"valid values: {sorted(KNOWN_BACKENDS)}"
    )


def _normalize_key_prefix(value: str) -> str:
    prefix = str(value or "cosyvoice/clone-samples").strip().strip("/")
    cleaned = "/".join(
        part for part in prefix.split("/") if part not in {"", ".", ".."}
    )
    return cleaned or "cosyvoice/clone-samples"


def _content_type_for_ext(ext: str) -> str:
    if ext == ".mp3":
        return "audio/mpeg"
    if ext == ".m4a":
        return "audio/mp4"
    return "audio/wav"


def _object_key_from_signed_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if not path:
        return ""
    return unquote(path)
