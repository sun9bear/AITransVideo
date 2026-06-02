"""APF2c-3 anonymous preview rate-limit counter wrapper tests.

Exercises ``src.services.anonymous_preview_rate_limit`` and verifies
that the in-memory store is structurally compatible with
``AnonymousPreviewBackendAdapter`` without standing up any real
backend, gateway, frontend, upload, probe, compliance, preview media,
clone provider, pricing, payment, migration or deployment code.

Boundary checks (import / AST guards) live alongside the behavior
tests so any future drift towards filesystem / network / subprocess /
provider calls fails this file in CI. No ``skip`` / ``xfail`` markers
are used.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from src.services.anonymous_preview_rate_limit import (
    InMemoryRateLimitCounterStore,
    RateLimitCounterUnavailable,
    UnavailableRateLimitCounterStore,
)
from src.services.anonymous_preview_backend_adapter import (
    AnonymousPreviewBackendAdapter,
    RequestFacts,
    UploadFacts,
)
from src.services.anonymous_preview_intake import (
    SHANGHAI,
    ComplianceResult,
    ComplianceStatus,
    IntakeConfig,
    PreviewStatus,
    ProbeResult,
    SourceType,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "services"
    / "anonymous_preview_rate_limit.py"
)

_FROZEN_NOW = datetime(2026, 6, 2, 23, 0, 0, tzinfo=SHANGHAI)


# ---------------------------------------------------------------------------
# Behavior — InMemoryRateLimitCounterStore.
# ---------------------------------------------------------------------------


def test_missing_key_returns_zero() -> None:
    store = InMemoryRateLimitCounterStore()
    assert store.get("global:2026-06-02") == 0
    assert store.get("ip:hashed-ip:2026-06-02") == 0


def test_increment_increases_same_key_each_call() -> None:
    store = InMemoryRateLimitCounterStore()
    assert store.increment("source:abc:2026-06-02") == 1
    assert store.increment("source:abc:2026-06-02") == 2
    assert store.increment("source:abc:2026-06-02") == 3
    assert store.get("source:abc:2026-06-02") == 3


def test_independent_keys_are_isolated() -> None:
    store = InMemoryRateLimitCounterStore()
    store.increment("ip:hashed-a:2026-06-02")
    store.increment("ip:hashed-a:2026-06-02")
    store.increment("device:hashed-b:2026-06-02")

    assert store.get("ip:hashed-a:2026-06-02") == 2
    assert store.get("device:hashed-b:2026-06-02") == 1
    assert store.get("source:unseen:2026-06-02") == 0


def test_snapshot_returns_frozen_copy() -> None:
    store = InMemoryRateLimitCounterStore()
    store.increment("k1")
    store.increment("k1")
    store.increment("k2")

    snap = store.snapshot()
    assert dict(snap) == {"k1": 2, "k2": 1}

    # Snapshot must not alias the internal state.
    snap_dict = dict(snap)
    snap_dict["k1"] = 999
    assert store.get("k1") == 2


def test_invalid_key_raises_unavailable() -> None:
    store = InMemoryRateLimitCounterStore()
    with pytest.raises(RateLimitCounterUnavailable):
        store.get("")
    with pytest.raises(RateLimitCounterUnavailable):
        store.increment("")
    with pytest.raises(RateLimitCounterUnavailable):
        store.get(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Behavior — UnavailableRateLimitCounterStore.
# ---------------------------------------------------------------------------


def test_unavailable_store_get_raises_readable_exception() -> None:
    store = UnavailableRateLimitCounterStore("redis socket closed")
    with pytest.raises(RateLimitCounterUnavailable) as excinfo:
        store.get("any-key")
    assert "redis socket closed" in str(excinfo.value)


def test_unavailable_store_increment_raises_readable_exception() -> None:
    store = UnavailableRateLimitCounterStore()
    with pytest.raises(RateLimitCounterUnavailable) as excinfo:
        store.increment("any-key")
    assert "unavailable" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Adapter wiring — structural compatibility with the backend adapter.
# ---------------------------------------------------------------------------


def _hash_token(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _frozen_now() -> datetime:
    return _FROZEN_NOW


def _passing_probe(upload: UploadFacts) -> ProbeResult:
    return ProbeResult(
        duration_seconds=upload.duration_seconds,
        source_hash=upload.source_hash,
        media_type=f"video/{Path(upload.file_name).suffix.lstrip('.').lower()}",
        audio_present=True,
        audio_quality_score=0.9,
        teaser_candidate_range=(0.0, min(upload.duration_seconds, 180.0)),
        failure_reason=None,
    )


def _passing_compliance(_probe: ProbeResult) -> ComplianceResult:
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        reason="prefilter ok; teaser ASR ok; LLM compliance ok",
        audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
        blocked_media_retained=False,
    )


def _make_request(*, source_hash_suffix: str = "default") -> RequestFacts:
    return RequestFacts(
        raw_session_id=f"anon-session-{source_hash_suffix}",
        raw_ip="203.0.113.42",
        raw_device_cookie=f"device-cookie-{source_hash_suffix}",
        source_type=SourceType.LOCAL_UPLOAD,
        is_free_user=False,
        youtube_url=None,
        day_key="2026-06-02",
    )


def _make_upload(temp_upload_dir: Path, *, source_hash: str) -> UploadFacts:
    stored = temp_upload_dir / f"{source_hash}.mp4"
    stored.write_bytes(b"apf2c3-fake-bytes")
    return UploadFacts(
        file_name=f"{source_hash}.mp4",
        byte_length=4 * 1024 * 1024,
        duration_seconds=120.0,
        source_hash=source_hash,
        stored_path=stored,
        is_chunked=False,
    )


@pytest.fixture
def temp_upload_dir(tmp_path: Path) -> Path:
    target = tmp_path / "apf2c3_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def config(temp_upload_dir: Path) -> IntakeConfig:
    return IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=True,
    )


def test_adapter_with_in_memory_store_hits_per_source_hash_cap(
    config: IntakeConfig, temp_upload_dir: Path
) -> None:
    store = InMemoryRateLimitCounterStore()
    adapter = AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )

    request = _make_request(source_hash_suffix="rl")
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_rl_apf2c3")

    first = adapter.handle_intake(request, upload)
    second = adapter.handle_intake(request, upload)

    assert first.status is PreviewStatus.READY_FOR_MODE
    assert second.status is PreviewStatus.RATE_LIMITED
    # Default per-source-hash cap = 1, so the source-keyed counter is
    # the one that trips on the second attempt.
    assert "source:src_hash_rl_apf2c3" in second.status_reason
    # In-memory store must have recorded the first intake's counters.
    snap = dict(store.snapshot())
    assert snap.get("source:src_hash_rl_apf2c3:2026-06-02") == 1
    assert snap.get("global:2026-06-02") == 1


def test_adapter_with_unavailable_store_fails_closed_before_probe(
    config: IntakeConfig, temp_upload_dir: Path
) -> None:
    probe_calls: list[UploadFacts] = []
    compliance_calls: list[ProbeResult] = []

    def spy_probe(upload: UploadFacts) -> ProbeResult:
        probe_calls.append(upload)
        return _passing_probe(upload)

    def spy_compliance(probe: ProbeResult) -> ComplianceResult:
        compliance_calls.append(probe)
        return _passing_compliance(probe)

    adapter = AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=UnavailableRateLimitCounterStore("redis down"),
        probe_fn=spy_probe,
        compliance_fn=spy_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )

    request = _make_request(source_hash_suffix="down")
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_unavail_apf2c3")

    record = adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "rate-limit error (fail closed)" in record.status_reason
    # Fail-closed must short-circuit before probe / compliance run.
    assert probe_calls == []
    assert compliance_calls == []


# ---------------------------------------------------------------------------
# Import guard — module only imports stdlib.
# ---------------------------------------------------------------------------


def test_module_import_hygiene() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_top_level = {
        "gateway",
        "frontend",
        "frontend_next",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "httpx",
        "aiohttp",
        "boto3",
        "boto",
        "subprocess",
        "yt_dlp",
        "pytubefix",
        "pytube",
        "ffmpeg",
        "psycopg2",
        "psycopg",
        "sqlalchemy",
        "redis",
        "dotenv",
        # Filesystem stdlib also forbidden — module is meant to be RAM-only.
        "pathlib",
        "os",
        "shutil",
        "tempfile",
        "io",
    }
    forbidden_src_prefixes = (
        "src.pipeline",
        "src.modules",
        "src.services.jobs",
        "src.services.tts",
        "src.services.voice_clone",
        "src.services.tts_provider",
        "src.services.tts_service",
        "src.services.usage_meter",
        "src.services.content_compliance",
        "src.services.voice_registry",
        "src.services.voice",
        "src.services.gemini",
        "src.services.llm",
        "src.services.assemblyai",
        "src.services.whisper_align",
        "src.services.mainland_worker",
        "src.services.express",
        # The wrapper must not depend on the adapter — dependency
        # direction is wrapper ← adapter (structural).
        "src.services.anonymous_preview_backend_adapter",
        "src.services.anonymous_preview_intake",
        "src.services.anonymous_preview_storage_health",
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                root = name.split(".", 1)[0]
                if root in forbidden_top_level:
                    offenders.append(f"import {name}")
                elif name.startswith(forbidden_src_prefixes):
                    offenders.append(f"import {name}")
                elif root == "src":
                    offenders.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_top_level:
                offenders.append(f"from {module} import ...")
            elif module.startswith(forbidden_src_prefixes):
                offenders.append(f"from {module} import ...")
            elif root == "src":
                offenders.append(f"from {module} import ...")

    assert offenders == [], (
        f"rate-limit module imports forbidden modules: {offenders}"
    )


# ---------------------------------------------------------------------------
# AST guard — no filesystem / subprocess / network / provider calls.
# ---------------------------------------------------------------------------


_FORBIDDEN_ATTR_NAMES = {
    # Path / filesystem
    "exists",
    "is_file",
    "is_dir",
    "is_symlink",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "mkdir",
    "rmdir",
    "stat",
    "lstat",
    "unlink",
    "rename",
    "replace",
    "chmod",
    "touch",
    "iterdir",
    "glob",
    "rglob",
    # Subprocess / shell
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "system",
    "spawn",
    # Network / HTTP
    "request",
    "urlopen",
    "post",
    "put",
    "delete",
    "head",
    "patch",
    "send",
    "connect",
}


_FORBIDDEN_CALL_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "input",
}


def test_module_has_no_forbidden_calls() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
                offenders.append(f"{func.id}() at line {node.lineno}")
            elif isinstance(func, ast.Attribute):
                attr = func.attr
                # Allow ``self._counts.get(...)`` / ``self._lock`` etc.
                # — those are dict / threading.Lock attribute accesses,
                # not filesystem / network calls. ``dict.get`` is the
                # one accidental collision with the forbidden HTTP
                # ``.get`` attr, so we whitelist it explicitly when the
                # receiver is ``self._counts``.
                root = func.value
                if (
                    isinstance(root, ast.Attribute)
                    and isinstance(root.value, ast.Name)
                    and root.value.id == "self"
                ):
                    continue
                if attr in _FORBIDDEN_ATTR_NAMES:
                    offenders.append(f".{attr}() at line {node.lineno}")

    assert offenders == [], (
        f"rate-limit module performs forbidden calls: {offenders}"
    )


def test_module_does_not_reference_provider_or_infra_substrings() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8").lower()
    forbidden_substrings = (
        "preview_url",
        "download_url",
        "preview_artifact",
        "clone_voice",
        "voice_clone",
        "pricing_quote",
        "payment_token",
        "credit_reservation",
        "minimax",
        "cosyvoice",
        "volcengine",
        "assemblyai",
        "gemini",
        "deepseek",
        "yt_dlp",
        "pytubefix",
        "ffprobe",
        "ffmpeg",
        "boto3",
        "psycopg",
        "sqlalchemy",
        "requests.get",
        "requests.post",
        "urllib.request",
        "subprocess",
    )
    offenders = [s for s in forbidden_substrings if s in source]
    assert offenders == [], (
        f"rate-limit module references forbidden identifiers: {offenders}"
    )


# ---------------------------------------------------------------------------
# Test-self guard — no skip / xfail markers anywhere in this file.
# ---------------------------------------------------------------------------


def test_test_file_uses_no_skip_or_xfail() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    forbidden = {"skip", "skipif", "xfail"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            if isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
                offenders.append(f"pytest.mark.{node.attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "pytest":
                offenders.append(f"pytest.{node.attr}")
    assert offenders == [], f"test file uses forbidden markers: {offenders}"
