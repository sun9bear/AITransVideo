"""Phase 2 R2 download backend (plan 2026-04-23) contract tests.

Coverage (maps 1:1 to user-locked acceptance list):
  1. local mode: resolve_download_target returns None
  2. r2 HEAD hit: presign only, no upload
  3. r2 HEAD miss + local present: lazy upload then presign
  4. HEAD / upload / presign exceptions each → None
  5. R2 key carries .mp4 suffix
  6. lock path NOT in the artifact directory (lives under jobs_dir)
  7. download.redirect.r2 event written
  8. download.fallback.local event written
  9. download.local.direct event written
 10. frontend-next ships no R2 domain / bucket / presigned URL leakage

No boto3 calls are made. All R2 client helpers are monkeypatched so these
tests run in a clean CI env without AWS/R2 credentials.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

# ---- sys.path setup (mirrors tests/conftest.py pattern) ---------------------

REPO = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO / "gateway"
SRC_DIR = REPO / "src"
for _p in (str(GATEWAY_DIR), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture
def fresh_backend_router(monkeypatch, tmp_path):
    """Yield a freshly-imported ``backend_router`` with R2 enabled and a
    per-test jobs_dir. Also returns a ``FakeR2`` that the test drives.

    Fresh import avoids cross-test state in the module-level boto3 client
    cache inside ``r2_client``.
    """
    # Scrub any previous imports so monkeypatching ``settings`` and
    # ``r2_client`` isn't muddied by a stale module object.
    for mod in ("storage", "storage.r2_client", "storage.backend_router", "config"):
        sys.modules.pop(mod, None)

    import config  # noqa: F401  # re-loaded via importlib below
    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "download_redirect_backend", "r2")
    monkeypatch.setattr(config.settings, "r2_endpoint", "https://fake.r2/")
    monkeypatch.setattr(config.settings, "r2_access_key_id", "ak")
    monkeypatch.setattr(config.settings, "r2_secret_access_key", "sk")
    monkeypatch.setattr(config.settings, "r2_artifacts_bucket", "avt-artifacts")
    monkeypatch.setattr(config.settings, "r2_presigned_expires_s", 120)
    monkeypatch.setattr(config.settings, "r2_upload_timeout_s", 60)
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))

    backend_router = importlib.import_module("storage.backend_router")
    r2_client = importlib.import_module("storage.r2_client")

    fake = FakeR2()
    # Router imports r2_client lazily inside resolve_download_target; patch
    # on the module object so both direct and re-imported references see
    # the fake.
    monkeypatch.setattr(r2_client, "head_artifact", fake.head_artifact)
    monkeypatch.setattr(r2_client, "upload_artifact", fake.upload_artifact)
    monkeypatch.setattr(
        r2_client,
        "generate_presigned_download_url",
        fake.generate_presigned_download_url,
    )

    return backend_router, fake, tmp_path


class FakeR2:
    """Minimal stub for the three helpers in ``storage.r2_client``.

    Each helper records its call sequence and lets the test inject
    configurable return values / exceptions. Real boto3 is never touched.
    """

    def __init__(self) -> None:
        self.head_calls: list[str] = []
        self.upload_calls: list[tuple[Path, str]] = []
        self.presign_calls: list[tuple[str, str]] = []
        self.head_return: bool = False
        self.head_exc: Exception | None = None
        self.upload_exc: Exception | None = None
        self.presign_exc: Exception | None = None
        self.presign_url: str = "https://fake.r2/avt-artifacts/signed?x=1"

    def head_artifact(self, key: str) -> bool:
        self.head_calls.append(key)
        if self.head_exc is not None:
            raise self.head_exc
        return self.head_return

    def upload_artifact(
        self,
        local_path: Path,
        key: str,
        content_type: str = "video/mp4",
    ) -> None:
        # Plan 2026-05-07 §4.3 added the ``content_type`` kwarg so the
        # publisher can push subtitles as text/plain. Default keeps
        # legacy lazy-upload callers working unchanged.
        self.upload_calls.append((Path(local_path), key))
        if self.upload_exc is not None:
            raise self.upload_exc

    def generate_presigned_download_url(
        self,
        key: str,
        download_filename: str,
        content_type: str = "video/mp4",
    ) -> str:
        self.presign_calls.append((key, download_filename))
        if self.presign_exc is not None:
            raise self.presign_exc
        return self.presign_url


def _make_local_artifact(tmp_path: Path, name: str = "dubbed.mp4") -> Path:
    path = tmp_path / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 64)  # tiny placeholder
    return path


# ---- Case 1 -----------------------------------------------------------------


def test_local_mode_returns_none(monkeypatch, tmp_path):
    """Backend=local → router short-circuits to None, never touches r2_client."""
    for mod in ("storage", "storage.r2_client", "storage.backend_router", "config"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "download_redirect_backend", "local")
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))

    backend_router = importlib.import_module("storage.backend_router")
    local = _make_local_artifact(tmp_path)

    result = backend_router.resolve_download_target(
        job_id="job_abc",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="x.mp4",
    )
    assert result is None
    assert backend_router.is_r2_enabled() is False


# ---- Case 2 -----------------------------------------------------------------


def test_r2_head_hit_presigns_without_upload(fresh_backend_router):
    backend_router, fake, tmp_path = fresh_backend_router
    fake.head_return = True
    local = _make_local_artifact(tmp_path)

    url = backend_router.resolve_download_target(
        job_id="job_abc",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="my_video.mp4",
    )

    assert url == fake.presign_url
    assert fake.head_calls == ["jobs/job_abc/publish.dubbed_video.mp4"]
    assert fake.upload_calls == []  # no upload on HEAD hit
    assert len(fake.presign_calls) == 1
    assert fake.presign_calls[0][1] == "my_video.mp4"


# ---- Case 3 -----------------------------------------------------------------


def test_r2_head_miss_triggers_lazy_upload(fresh_backend_router):
    backend_router, fake, tmp_path = fresh_backend_router
    # First HEAD (pre-lock) → miss; second HEAD (post-lock) → miss again
    # so the code path reaches upload.
    head_sequence = iter([False, False])
    fake.head_artifact = lambda key: (fake.head_calls.append(key) or next(head_sequence))
    local = _make_local_artifact(tmp_path)

    url = backend_router.resolve_download_target(
        job_id="job_xyz",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="clip.mp4",
    )

    assert url == fake.presign_url
    assert fake.head_calls == [
        "jobs/job_xyz/publish.dubbed_video.mp4",
        "jobs/job_xyz/publish.dubbed_video.mp4",
    ]
    assert fake.upload_calls == [(local, "jobs/job_xyz/publish.dubbed_video.mp4")]
    assert len(fake.presign_calls) == 1


# ---- Case 4 -----------------------------------------------------------------


def test_head_exception_returns_none(fresh_backend_router):
    backend_router, fake, tmp_path = fresh_backend_router
    fake.head_exc = RuntimeError("network")
    local = _make_local_artifact(tmp_path)

    url = backend_router.resolve_download_target(
        job_id="job_h",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="x.mp4",
    )
    assert url is None
    assert fake.upload_calls == []
    assert fake.presign_calls == []


def test_upload_exception_returns_none(fresh_backend_router):
    backend_router, fake, tmp_path = fresh_backend_router
    # HEAD miss → triggers upload, which raises.
    fake.head_return = False
    fake.upload_exc = RuntimeError("timeout")
    local = _make_local_artifact(tmp_path)

    url = backend_router.resolve_download_target(
        job_id="job_u",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="x.mp4",
    )
    assert url is None
    assert fake.presign_calls == []


def test_presign_exception_returns_none(fresh_backend_router):
    backend_router, fake, tmp_path = fresh_backend_router
    fake.head_return = True  # object already there → straight to presign
    fake.presign_exc = RuntimeError("sig error")
    local = _make_local_artifact(tmp_path)

    url = backend_router.resolve_download_target(
        job_id="job_p",
        artifact_key="publish.dubbed_video",
        local_path=local,
        download_filename="x.mp4",
    )
    assert url is None


# ---- Case 5 -----------------------------------------------------------------


def test_r2_key_carries_mp4_suffix(fresh_backend_router):
    backend_router, _fake, tmp_path = fresh_backend_router
    local = _make_local_artifact(tmp_path, name="dubbed.mp4")
    key = backend_router.r2_key_for("job_k", "publish.dubbed_video", local_path=local)
    assert key == "jobs/job_k/publish.dubbed_video.mp4"


def test_r2_key_without_local_path_has_no_suffix(fresh_backend_router):
    """Backward contract: no local_path → bare key (callers that don't
    know the file extension ahead of time should still get a clean key)."""
    backend_router, _fake, _tmp = fresh_backend_router
    assert (
        backend_router.r2_key_for("job_k", "publish.dubbed_video")
        == "jobs/job_k/publish.dubbed_video"
    )


# ---- Case 6 -----------------------------------------------------------------


def test_lock_path_not_in_artifact_directory(fresh_backend_router):
    backend_router, _fake, tmp_path = fresh_backend_router
    # _lock_path_for_key is the internal that decides lock placement.
    lock_path = backend_router._lock_path_for_key("jobs/job_x/publish.dubbed_video.mp4")

    # Must live under the configured jobs_dir, NOT adjacent to the artifact.
    assert str(lock_path).startswith(str(tmp_path))
    assert "_r2_upload_locks" in str(lock_path)
    # Must not be under the artifact parent directory (artifacts/ in this test).
    assert "artifacts" not in lock_path.parts
    # Filename is a sha256 hex — 64 chars.
    expected = hashlib.sha256(b"jobs/job_x/publish.dubbed_video.mp4").hexdigest()
    assert lock_path.name == expected


# ---- Cases 7 / 8 / 9 --------------------------------------------------------
# These exercise the **real** production event writer
# (``gateway.storage.event_log.emit_download_event``). Earlier revisions of
# this test file re-implemented the JSONL record shape inline — which meant
# the production append path could break without any test failing. CodeX
# review (2026-04-24 [P2]) flagged this; the fix extracts the writer into
# ``event_log.py`` (pure stdlib, no fastapi) so tests can call it directly.


@pytest.fixture
def emit_helper(monkeypatch, tmp_path):
    """Yield a freshly-imported ``emit_download_event`` with jobs_dir
    monkeypatched to a per-test tmp_path. Scrubs sys.modules so a prior
    test's settings override doesn't leak in."""
    for mod in ("storage", "storage.event_log", "config"):
        sys.modules.pop(mod, None)

    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))

    event_log = importlib.import_module("storage.event_log")
    return event_log.emit_download_event, tmp_path


def test_emit_download_event_writes_redirect_r2(emit_helper):
    _assert_emit_writes_expected(
        emit_helper,
        event_type="download.redirect.r2",
        backend="r2",
        message="Download redirected to R2",
    )


def test_emit_download_event_writes_fallback_local(emit_helper):
    _assert_emit_writes_expected(
        emit_helper,
        event_type="download.fallback.local",
        backend="local",
        message="Download fell back to local source",
    )


def test_emit_download_event_writes_local_direct(emit_helper):
    _assert_emit_writes_expected(
        emit_helper,
        event_type="download.local.direct",
        backend="local",
        message="Download served from local source",
    )


def _assert_emit_writes_expected(
    emit_helper_pair,
    *,
    event_type: str,
    backend: str,
    message: str,
) -> None:
    """Call the production :func:`emit_download_event` and assert the on-disk
    line matches the ``JobEvent.to_dict()`` schema the Job API's
    ``JobStore.load_events`` consumes."""
    emit, jobs_dir = emit_helper_pair
    job_id = "job_phase2"
    events_path = jobs_dir / f"{job_id}.events.jsonl"

    emit(
        job_id,
        event_type,
        message=message,
        payload={"artifact_key": "publish.dubbed_video", "backend": backend},
    )

    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected exactly one event line, got {len(lines)}"
    back = json.loads(lines[0])
    required = {
        "job_id", "event_type", "created_at", "message",
        "stage", "status", "level", "payload",
    }
    assert required <= set(back.keys()), (
        f"event record missing required fields: {required - set(back.keys())}"
    )
    assert back["job_id"] == job_id
    assert back["event_type"] == event_type
    assert back["stage"] == "download"
    assert back["level"] == "info"
    assert back["status"] is None
    assert back["message"] == message
    assert back["payload"]["backend"] == backend
    assert back["payload"]["artifact_key"] == "publish.dubbed_video"


def test_emit_download_event_supported_types_in_sync_with_jobs_events():
    """Cross-module contract: the redirect event types the gateway writer
    accepts without a drift warning must match the
    ``services.jobs.events`` SUPPORTED_EVENT_TYPES set for both
    ``download.*`` and ``stream.*`` families. Catches the case where
    someone adds a new event type to one side without the other.

    Plan 2026-05-07 §11.3 C6 (Stage C, 2026-05-12): ``_DOWNLOAD_EVENT_TYPES``
    was extended to include ``stream.*`` keeping the same variable name
    for git-blame continuity; the assertion below covers both families.
    """
    # Scrub cache — other fixtures muck with sys.modules["config"].
    for mod in ("storage", "storage.event_log"):
        sys.modules.pop(mod, None)
    event_log = importlib.import_module("storage.event_log")
    gateway_types = set(event_log._DOWNLOAD_EVENT_TYPES)

    # Pull SUPPORTED_EVENT_TYPES from the Job API side via AST — importing
    # services.jobs.events would pull pydub and defeat the point.
    events_src = (SRC_DIR / "services" / "jobs" / "events.py").read_text(encoding="utf-8")
    # Extract every (download|stream).* string literal assigned to an
    # EVENT_TYPE_* constant. Robust enough for this small, stable file.
    import re
    literals = set(re.findall(r'"((?:download|stream)\.[a-z0-9_.]+)"', events_src))

    assert gateway_types == literals, (
        f"Event-type drift between gateway/storage/event_log.py and "
        f"src/services/jobs/events.py.\n"
        f"  gateway-only: {gateway_types - literals}\n"
        f"  job-api-only: {literals - gateway_types}"
    )


# ---- Case 10: frontend-zero-awareness of R2 --------------------------------


def test_frontend_has_no_r2_leakage():
    """AST + source scan: no frontend-next file mentions R2 domain, bucket
    name, signed-URL query params, or SigV4 algorithm. Download URLs must
    remain /job-api/jobs/{id}/download/publish.dubbed_video (handled by
    Gateway). This is the user's locked "前端零感知 R2" contract (§9).
    """
    banned = [
        "r2.cloudflarestorage",
        "avt-artifacts",
        "X-Amz-Signature",
        "X-Amz-Expires",
        "X-Amz-Algorithm",
        "AWS4-HMAC-SHA256",
    ]

    frontend_root = REPO / "frontend-next" / "src"
    assert frontend_root.is_dir(), "frontend-next/src must exist for this guard"

    offenders: list[tuple[Path, str]] = []
    for path in frontend_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in banned:
            if needle in text:
                offenders.append((path.relative_to(REPO), needle))
    assert not offenders, (
        "Frontend must stay R2-agnostic; offending references:\n"
        + "\n".join(f"  {p}: '{s}'" for p, s in offenders)
    )


# ---- Bonus guard: job_intercept wires exactly one R2 download surface ------


def test_intercept_has_single_r2_download_surface():
    """Cheap structural guard: intercept_job_subresource contains exactly
    one branch that looks at ``download/{key}`` URLs.

    Plan 2026-04-23 narrowed this to ``publish.dubbed_video`` literal.
    Plan 2026-05-07 §4.7 broadened to all downloadable keys via the
    ``_DOWNLOAD_KEY_RE`` regex match — exactly one ``download_match`` use
    site. Catches future accidental duplication.
    """
    source = (GATEWAY_DIR / "job_intercept.py").read_text(encoding="utf-8")
    matches = source.count("download_match = (")
    assert matches == 1, (
        f"Expected exactly 1 'download_match = (' in job_intercept.py, "
        f"got {matches}. The download intercept must have a single entry "
        f"point so we can reason about ordering / fallback semantics. If "
        f"you are deliberately adding a second surface, update this guard."
    )


def test_intercept_has_single_r2_stream_surface():
    """Stage C parallel guard: same single-entry rule applies to
    ``/stream/{kind}`` (plan 2026-05-07 §11.3 C3-C4). Future maintainers
    must not add a second ``stream_match`` dispatch site, or fallback
    ordering between R2 / Job API local becomes ambiguous.
    """
    source = (GATEWAY_DIR / "job_intercept.py").read_text(encoding="utf-8")
    matches = source.count("stream_match = (")
    assert matches == 1, (
        f"Expected exactly 1 'stream_match = (' in job_intercept.py, "
        f"got {matches}. See same rationale as download surface guard."
    )


def test_stream_kind_regex_only_matches_known_kinds():
    """CodeX P2 follow-up (2026-05-12): ``_STREAM_KIND_RE`` must NOT
    match arbitrary ``[a-z]+`` segments, only the canonical three.
    Unknown kinds must bypass the Gateway intercept entirely (no
    ``stream.fallback.local`` event emitted, no DB hit) so the
    rollout fallback metric stays clean.
    """
    for mod in ("storage", "storage.event_log", "config"):
        sys.modules.pop(mod, None)
    sys.modules.pop("job_intercept", None)
    job_intercept = importlib.import_module("job_intercept")
    re_obj = job_intercept._STREAM_KIND_RE
    # Allowed
    assert re_obj.match("stream/video") is not None
    assert re_obj.match("stream/audio") is not None
    assert re_obj.match("stream/poster") is not None
    # Refused — these would have matched the looser `[a-z]+` pattern
    assert re_obj.match("stream/unknown") is None
    assert re_obj.match("stream/preview-source") is None
    assert re_obj.match("stream/foo") is None
    assert re_obj.match("stream/") is None
    assert re_obj.match("stream/video/sub") is None


# ============================================================================
# Stage C tests (plan 2026-05-07 §11.3, 2026-05-12)
# Coverage: /stream/{kind} R2 redirect for video/audio/poster, with the
# same registry + service_mode allowlist semantics as /download/{key}.
# ============================================================================


class _StreamFakeJob:
    """Minimal Job stub for direct ``_resolve_r2_stream_redirect`` calls."""

    def __init__(
        self,
        *,
        job_id: str = "job_stream_test",
        service_mode: str = "studio",
        edit_generation: int = 0,
        project_dir: str = "/opt/aivideotrans/app/projects/u/job_stream_test",
        r2_artifacts: list[dict] | None = None,
        display_name: str | None = None,
        title: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.service_mode = service_mode
        self.edit_generation = edit_generation
        self.project_dir = project_dir
        self.r2_artifacts = r2_artifacts
        self.display_name = display_name
        self.title = title


class _StreamFakeDB:
    """Async-compatible ``db.execute(select(Job).where(...))`` stub."""

    def __init__(self, job_obj):
        self._job = job_obj

    async def execute(self, *args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(scalar_one_or_none=lambda: self._job)


def _stream_entry(artifact_key, *, gen=0, state="pushed", r2_key=None,
                  filename=None, content_type=None):
    d = {
        "artifact_key": artifact_key,
        "edit_generation": gen,
        "state": state,
    }
    if state in ("pushed", "already_present"):
        d["r2_key"] = r2_key or f"jobs/job_stream_test/g{gen}/{artifact_key}.bin"
        d["filename"] = filename or f"vid_{artifact_key}.bin"
        d["content_type"] = content_type or "application/octet-stream"
    return d


@pytest.fixture
def stream_fresh_modules(monkeypatch, tmp_path):
    """Same module-reload dance as the download fixture so each test
    runs against a clean ``storage`` import with monkeypatched config."""
    for mod in (
        "storage", "storage.r2_client", "storage.backend_router",
        "config",
    ):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "download_redirect_backend", "r2")
    monkeypatch.setattr(config.settings, "r2_endpoint", "https://fake.r2/")
    monkeypatch.setattr(config.settings, "r2_access_key_id", "AKIATEST")
    monkeypatch.setattr(config.settings, "r2_secret_access_key", "secrettest")
    monkeypatch.setattr(config.settings, "r2_artifacts_bucket", "avt-artifacts")
    monkeypatch.setattr(config.settings, "r2_presigned_expires_s", 120)
    monkeypatch.setattr(config.settings, "r2_upload_timeout_s", 60)
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))

    # Pre-stub both presign helpers so the test doesn't open a boto3
    # session. Stream uses the dedicated long-TTL inline-disposition
    # helper (CodeX P2 follow-up); download still uses the short-TTL
    # attachment helper. Calls list keeps a (helper_name, ...) prefix
    # so individual tests can introspect which path was taken.
    r2_client = importlib.import_module("storage.r2_client")
    calls: list[tuple] = []

    def _fake_download_presign(key, filename, content_type="video/mp4"):
        calls.append(("download", key, filename, content_type))
        return f"https://fake.r2/{key}?sig=download"

    def _fake_stream_presign(key, content_type="video/mp4", expires_s=None):
        calls.append(("stream", key, content_type, expires_s))
        return f"https://fake.r2/{key}?sig=stream"

    monkeypatch.setattr(
        r2_client, "generate_presigned_download_url", _fake_download_presign,
    )
    monkeypatch.setattr(
        r2_client, "generate_presigned_stream_url", _fake_stream_presign,
    )
    return calls


@pytest.mark.asyncio
async def test_stream_video_r2_redirect_via_registry(stream_fresh_modules):
    """Studio video stream → registry hit → STREAM presigned URL.

    CodeX P2 (2026-05-12): the call must land on
    ``generate_presigned_stream_url`` (long TTL, no Content-Disposition
    attachment) — NOT the download helper. The fixture records the
    helper name as the tuple's first element.
    """
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry(
        "publish.dubbed_video", content_type="video/mp4",
        r2_key="jobs/job_stream_test/g0/publish.dubbed_video.mp4",
        filename="my_video.mp4",
    )]
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=registry))

    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert kind == "registry"
    assert url and "sig=stream" in url, (
        f"stream redirect must call stream presign, not download "
        f"(got url={url})"
    )
    # Stream helper signature: (helper, key, content_type, expires_s)
    assert len(stream_fresh_modules) == 1
    helper, key, ct, expires = stream_fresh_modules[0]
    assert helper == "stream"
    assert key == "jobs/job_stream_test/g0/publish.dubbed_video.mp4"
    assert ct == "video/mp4"


@pytest.mark.asyncio
async def test_stream_poster_r2_redirect(stream_fresh_modules):
    """Studio poster stream → registry hit with image/jpeg content_type."""
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry(
        "publish.dubbed_video_poster", content_type="image/jpeg",
        r2_key="jobs/job_stream_test/g0/publish.dubbed_video_poster.jpg",
        filename="my_video_poster.jpg",
    )]
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=registry))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="poster")
    assert kind == "registry"
    assert url and "publish.dubbed_video_poster" in url
    helper, _key, ct, _expires = stream_fresh_modules[0]
    assert helper == "stream"
    assert ct == "image/jpeg"


@pytest.mark.asyncio
async def test_stream_audio_express_forbidden(stream_fresh_modules):
    """Express + audio: must NOT 302 to R2 (audio not in
    EXPRESS_ALLOWED_STREAM_KINDS); Gateway falls through to Job API which
    will return its own 403."""
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry("editor.dubbed_audio_complete", content_type="audio/wav")]
    db = _StreamFakeDB(_StreamFakeJob(
        service_mode="express", r2_artifacts=registry,
    ))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="audio")
    assert url is None
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_fallback_when_registry_empty(stream_fresh_modules):
    """Registry NULL → caller falls through to Job API local stream."""
    from job_intercept import _resolve_r2_stream_redirect
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=None))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert url is None
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_unknown_kind_falls_through(stream_fresh_modules):
    """Unknown /stream/foo segment doesn't try R2 at all."""
    from job_intercept import _resolve_r2_stream_redirect
    db = _StreamFakeDB(_StreamFakeJob())
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="foo")
    assert url is None
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_old_generation_entry_ignored(stream_fresh_modules):
    """Registry entry from gen 0 must NOT serve a gen-1 stream request
    (mirrors the download P1.3 / Stage A overwrite-isolation invariant)."""
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry("publish.dubbed_video", gen=0)]
    db = _StreamFakeDB(_StreamFakeJob(edit_generation=1, r2_artifacts=registry))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert url is None  # generation mismatch
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_skipped_missing_state_falls_through(stream_fresh_modules):
    """An entry in 'skipped_missing' state means R2 has nothing — caller
    falls through to Job API local (which will 404 the same way)."""
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry("publish.dubbed_video", state="skipped_missing")]
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=registry))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert url is None
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_r2_disabled_falls_through(monkeypatch, tmp_path):
    """When AVT_DOWNLOAD_REDIRECT_BACKEND != r2, stream helper short-circuits."""
    for mod in ("storage", "storage.r2_client", "storage.backend_router", "config"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "download_redirect_backend", "local")
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry("publish.dubbed_video")]
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=registry))
    url, kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert url is None
    assert kind == ""


