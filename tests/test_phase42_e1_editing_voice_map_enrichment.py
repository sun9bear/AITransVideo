"""Phase 4.2 E.1 PR #15 P1 三轮 fix (Codex 2026-05-27) — gateway intercept
enrichment for editing/voice-map.

Background: the frontend ``setVoiceOverride`` only sends ``{segment_id,
provider, voice_id, tts_model_key?, voice_reuse?}``. Without gateway-side
enrichment, CosyVoice clone voice overrides hit the upstream Job API
without ``requires_worker`` / ``worker_target_model`` and the persisted
voice_map.json entry silently downgrades to legacy CosyVoice. This
suite locks the enrichment contract.

The enrichment lives in
``gateway/job_intercept.py::_enrich_editing_voice_map_routing``
(an isolated pure-async helper) so the tests can drive it directly with
a stub AsyncSession instead of spinning up the whole gateway app.

6 contracts locked:

1. Clone voice (user_voices match) → inject `requires_worker=True` +
   `worker_target_model`
2. Builtin public CosyVoice voice → no inject, pass-through
3. Cosyvoice voice with NO user_voices match AND not in public catalog
   → 400 ``voice_clone_metadata_missing`` fail-closed
4. Client-supplied ``requires_worker`` / ``worker_target_model`` are
   ALWAYS stripped before the lookup runs (defense in depth — server
   is the only authority)
5. Non-cosyvoice paths (MiniMax / VolcEngine) are pass-through (no
   lookup, no inject, just stripped)
6. DB lookup transient failure on cosyvoice path → 503
   ``voice_clone_routing_lookup_failed``

Plus 3 structural / cross-layer guards:

7. ``src/pipeline`` still does NOT import gateway (F.3 regression check)
8. ``src/services/jobs/`` (pipeline subprocess) does NOT do its own DB
   lookup against ``user_voices`` (gateway is the only authority)
9. Action ``clear`` is exempt — no enrichment needed for delete
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "gateway", REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Stub AsyncSession + dependency hijacks
# ---------------------------------------------------------------------------


class _StubSession:
    """No-op AsyncSession stand-in. The enrichment helper only passes us
    to ``lookup_clone_voice_routing_metadata`` and
    ``_fetch_cosyvoice_public_voice_ids``; we monkeypatch BOTH of those
    so the session never actually gets used."""

    pass


@pytest.fixture
def enrich_with_stubs(monkeypatch):
    """Returns a callable ``run_enrich(payload, *, user_id, lookup_map,
    public_ids, lookup_raises, public_raises)`` that exercises the
    enrichment helper end-to-end with controllable monkey-patched
    lookups.

    - ``lookup_map``: dict[voice_id, {requires_worker, worker_target_model}]
      to return from ``lookup_clone_voice_routing_metadata``
    - ``public_ids``: set of voice_ids that are public catalog presets
    - ``lookup_raises``: exception class to raise from the user_voices
      lookup (sim transient DB failure)
    - ``public_raises``: exception class to raise from public catalog
      lookup
    """
    import job_intercept
    import user_voice_service

    async def _make_call(
        payload: dict[str, Any],
        *,
        user_id: object = "u-test",
        lookup_map: dict[str, dict[str, Any]] | None = None,
        public_ids: set[str] | None = None,
        lookup_raises: type[BaseException] | None = None,
        public_raises: type[BaseException] | None = None,
    ) -> tuple[dict | None, dict | None]:
        async def fake_lookup(db, *, user_id, voice_ids):
            if lookup_raises:
                raise lookup_raises("simulated transient DB failure")
            return dict(lookup_map or {})

        async def fake_public(db):
            if public_raises:
                raise public_raises("simulated catalog failure")
            return set(public_ids or [])

        monkeypatch.setattr(
            user_voice_service,
            "lookup_clone_voice_routing_metadata",
            fake_lookup,
        )
        monkeypatch.setattr(
            job_intercept,
            "_fetch_cosyvoice_public_voice_ids",
            fake_public,
        )

        return await job_intercept._enrich_editing_voice_map_routing(
            _StubSession(), user_id=user_id, payload=payload,
        )

    def run(payload, **kwargs):
        return asyncio.run(_make_call(payload, **kwargs))

    return run


# ---------------------------------------------------------------------------
# Contract 1: clone voice → inject routing
# ---------------------------------------------------------------------------


def test_enrich_voice_map_injects_routing_for_clone_voice(enrich_with_stubs):
    """**Contract 1**: voice_id is in user_voices (user-owned clone) →
    enriched payload carries ``requires_worker=True`` and
    ``worker_target_model``.
    """
    payload = {
        "segment_id": "segment_001",
        "provider": "cosyvoice",
        "voice_id": "voice-cosy-v3-flash-mine-uuid",
    }
    enriched, error = enrich_with_stubs(
        payload,
        user_id="u-1",
        lookup_map={
            "voice-cosy-v3-flash-mine-uuid": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            },
        },
    )
    assert error is None
    assert enriched is not None
    assert enriched["requires_worker"] is True
    assert enriched["worker_target_model"] == "cosyvoice-v3.5-flash"
    assert enriched["provider"] == "cosyvoice"
    # Other fields pass through
    assert enriched["voice_id"] == "voice-cosy-v3-flash-mine-uuid"
    assert enriched["segment_id"] == "segment_001"


# ---------------------------------------------------------------------------
# Contract 2: builtin CosyVoice public preset → no inject
# ---------------------------------------------------------------------------


def test_enrich_voice_map_no_inject_for_builtin_cosyvoice(enrich_with_stubs):
    """**Contract 2**: voice_id is a public preset (in voice_catalog,
    matchable + verified) but NOT in user_voices → pass-through with no
    routing fields. Builtin DashScope path doesn't need worker.
    """
    payload = {
        "segment_id": "segment_002",
        "provider": "cosyvoice",
        "voice_id": "cosyvoice-v3.5-flash-zh-female-builtin-A",
    }
    enriched, error = enrich_with_stubs(
        payload,
        lookup_map={},  # not in user_voices
        public_ids={"cosyvoice-v3.5-flash-zh-female-builtin-A"},
    )
    assert error is None
    assert enriched is not None
    assert "requires_worker" not in enriched
    assert "worker_target_model" not in enriched
    assert enriched["voice_id"] == "cosyvoice-v3.5-flash-zh-female-builtin-A"


# ---------------------------------------------------------------------------
# Contract 3: CosyVoice voice with no user_voices + not public → 400
# ---------------------------------------------------------------------------


def test_enrich_voice_map_fail_closed_for_orphan_clone(enrich_with_stubs):
    """**Contract 3**: voice_id looks like a CosyVoice voice but isn't
    in user_voices AND isn't in the public catalog. Fail-closed with
    400 ``voice_clone_metadata_missing`` rather than letting the orphan
    drift to legacy.
    """
    payload = {
        "segment_id": "segment_003",
        "provider": "cosyvoice",
        "voice_id": "voice-cosy-v3-flash-orphan-uuid",
    }
    enriched, error = enrich_with_stubs(
        payload,
        lookup_map={},
        public_ids=set(),  # not a preset
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_metadata_missing"
    assert "voice_id" in error


# ---------------------------------------------------------------------------
# Contract 4: client-supplied requires_worker / worker_target_model stripped
# ---------------------------------------------------------------------------


def test_enrich_voice_map_strips_client_forged_routing(enrich_with_stubs):
    """**Contract 4 (security)**: client-supplied ``requires_worker`` and
    ``worker_target_model`` MUST be stripped before our own lookup runs.
    Otherwise a malicious client could set them on a NON-clone voice to
    force the worker path.

    Test: pass `requires_worker=True` and a forged `worker_target_model`
    on a public builtin voice. Expect: stripped, server treats it as a
    builtin (no injection).
    """
    payload = {
        "segment_id": "segment_004",
        "provider": "cosyvoice",
        "voice_id": "cosyvoice-v3.5-flash-builtin-B",
        "requires_worker": True,  # client-forged
        "worker_target_model": "cosyvoice-v3.5-plus",  # client-forged
    }
    enriched, error = enrich_with_stubs(
        payload,
        lookup_map={},  # NOT in user_voices
        public_ids={"cosyvoice-v3.5-flash-builtin-B"},
    )
    assert error is None
    assert enriched is not None
    # Client values WERE stripped — they're not in the enriched payload.
    assert "requires_worker" not in enriched
    assert "worker_target_model" not in enriched

    # And on the legitimate clone path the server-computed routing wins
    payload_clone = {
        "segment_id": "segment_004b",
        "provider": "cosyvoice",
        "voice_id": "voice-cosy-real-clone",
        # Client tries to spoof a DIFFERENT target_model than what DB says
        "requires_worker": True,
        "worker_target_model": "cosyvoice-v3.5-plus-MALICIOUS",
    }
    enriched2, error2 = enrich_with_stubs(
        payload_clone,
        lookup_map={
            "voice-cosy-real-clone": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",  # server value
            },
        },
    )
    assert error2 is None
    assert enriched2["requires_worker"] is True
    # Server value wins, NOT the forged "...-MALICIOUS" suffix.
    assert enriched2["worker_target_model"] == "cosyvoice-v3.5-flash"


# ---------------------------------------------------------------------------
# Contract 5: non-cosyvoice paths are stripped-pass-through
# ---------------------------------------------------------------------------


def test_enrich_voice_map_minimax_pass_through(enrich_with_stubs):
    """**Contract 5**: MiniMax provider → no lookup, no inject, just
    pass-through (still strip any forged routing flags)."""
    payload = {
        "segment_id": "segment_005",
        "provider": "minimax",
        "voice_id": "Chinese (Mandarin)_Wise_Woman",
        "requires_worker": True,  # client-forged
    }
    enriched, error = enrich_with_stubs(payload)
    assert error is None
    assert enriched is not None
    assert "requires_worker" not in enriched
    assert "worker_target_model" not in enriched
    assert enriched["provider"] == "minimax"


def test_enrich_voice_map_volcengine_pass_through(enrich_with_stubs):
    """**Contract 5b**: VolcEngine provider → pass-through."""
    payload = {
        "segment_id": "segment_006",
        "provider": "volcengine",
        "voice_id": "saturn_zh_male_07",
    }
    enriched, error = enrich_with_stubs(payload)
    assert error is None
    assert enriched is not None
    assert "requires_worker" not in enriched


# ---------------------------------------------------------------------------
# Contract 6: DB transient failure on cosyvoice path → 503
# ---------------------------------------------------------------------------


def test_enrich_voice_map_503_on_lookup_exception(enrich_with_stubs):
    """**Contract 6**: user_voices DB lookup raises → 503
    ``voice_clone_routing_lookup_failed`` (let user retry; DO NOT
    degrade to legacy)."""

    class _DbFail(RuntimeError):
        pass

    payload = {
        "segment_id": "segment_007",
        "provider": "cosyvoice",
        "voice_id": "voice-cosy-anything",
    }
    enriched, error = enrich_with_stubs(payload, lookup_raises=_DbFail)
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"


def test_enrich_voice_map_503_on_public_catalog_exception(enrich_with_stubs):
    """**Contract 6b**: user_voices lookup returns empty (no match) BUT
    public catalog lookup raises → also 503. Don't fail-open by skipping
    the public catalog check on a fault."""

    class _CatalogFail(RuntimeError):
        pass

    payload = {
        "segment_id": "segment_008",
        "provider": "cosyvoice",
        "voice_id": "voice-cosy-unknown",
    }
    enriched, error = enrich_with_stubs(
        payload, lookup_map={}, public_raises=_CatalogFail,
    )
    assert enriched is None
    assert error is not None
    assert error["code"] == "voice_clone_routing_lookup_failed"


# ---------------------------------------------------------------------------
# Contract 9: action == clear is exempt
# ---------------------------------------------------------------------------


def test_enrich_voice_map_clear_action_is_noop(enrich_with_stubs):
    """**Contract 9**: action='clear' (delete override) → no enrichment.
    Returns (None, None) so caller forwards original body unchanged.
    """
    payload = {
        "segment_id": "segment_009",
        "action": "clear",
    }
    enriched, error = enrich_with_stubs(payload)
    assert enriched is None
    assert error is None


def test_enrich_voice_map_empty_voice_id_no_enrichment(enrich_with_stubs):
    """**Contract 9b**: empty / missing voice_id → no enrichment. The
    storage layer will reject the request with a 400 ('voice_id must be
    non-empty'); we just don't perform a lookup with an empty key.
    """
    payload = {
        "segment_id": "segment_010",
        "provider": "cosyvoice",
        "voice_id": "",
    }
    enriched, error = enrich_with_stubs(payload)
    assert error is None
    # The strip-only path returns a stripped dict (no error, no inject).
    assert enriched is not None
    assert "requires_worker" not in enriched


# ---------------------------------------------------------------------------
# Cross-layer architectural guards (7-8 in module docstring)
# ---------------------------------------------------------------------------


def test_pipeline_does_not_import_gateway():
    """**Contract 7**: ``src/pipeline`` AST must not import any module
    starting with ``gateway.``. Already covered by F.3 elsewhere; we
    re-assert here so this PR's regression surface includes it.
    """
    process_py = REPO_ROOT / "src" / "pipeline" / "process.py"
    src = process_py.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("gateway"), (
                f"src/pipeline/process.py has forbidden gateway import: {mod}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("gateway"), (
                    f"src/pipeline/process.py has forbidden gateway "
                    f"import: {alias.name}"
                )


def test_services_jobs_does_not_db_lookup_user_voices_directly():
    """**Contract 8**: editing_voice_map / service / api / review_actions
    inside ``src/services/jobs/`` MUST NOT do their own ``user_voices``
    DB lookup. Gateway intercept is the only authority for routing
    enrichment; pipeline subprocess just trusts the gateway-enriched
    fields.

    Detection: AST scan source files for any reference to ``user_voices``
    SQL/ORM in import statements.
    """
    suspect_files = [
        "editing_voice_map.py",
        "review_actions.py",
        "service.py",
        "api.py",
    ]
    base = REPO_ROOT / "src" / "services" / "jobs"
    for fname in suspect_files:
        path = base / fname
        assert path.exists(), f"{path} missing"
        text = path.read_text(encoding="utf-8")
        # No imports of user_voice_service / UserVoice ORM
        # (gateway-only modules)
        for pattern in (
            r"from\s+user_voice_service\s+import",
            r"from\s+gateway\.",
            r"import\s+user_voice_service",
            # ORM model lives in gateway/models.py
            r"from\s+models\s+import\s+.*UserVoice",
        ):
            assert not re.search(pattern, text), (
                f"{fname}: pipeline-side file imports gateway-only "
                f"user_voices lookup helper ({pattern!r}). "
                f"Enrichment must stay in gateway intercept."
            )


def test_post_edit_mutation_branch_calls_enrich_for_voice_map():
    """**Contract 10 (wiring)**: ``_post_edit_mutation_with_policy``
    actually calls ``_enrich_editing_voice_map_routing`` when subpath
    is ``editing/voice-map``. Otherwise the helper exists but is dead.

    Static text scan — confirms the call site is present and uses
    ``override_body`` to forward the re-serialized payload.
    """
    src = (REPO_ROOT / "gateway" / "job_intercept.py").read_text(encoding="utf-8")
    # The call must be inside _post_edit_mutation_with_policy. Anchor to
    # the function definition + 5000 chars (function body) and confirm
    # the call + override_body usage.
    func_start = src.find("async def _post_edit_mutation_with_policy")
    assert func_start >= 0
    window = src[func_start: func_start + 8000]
    assert "_enrich_editing_voice_map_routing(" in window, (
        "_post_edit_mutation_with_policy doesn't call the enrichment "
        "helper. The wiring is missing — adding the helper alone is dead "
        "code (Codex P1 二轮 review concern)."
    )
    assert "override_body=forwarded_body" in window, (
        "_post_edit_mutation_with_policy doesn't re-serialize the "
        "enriched payload before forwarding (must pass override_body to "
        "proxy_request — otherwise upstream Job API sees the original "
        "client body without injected routing)."
    )
    # And it must check the editing/voice-map subpath
    assert 'subpath == "editing/voice-map"' in window, (
        "_post_edit_mutation_with_policy doesn't dispatch on the "
        "editing/voice-map subpath. Without the dispatch the helper is "
        "never invoked."
    )


# ---------------------------------------------------------------------------
# Integration: drive _post_edit_mutation_with_policy through real route
# (Codex 2026-05-27 P1 四轮 — static scan alone isn't enough, need to
# observe the actual override_body that reaches proxy_request)
# ---------------------------------------------------------------------------


class _MockJob:
    """Minimal Job stand-in matching the attributes
    ``_post_edit_mutation_with_policy`` touches."""

    def __init__(self, job_id: str = "test-job-id", status: str = "editing",
                 project_dir: str = "/tmp/proj", user_id: str = "u-test"):
        self.job_id = job_id
        self.status = status
        self.project_dir = project_dir
        self.user_id = user_id


class _MockExecuteResult:
    def __init__(self, scalar_value):
        self._scalar = scalar_value

    def scalar_one_or_none(self):
        return self._scalar


class _MockDb:
    """Stub AsyncSession that returns a Job row + supports commit / rollback."""

    def __init__(self, job: _MockJob | None):
        self.job = job
        self.commit_called = False

    async def execute(self, _stmt):
        return _MockExecuteResult(self.job)

    async def commit(self):
        self.commit_called = True

    async def rollback(self):
        pass


class _MockRequest:
    """Minimal Starlette-Request-shaped object. We control ``body()`` so
    re-reads return the same cached bytes, and ``method`` / ``headers`` so
    ``proxy_request`` doesn't blow up if it falls through to the real
    path (we monkeypatch it though)."""

    def __init__(self, body_bytes: bytes, *, method: str = "POST"):
        self._body = body_bytes
        self.method = method
        self.headers = {"content-type": "application/json"}
        self.query_params: dict[str, str] = {}
        self.url = type("U", (), {"path": "/job-api/jobs/test-job-id/editing/voice-map"})()
        # For starlette compatibility (not used by our code path):
        self.client = None

    async def body(self) -> bytes:
        return self._body


@pytest.fixture
def drive_route(monkeypatch):
    """Returns a callable that drives ``_post_edit_mutation_with_policy``
    with controllable mocks. Captures the body that would reach
    ``proxy_request`` and returns it for assertion.
    """
    import job_intercept
    import user_voice_service

    async def _drive(
        *,
        request_body: dict,
        lookup_map: dict[str, dict[str, Any]] | None = None,
        public_ids: set[str] | None = None,
        lookup_raises: type[BaseException] | None = None,
        public_raises: type[BaseException] | None = None,
        enable_post_edit: bool = True,
        job_status: str = "editing",
        subpath: str = "editing/voice-map",
        user_id: str = "u-test",
    ) -> dict[str, Any]:
        """Returns dict with keys:
          - ``status``: HTTP status from the response
          - ``upstream_body``: dict | None — body that reached proxy
          - ``upstream_called``: bool
          - ``response_json``: dict | None — JSONResponse body if no proxy
        """
        captured: dict[str, Any] = {
            "upstream_called": False,
            "override_body": None,
            "request_body_fallback": None,
        }

        # Mock DB lookups
        async def fake_lookup(db, *, user_id, voice_ids):
            if lookup_raises:
                raise lookup_raises("simulated transient DB failure")
            return dict(lookup_map or {})

        async def fake_public(db):
            if public_raises:
                raise public_raises("simulated catalog failure")
            return set(public_ids or [])

        monkeypatch.setattr(
            user_voice_service,
            "lookup_clone_voice_routing_metadata",
            fake_lookup,
        )
        monkeypatch.setattr(
            job_intercept,
            "_fetch_cosyvoice_public_voice_ids",
            fake_public,
        )

        # Mock job ownership / access enforcement — no-op
        async def _fake_enforce(*args, **kwargs):
            return None

        monkeypatch.setattr(
            job_intercept, "_enforce_post_edit_access", _fake_enforce,
        )

        # Mock the policy flag
        monkeypatch.setattr(
            job_intercept.settings, "enable_post_edit", enable_post_edit,
        )

        # Mock proxy_request — capture the override_body (or the request
        # body fallback) and return a fake 200 success Response.
        async def _fake_proxy(*, request, upstream_base, strip_prefix,
                              override_body=None, **kwargs):
            from starlette.responses import Response as _R
            captured["upstream_called"] = True
            if override_body is not None:
                captured["override_body"] = override_body
            else:
                captured["request_body_fallback"] = await request.body()
            return _R(
                content=b'{"success": true, "stub_proxy": true}',
                status_code=200,
                media_type="application/json",
            )

        monkeypatch.setattr(job_intercept, "proxy_request", _fake_proxy)

        # Construct mock request + job
        body_bytes = json.dumps(request_body).encode("utf-8")
        request = _MockRequest(body_bytes)
        mock_user = type("MockUser", (), {"id": user_id})()
        mock_db = _MockDb(_MockJob(status=job_status, user_id=user_id))

        try:
            response = await job_intercept._post_edit_mutation_with_policy(
                request, "test-job-id", mock_db, mock_user, subpath=subpath,
            )
            status_code = response.status_code
            body = response.body
            try:
                response_json = json.loads(body) if body else None
            except Exception:
                response_json = None
        except job_intercept.HTTPException as exc:
            status_code = exc.status_code
            response_json = {"detail": exc.detail}

        # Parse override_body if captured
        upstream_body: dict | None = None
        if captured["override_body"]:
            upstream_body = json.loads(captured["override_body"])
        elif captured["request_body_fallback"]:
            upstream_body = json.loads(captured["request_body_fallback"])

        return {
            "status": status_code,
            "upstream_body": upstream_body,
            "upstream_called": captured["upstream_called"],
            "override_body_used": captured["override_body"] is not None,
            "response_json": response_json,
        }

    def run(**kwargs):
        return asyncio.run(_drive(**kwargs))

    return run


# Section: integration — REAL ROUTE → enrichment → proxy receives override


def test_integration_real_payload_injects_routing_before_proxy(drive_route):
    """**Codex 2026-05-27 P1 四轮 (integration)**: when the frontend
    sends the REAL payload shape (only segment_id / provider / voice_id),
    by the time proxy_request is invoked the body MUST already contain
    `requires_worker=True` + `worker_target_model`.

    This is the contract that static scans can't prove:
    1. subpath ``editing/voice-map`` triggers the enrichment branch
    2. enrichment runs the lookup
    3. enriched payload is re-serialized into ``override_body``
    4. proxy_request receives that override_body (NOT the original)
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_001",
            "provider": "cosyvoice",
            "voice_id": "voice-cosy-flash-clone-uuid",
            # NO requires_worker, NO worker_target_model — real shape
        },
        lookup_map={
            "voice-cosy-flash-clone-uuid": {
                "requires_worker": True,
                "worker_target_model": "cosyvoice-v3.5-flash",
            },
        },
    )
    assert result["status"] == 200
    assert result["upstream_called"] is True
    assert result["override_body_used"] is True, (
        "proxy_request was called WITHOUT override_body — the enrichment "
        "ran but the re-serialized payload didn't reach upstream. Real "
        "Job API would see the original client body and persist a "
        "downgraded voice_map entry."
    )
    assert result["upstream_body"] is not None
    body = result["upstream_body"]
    assert body["requires_worker"] is True, (
        "upstream body missing requires_worker=True. The enrichment "
        "either didn't run or didn't merge into override_body."
    )
    assert body["worker_target_model"] == "cosyvoice-v3.5-flash"
    assert body["provider"] == "cosyvoice"
    assert body["voice_id"] == "voice-cosy-flash-clone-uuid"
    assert body["segment_id"] == "segment_int_001"


