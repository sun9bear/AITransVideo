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
    """Cross-module contract: the event types the gateway writer accepts
    without a drift warning must match the ``services.jobs.events``
    SUPPORTED_EVENT_TYPES set for download.* events. Catches the case where
    someone adds a new download.* event type to one side without the other.
    """
    # Scrub cache — other fixtures muck with sys.modules["config"].
    for mod in ("storage", "storage.event_log"):
        sys.modules.pop(mod, None)
    event_log = importlib.import_module("storage.event_log")
    gateway_types = set(event_log._DOWNLOAD_EVENT_TYPES)

    # Pull SUPPORTED_EVENT_TYPES from the Job API side via AST — importing
    # services.jobs.events would pull pydub and defeat the point.
    events_src = (SRC_DIR / "services" / "jobs" / "events.py").read_text(encoding="utf-8")
    # Extract every download.* string literal assigned to an EVENT_TYPE_*
    # constant. Robust enough for this small, stable file.
    import re
    literals = set(re.findall(r'"(download\.[a-z0-9_.]+)"', events_src))
    job_api_types = {t for t in literals if t.startswith("download.")}

    assert gateway_types == job_api_types, (
        f"Event-type drift between gateway/storage/event_log.py and "
        f"src/services/jobs/events.py.\n"
        f"  gateway-only: {gateway_types - job_api_types}\n"
        f"  job-api-only: {job_api_types - gateway_types}"
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
