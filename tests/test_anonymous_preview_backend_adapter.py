"""APF2c-1 backend adapter tests.

Exercises ``src.services.anonymous_preview_backend_adapter`` against
fake dependencies only. No real backend, gateway, frontend, upload,
probe, compliance, preview media, clone provider, pricing, payment,
migration or deployment code is touched.

Design source of truth:
``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.

Test surface:

* behavior contract — happy path, every fail-closed branch, status-only
  ``PreviewRecord`` rendering, YouTube rejection for anonymous/free;
* import guard — the production adapter must only import the stdlib and
  ``src.services.anonymous_preview_intake``;
* AST guard — the production adapter must not call filesystem /
  subprocess / network / provider APIs.

File I/O is constrained to the ``tmp_path`` fixture (used by the fake
counter store and to land fake upload bytes during tests). No ``skip``
or ``xfail`` markers are used.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

import pytest

from src.services.anonymous_preview_backend_adapter import (
    AnonymousPreviewBackendAdapter,
    RequestFacts,
    UploadFacts,
)
from src.services.anonymous_preview_intake import (
    DEFAULT_PREVIEW_RECORD_TTL_SECONDS,
    FORBIDDEN_PREVIEW_RECORD_FIELDS,
    SHANGHAI,
    ComplianceResult,
    ComplianceStatus,
    IntakeConfig,
    PreviewRecord,
    PreviewStatus,
    ProbeResult,
    SourceType,
)


_FROZEN_NOW = datetime(2026, 6, 2, 20, 36, 0, tzinfo=SHANGHAI)

_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "services"
    / "anonymous_preview_backend_adapter.py"
)


# ---------------------------------------------------------------------------
# Fake supporting infrastructure.
# ---------------------------------------------------------------------------


class FakeRateLimitUnavailable(Exception):
    """Raised by the fake counter store when its backing file is unusable."""


class FakeCounterStore:
    """JSON-backed counter store living under ``tmp_path``.

    Fail-closed when the path is None, the parent directory is missing,
    or the file is corrupt. Real production stores (Redis / DB) are
    expected to surface the same fail-closed semantics through their
    own exception types.
    """

    def __init__(self, path: Optional[Path]):
        self._path = path

    def _load(self) -> MutableMapping[str, int]:
        if self._path is None:
            raise FakeRateLimitUnavailable("counter store path is not configured")
        if not self._path.parent.exists():
            raise FakeRateLimitUnavailable(
                f"counter store parent directory missing: {self._path.parent}"
            )
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise FakeRateLimitUnavailable(
                f"counter store unreadable: {exc}"
            ) from exc

    def _save(self, data: Mapping[str, int]) -> None:
        if self._path is None:
            raise FakeRateLimitUnavailable("counter store path is not configured")
        try:
            self._path.write_text(json.dumps(dict(data)), encoding="utf-8")
        except OSError as exc:
            raise FakeRateLimitUnavailable(
                f"counter store unwritable: {exc}"
            ) from exc

    def get(self, key: str) -> int:
        return int(self._load().get(key, 0))

    def increment(self, key: str) -> int:
        data = self._load()
        data[key] = int(data.get(key, 0)) + 1
        self._save(data)
        return data[key]


def _hash_token(prefix: str, value: str) -> str:
    """Deterministic sha256-based hasher. Production code is expected to
    use a server-side keyed hash, but the boundary is the same: the
    adapter only sees raw values to feed into the hasher and never
    leaks them to ``PreviewRecord``.
    """

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


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_upload_dir(tmp_path: Path) -> Path:
    target = tmp_path / "apf2c_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def counter_path(tmp_path: Path) -> Path:
    return tmp_path / "apf2c_counters.json"


@pytest.fixture
def counter_store(counter_path: Path) -> FakeCounterStore:
    return FakeCounterStore(counter_path)


@pytest.fixture
def config(temp_upload_dir: Path) -> IntakeConfig:
    return IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=True,
    )


@pytest.fixture
def adapter(
    config: IntakeConfig, counter_store: FakeCounterStore
) -> AnonymousPreviewBackendAdapter:
    return AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )


def _make_request(
    *,
    source_type: SourceType = SourceType.LOCAL_UPLOAD,
    is_free_user: bool = False,
    youtube_url: Optional[str] = None,
    raw_session_id: str = "anon-session-apf2c",
    raw_ip: str = "203.0.113.42",
    raw_device_cookie: str = "device-cookie-apf2c",
    day_key: str = "2026-06-02",
) -> RequestFacts:
    return RequestFacts(
        raw_session_id=raw_session_id,
        raw_ip=raw_ip,
        raw_device_cookie=raw_device_cookie,
        source_type=source_type,
        is_free_user=is_free_user,
        youtube_url=youtube_url,
        day_key=day_key,
    )


def _make_upload(
    temp_upload_dir: Path,
    *,
    file_name: str = "clip.mp4",
    byte_length: int = 4 * 1024 * 1024,
    duration_seconds: float = 120.0,
    source_hash: str = "src_hash_apf2c_default",
    is_chunked: bool = False,
) -> UploadFacts:
    # The adapter never opens this file; we drop a tiny payload purely
    # so test-local debugging sees a real path on disk.
    stored = temp_upload_dir / file_name
    stored.write_bytes(b"apf2c-fake-bytes")
    return UploadFacts(
        file_name=file_name,
        byte_length=byte_length,
        duration_seconds=duration_seconds,
        source_hash=source_hash,
        stored_path=stored,
        is_chunked=is_chunked,
    )


# ---------------------------------------------------------------------------
# Behavior contract — happy path.
# ---------------------------------------------------------------------------


def test_local_upload_happy_path_returns_ready_for_mode(
    adapter, temp_upload_dir
):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_happy")

    record = adapter.handle_intake(request, upload)

    assert isinstance(record, PreviewRecord)
    assert record.status is PreviewStatus.READY_FOR_MODE
    assert record.source_type is SourceType.LOCAL_UPLOAD
    assert record.source_hash == "src_hash_happy"
    assert record.upload_hash == "src_hash_happy"
    assert record.compliance_status is ComplianceStatus.PASS
    # Raw session id must not leak through — only a hashed token does.
    assert "anon-session-apf2c" not in record.session_id_hash
    assert record.session_id_hash.startswith("sess_")
    # TTL exactly 24h per the pinned default.
    assert record.expires_at - record.created_at == timedelta(
        seconds=DEFAULT_PREVIEW_RECORD_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# Behavior contract — IntakeRejected translated to status-only record.
# ---------------------------------------------------------------------------


def test_compliance_block_returns_status_only_rejected(adapter, temp_upload_dir):
    def blocking_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="prohibited content",
            audit_metadata={"matched_rule": "demo_rule"},
            blocked_media_retained=False,
        )

    blocked = replace(adapter, compliance_fn=blocking_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_blocked")

    record = blocked.handle_intake(request, upload)

    assert isinstance(record, PreviewRecord)
    assert record.status is PreviewStatus.REJECTED
    assert "compliance block" in record.status_reason


def test_compliance_needs_review_returns_soft_rejected(
    adapter, temp_upload_dir
):
    def manual_review_compliance(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.NEEDS_MANUAL_REVIEW,
            reason="LLM unsure",
            audit_metadata={"layers": ("local_prefilter", "asr_teaser", "llm")},
            blocked_media_retained=False,
        )

    soft = replace(adapter, compliance_fn=manual_review_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_soft")

    record = soft.handle_intake(request, upload)

    assert record.status is PreviewStatus.SOFT_REJECTED


def test_compliance_with_retained_blocked_media_fails_closed(
    adapter, temp_upload_dir
):
    def retained_blocked(_probe: ProbeResult) -> ComplianceResult:
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="block but bytes retained",
            audit_metadata={},
            blocked_media_retained=True,
        )

    bad = replace(adapter, compliance_fn=retained_blocked)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_retained")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "must not be retained" in record.status_reason


def test_probe_failure_translated_to_failed(adapter, temp_upload_dir):
    def failing_probe(upload: UploadFacts) -> ProbeResult:
        return ProbeResult(
            duration_seconds=0,
            source_hash=upload.source_hash,
            media_type="",
            audio_present=False,
            audio_quality_score=0,
            teaser_candidate_range=(0, 0),
            failure_reason="ffprobe returned no streams",
        )

    failing = replace(adapter, probe_fn=failing_probe)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_probe_fail")

    record = failing.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "probe failure" in record.status_reason


def test_probe_exception_fails_closed_no_silent_fallback(
    adapter, temp_upload_dir
):
    def crashing_probe(_upload: UploadFacts) -> ProbeResult:
        raise RuntimeError("ffmpeg segfaulted")

    crashed = replace(adapter, probe_fn=crashing_probe)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_probe_crash")

    record = crashed.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "probe error (fail closed)" in record.status_reason


def test_compliance_exception_fails_closed_no_silent_fallback(
    adapter, temp_upload_dir
):
    def crashing_compliance(_probe: ProbeResult) -> ComplianceResult:
        raise TimeoutError("LLM compliance timed out")

    crashed = replace(adapter, compliance_fn=crashing_compliance)
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_comp_crash")

    record = crashed.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "compliance error (fail closed)" in record.status_reason


def test_invalid_upload_extension_returns_rejected(adapter, temp_upload_dir):
    request = _make_request()
    upload = _make_upload(
        temp_upload_dir,
        file_name="clip.exe",
        source_hash="src_hash_bad_ext",
    )

    record = adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.REJECTED
    assert "extension" in record.status_reason


def test_chunked_upload_rejected_when_single_request_only(
    adapter, temp_upload_dir
):
    request = _make_request()
    upload = _make_upload(
        temp_upload_dir,
        is_chunked=True,
        source_hash="src_hash_chunked",
    )

    record = adapter.handle_intake(request, upload)

    assert record.status is PreviewStatus.REJECTED
    assert "chunked" in record.status_reason


# ---------------------------------------------------------------------------
# Behavior contract — fail-closed on missing config / storage / counter.
# ---------------------------------------------------------------------------


def test_missing_config_fails_closed(counter_store, temp_upload_dir):
    bad = AnonymousPreviewBackendAdapter(
        config=None,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_config")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "IntakeConfig is missing" in record.status_reason


def test_missing_temp_upload_dir_fails_closed(counter_store, temp_upload_dir):
    bad_config = IntakeConfig(
        temp_upload_dir=None,
        temp_storage_available=True,
    )
    bad = AnonymousPreviewBackendAdapter(
        config=bad_config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_dir")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "temp_upload_dir is not configured" in record.status_reason


def test_temp_storage_unavailable_fails_closed(counter_store, temp_upload_dir):
    bad_config = IntakeConfig(
        temp_upload_dir=temp_upload_dir,
        temp_storage_available=False,
    )
    bad = AnonymousPreviewBackendAdapter(
        config=bad_config,
        counter_store=counter_store,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_storage_down")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "temp_storage_available is False" in record.status_reason


def test_counter_store_none_fails_closed_before_probe(config, temp_upload_dir):
    probe_calls: list[UploadFacts] = []
    compliance_calls: list[ProbeResult] = []

    def spy_probe(upload: UploadFacts) -> ProbeResult:
        probe_calls.append(upload)
        return _passing_probe(upload)

    def spy_compliance(probe: ProbeResult) -> ComplianceResult:
        compliance_calls.append(probe)
        return _passing_compliance(probe)

    bad = AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=None,
        probe_fn=spy_probe,
        compliance_fn=spy_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_counter")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "counter store unavailable" in record.status_reason
    # Fail closed must skip probe and compliance entirely.
    assert probe_calls == []
    assert compliance_calls == []


def test_counter_store_unreadable_fails_closed(config, temp_upload_dir, tmp_path):
    broken = FakeCounterStore(tmp_path / "missing_parent" / "counters.json")
    bad = AnonymousPreviewBackendAdapter(
        config=config,
        counter_store=broken,
        probe_fn=_passing_probe,
        compliance_fn=_passing_compliance,
        hasher=_hash_token,
        now_fn=_frozen_now,
    )
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_counter_broken")

    record = bad.handle_intake(request, upload)

    assert record.status is PreviewStatus.FAILED
    assert "rate-limit error (fail closed)" in record.status_reason


def test_rate_limit_overflow_returns_rate_limited(adapter, temp_upload_dir):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_rate_overflow")

    first = adapter.handle_intake(request, upload)
    second = adapter.handle_intake(request, upload)

    assert first.status is PreviewStatus.READY_FOR_MODE
    assert second.status is PreviewStatus.RATE_LIMITED
    assert "source:src_hash_rate_overflow" in second.status_reason


# ---------------------------------------------------------------------------
# Behavior contract — YouTube fails closed for anonymous and free.
# ---------------------------------------------------------------------------


def test_youtube_anonymous_rejected_no_upload_consumed(adapter):
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=False,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    assert record.status is PreviewStatus.REJECTED
    assert "youtube_url" in record.status_reason


def test_youtube_free_rejected(adapter):
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=True,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    assert record.status is PreviewStatus.REJECTED
    assert "youtube_url" in record.status_reason


def test_local_upload_with_missing_upload_facts_fails_closed(adapter):
    request = _make_request(source_type=SourceType.LOCAL_UPLOAD)

    record = adapter.handle_intake(request, upload=None)

    assert record.status is PreviewStatus.FAILED
    assert "upload facts missing" in record.status_reason


# ---------------------------------------------------------------------------
# Behavior contract — status-only PreviewRecord (no forbidden fields).
# ---------------------------------------------------------------------------


def test_happy_path_record_omits_forbidden_fields(adapter, temp_upload_dir):
    request = _make_request()
    upload = _make_upload(temp_upload_dir, source_hash="src_hash_no_forbidden")

    record = adapter.handle_intake(request, upload)

    fields = set(record.__dict__)
    assert FORBIDDEN_PREVIEW_RECORD_FIELDS.isdisjoint(fields)
    assert record.selected_mode_placeholder is None
    assert record.recommended_mode_placeholder is None
    assert record.claim_token_placeholder is None


def test_failure_record_omits_forbidden_fields(adapter):
    request = _make_request(
        source_type=SourceType.YOUTUBE_URL,
        is_free_user=False,
        youtube_url="https://example.invalid/anything",
    )

    record = adapter.handle_intake(request, upload=None)

    fields = set(record.__dict__)
    assert FORBIDDEN_PREVIEW_RECORD_FIELDS.isdisjoint(fields)


# ---------------------------------------------------------------------------
# Import guard — adapter module only imports stdlib + intake contract.
# ---------------------------------------------------------------------------


def test_adapter_module_import_hygiene():
    source = _ADAPTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    allowed_src_prefix = "src.services.anonymous_preview_intake"
    forbidden_top_level = {
        "gateway",
        "frontend",
        "frontend_next",
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "httpx",
        "boto3",
        "aiohttp",
        "subprocess",
        "yt_dlp",
        "pytubefix",
        "pytube",
        "ffmpeg",
        "psycopg2",
        "psycopg",
        "sqlalchemy",
        "redis",
        "boto",
        "dotenv",
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
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                root = name.split(".", 1)[0]
                if root in forbidden_top_level:
                    offenders.append(f"import {name}")
                elif root == "src" and not name.startswith(allowed_src_prefix):
                    offenders.append(f"import {name}")
                elif name.startswith(forbidden_src_prefixes):
                    offenders.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in forbidden_top_level:
                offenders.append(f"from {module} import ...")
            elif root == "src" and not module.startswith(allowed_src_prefix):
                offenders.append(f"from {module} import ...")
            elif module.startswith(forbidden_src_prefixes):
                offenders.append(f"from {module} import ...")

    assert offenders == [], f"adapter module imports forbidden modules: {offenders}"


# ---------------------------------------------------------------------------
# AST guard — adapter module performs no filesystem / subprocess / network /
# provider calls.
# ---------------------------------------------------------------------------


_FORBIDDEN_ATTR_NAMES = {
    # Path / filesystem
    "exists",
    "is_file",
    "is_dir",
    "is_symlink",
    "is_socket",
    "is_fifo",
    "is_block_device",
    "is_char_device",
    "is_mount",
    "is_absolute",
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
    "symlink_to",
    "iterdir",
    "glob",
    "rglob",
    "walk",
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
    "get",
    "post",
    "put",
    "delete",
    "head",
    "patch",
    "send",
    "connect",
}

# Allow ``CounterStore.get`` / ``CounterStore.increment`` and other
# injected-dependency calls on ``self.counter_store`` / ``self.probe_fn``
# / ``self.compliance_fn`` / ``self.hasher`` / ``self.now_fn``. Those are
# protocol indirections, not direct filesystem / network access.
_FORBIDDEN_ATTR_NAMES_ON_SELF = set()  # all `self.<attr>` calls are allowed


_FORBIDDEN_CALL_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "input",
}


def test_adapter_module_has_no_forbidden_calls():
    source = _ADAPTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Bare name calls — e.g. open(...), input(...).
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
                offenders.append(f"{func.id}() at line {node.lineno}")
            # Attribute calls — e.g. path.read_text(), subprocess.run().
            elif isinstance(func, ast.Attribute):
                attr = func.attr
                # Allow ``self.<dep>.<method>()`` — injected dependency
                # indirections are intentional.
                root = func.value
                if isinstance(root, ast.Attribute) and (
                    isinstance(root.value, ast.Name) and root.value.id == "self"
                ):
                    continue
                if attr in _FORBIDDEN_ATTR_NAMES:
                    offenders.append(f".{attr}() at line {node.lineno}")

    assert offenders == [], (
        f"adapter module performs forbidden calls: {offenders}"
    )


def test_adapter_module_does_not_reference_provider_modules():
    """String-level guard: ensure no provider / preview / clone /
    pricing / payment identifier names leak into the adapter source. The
    import guard covers ``import`` statements; this guard covers stray
    string references (e.g. dynamic imports, log lines)."""

    source = _ADAPTER_PATH.read_text(encoding="utf-8")
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
    )
    offenders = [s for s in forbidden_substrings if s in source.lower()]
    assert offenders == [], (
        f"adapter module references forbidden identifiers: {offenders}"
    )


# ---------------------------------------------------------------------------
# Test-self guards — no skip / xfail markers, AST scan of this file.
# ---------------------------------------------------------------------------


def test_test_file_uses_no_skip_or_xfail():
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