def test_integration_builtin_cosyvoice_no_routing_injected(drive_route):
    """**Codex P1 四轮 (integration)**: builtin CosyVoice voice → enrichment
    runs but DOESN'T inject routing. upstream body should NOT carry
    requires_worker, but the body MUST still be the (stripped) version
    via override_body (not the raw client body) — because we stripped
    client-supplied fields defensively.
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_002",
            "provider": "cosyvoice",
            "voice_id": "cosyvoice-v3.5-flash-builtin-A",
        },
        lookup_map={},
        public_ids={"cosyvoice-v3.5-flash-builtin-A"},
    )
    assert result["status"] == 200
    assert result["upstream_called"] is True
    assert result["override_body_used"] is True
    body = result["upstream_body"]
    assert "requires_worker" not in body
    assert "worker_target_model" not in body


def test_integration_orphan_clone_returns_400_no_proxy(drive_route):
    """**Codex P1 四轮 (integration)**: orphan CosyVoice clone-shaped id →
    400 voice_clone_metadata_missing AND proxy_request is NEVER called.
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_003",
            "provider": "cosyvoice",
            "voice_id": "voice-cosy-orphan-uuid",
        },
        lookup_map={},
        public_ids=set(),
    )
    assert result["status"] == 400
    assert result["upstream_called"] is False, (
        "proxy_request was called even though enrichment returned 400. "
        "Orphan clone voices must fail-closed at the gateway BEFORE "
        "the upstream Job API touches them."
    )
    detail = result["response_json"]["detail"]
    assert detail["code"] == "voice_clone_metadata_missing"