@pytest.mark.asyncio
async def test_stream_uses_long_ttl_inline_helper_not_download_helper(
    stream_fresh_modules,
):
    """CodeX P2 invariant (2026-05-12): stream MUST NOT reuse the
    short-TTL ``attachment``-disposition download helper, or
    ``<video>`` players will hit 403 mid-playback on any clip > 120s
    and the player will try to save the file instead of play it.

    Drift guard: if a future refactor accidentally points
    ``_resolve_r2_stream_redirect`` back at
    ``generate_presigned_download_url``, this test catches it.
    """
    from job_intercept import _resolve_r2_stream_redirect
    registry = [_stream_entry("publish.dubbed_video")]
    db = _StreamFakeDB(_StreamFakeJob(r2_artifacts=registry))
    url, _kind = await _resolve_r2_stream_redirect(db, "job_stream_test", stream_kind="video")
    assert url and "sig=stream" in url
    # Negative: download helper must not have been touched
    helper_names = [c[0] for c in stream_fresh_modules]
    assert "download" not in helper_names, (
        f"stream path used download helper(s); calls={stream_fresh_modules}"
    )
    assert helper_names == ["stream"]


def test_r2_client_stream_presign_uses_no_content_disposition(monkeypatch, tmp_path):
    """Unit-level: ``generate_presigned_stream_url`` must NOT pass
    ``ResponseContentDisposition`` to boto3. Doing so would tell R2
    to add an ``attachment; filename=...`` response header, which
    breaks in-browser playback (browser tries to save instead of
    play).
    """
    for mod in ("storage", "storage.r2_client", "config"):
        sys.modules.pop(mod, None)
    config = importlib.import_module("config")
    monkeypatch.setattr(config.settings, "download_redirect_backend", "r2")
    monkeypatch.setattr(config.settings, "r2_endpoint", "https://fake.r2/")
    monkeypatch.setattr(config.settings, "r2_access_key_id", "ak")
    monkeypatch.setattr(config.settings, "r2_secret_access_key", "sk")
    monkeypatch.setattr(config.settings, "r2_artifacts_bucket", "avt-artifacts")
    monkeypatch.setattr(config.settings, "r2_stream_presigned_expires_s", 1800)
    monkeypatch.setattr(config.settings, "r2_upload_timeout_s", 60)
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path))

    r2_client = importlib.import_module("storage.r2_client")
    # Capture the call so we can inspect Params without going to boto3
    captured: dict = {}

    class _FakeClient:
        def generate_presigned_url(self, op, *, Params, ExpiresIn):
            captured["op"] = op
            captured["Params"] = Params
            captured["ExpiresIn"] = ExpiresIn
            return f"https://r2/{Params['Key']}"

    monkeypatch.setattr(r2_client, "_get_client", lambda: _FakeClient())

    url = r2_client.generate_presigned_stream_url(
        "jobs/abc/g0/publish.dubbed_video.mp4", content_type="video/mp4",
    )
    assert url
    assert captured["op"] == "get_object"
    assert "ResponseContentDisposition" not in captured["Params"], (
        "stream presign must not set Content-Disposition (would force "
        "browser to download instead of play)"
    )
    assert captured["Params"]["ResponseContentType"] == "video/mp4"
    assert captured["ExpiresIn"] == 1800


def test_stream_event_types_in_sync():
    """Plan 2026-05-07 §11.3 C6: gateway/storage/event_log.py
    ``_DOWNLOAD_EVENT_TYPES`` must include all 4 stream.* types and
    services.jobs.events.SUPPORTED_EVENT_TYPES must agree. Drift between
    the two surfaces would let stream events fail silently in the JSONL
    emit path or violate the Job API event contract.
    """
    from storage.event_log import _DOWNLOAD_EVENT_TYPES
    from services.jobs import events as _events
    expected_stream = {
        "stream.redirect.r2",
        "stream.redirect.r2_registry",
        "stream.fallback.local",
        "stream.local.direct",
    }
    assert expected_stream.issubset(_DOWNLOAD_EVENT_TYPES)
    assert expected_stream.issubset(_events.SUPPORTED_EVENT_TYPES)
