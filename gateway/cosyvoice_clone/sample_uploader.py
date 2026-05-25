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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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


# Codex 2026-05-25 C.2 二轮 review 部署前项 #A：``GatewaySettings.cosyvoice_sample_uploader``
# 是 ``Literal["local_fs_stub", "aliyun_oss"]``，但工厂当前只实现 stub。
# 下面三个集合显式区分"配置 schema 允许的值" vs "工厂已实现的值" vs "生产可用值"：
#
# - KNOWN_BACKENDS    ：配置 schema 接受（防 unknown env 反向探测）
# - IMPLEMENTED_BACKENDS：工厂能真造出 uploader 实例
# - PRODUCTION_READY_BACKENDS：DashScope 可访问 → 真实联调可走
#
# 当前状态（Phase 4.1 C.2）：
#   IMPLEMENTED = {"local_fs_stub"}  — 仅本地 stub
#   PROD_READY  = frozenset()        — 0；联调前 AliyunOssUploader 落地后加 "aliyun_oss"
#
# 这让 endpoint Layer 3 可以两步 fail-closed：
#   1. stub backend → 503 sample_uploader_not_configured（明确告知运维切换）
#   2. 未实现的 backend（aliyun_oss）→ 503 sample_uploader_not_implemented
#      （防止 endpoint 在 transcode + 上传后才 500，给运维明确信号）
#
# Phase 4.1.x AliyunOssUploader 落地时同步更新这两个集合 + 测试。
KNOWN_BACKENDS: frozenset[str] = frozenset({"local_fs_stub", "aliyun_oss"})
IMPLEMENTED_BACKENDS: frozenset[str] = frozenset({"local_fs_stub"})
PRODUCTION_READY_BACKENDS: frozenset[str] = frozenset()  # Phase 4.1.x 加 "aliyun_oss"


def build_sample_uploader_from_settings(settings: object) -> SampleUploader:
    """根据 ``GatewaySettings`` 构造合适的 uploader 实现。

    当前只支持 ``local_fs_stub``（默认）。``aliyun_oss`` 在 config schema 中
    已预留，但工厂未实现 —— 调用此函数会抛 ``NotImplementedError``。

    生产 endpoint 应该在调用此函数 **之前** 用 ``PRODUCTION_READY_BACKENDS``
    检查（``gateway/cosyvoice_clone/api.py`` Layer 3），让无 OSS 配置时
    直接 503 而不是走到这里抛 500。

    Phase 4.1.x AliyunOssUploader 落地时：
        if backend == "aliyun_oss":
            return AliyunOssUploader(...)
        # 并把 "aliyun_oss" 加进 IMPLEMENTED + PRODUCTION_READY
    """
    backend = getattr(settings, "cosyvoice_sample_uploader", "local_fs_stub")
    base_dir_str = getattr(settings, "cosyvoice_sample_local_dir", None) or str(
        DEFAULT_LOCAL_FS_DIR
    )
    if backend == "local_fs_stub":
        return LocalFsStubUploader(base_dir=Path(base_dir_str))
    if backend == "aliyun_oss":
        # 工厂级 fail-closed：明确 NotImplementedError，让 endpoint Layer 3
        # 守卫早期 503，而不是 transcode 完才 500。
        raise NotImplementedError(
            "AliyunOssUploader 尚未实现；当前 sample_uploader=aliyun_oss 仅是"
            "配置占位。Phase 4.1.x 真实联调前必须补 AliyunOssUploader 实现。"
        )
    raise ValueError(
        f"Unknown cosyvoice_sample_uploader={backend!r}; "
        f"valid values: {sorted(KNOWN_BACKENDS)}"
    )