def test_integration_db_failure_returns_503_no_proxy(drive_route):
    """**Codex P1 四轮 (integration)**: transient DB failure → 503 AND
    proxy_request never invoked. Don't let an upstream Job API write
    happen while we have an inconsistent view of user_voices.
    """

    class _DbFail(RuntimeError):
        pass

    result = drive_route(
        request_body={
            "segment_id": "segment_int_004",
            "provider": "cosyvoice",
            "voice_id": "voice-cosy-anything",
        },
        lookup_raises=_DbFail,
    )
    assert result["status"] == 503
    assert result["upstream_called"] is False
    assert result["response_json"]["detail"]["code"] == "voice_clone_routing_lookup_failed"


def test_integration_client_forged_routing_does_not_leak_to_upstream(drive_route):
    """**Codex P1 四轮 (security integration)**: client sends forged
    `requires_worker` + `worker_target_model` for a BUILTIN voice
    (would force the worker path). Gateway must strip both before
    proxying. Upstream body must NOT contain forged fields.
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_005",
            "provider": "cosyvoice",
            "voice_id": "cosyvoice-v3.5-flash-builtin-B",
            "requires_worker": True,  # forged
            "worker_target_model": "cosyvoice-v3.5-plus",  # forged
        },
        lookup_map={},
        public_ids={"cosyvoice-v3.5-flash-builtin-B"},
    )
    assert result["status"] == 200
    assert result["override_body_used"] is True
    body = result["upstream_body"]
    assert "requires_worker" not in body
    assert "worker_target_model" not in body


def test_integration_clear_action_pass_through_no_enrichment(drive_route):
    """**Codex P1 四轮 (integration)**: clear action doesn't trigger
    enrichment but still proxies through. upstream body is the original
    client payload (no override_body used).
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_006",
            "action": "clear",
        },
        lookup_map={},
    )
    assert result["status"] == 200
    assert result["upstream_called"] is True
    # clear path returns (None, None) → no override_body, raw forward
    assert result["override_body_used"] is False
    # Original body still reaches upstream
    body = result["upstream_body"]
    assert body["action"] == "clear"
    assert body["segment_id"] == "segment_int_006"


def test_integration_minimax_path_strips_forged_routing(drive_route):
    """**Codex P1 四轮 (integration)**: MiniMax path with forged
    requires_worker → strip + forward stripped body via override_body.
    """
    result = drive_route(
        request_body={
            "segment_id": "segment_int_007",
            "provider": "minimax",
            "voice_id": "Chinese (Mandarin)_Wise_Woman",
            "requires_worker": True,  # forged
        },
    )
    assert result["status"] == 200
    assert result["upstream_called"] is True
    # MiniMax path triggers strip-only branch → override_body used
    # (carries the stripped version) so upstream cannot see the forged flag.
    assert result["override_body_used"] is True
    body = result["upstream_body"]
    assert body["provider"] == "minimax"
    assert "requires_worker" not in body
