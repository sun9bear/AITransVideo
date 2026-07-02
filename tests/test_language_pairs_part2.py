"""PR-A part 2 acceptance tests — §2 entitlements, §3 create-path gate,
§4 double-layer capability gate, §5 facts endpoint, §6 cost-by-pair.

Plan: docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md §3/§4/§5
+ docs/plans/2026-06-13-pra-part2-implementation-map.md §2-§7.

Gateway business modules have a deep import chain (database -> asyncpg). We stub
only the infrastructure layer, mirroring tests/test_gateway_entitlements.py +
tests/test_gateway_create_job.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub gateway infrastructure so business-logic modules import cleanly.
# ---------------------------------------------------------------------------
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from admin_settings import AdminSettings  # noqa: E402
from entitlements import get_effective_allowed_language_pairs  # noqa: E402
from job_intercept import intercept_create_job, intercept_language_facts  # noqa: E402
from services.language_registry import (  # noqa: E402
    CAPABILITY_POST_EDIT,
    CAPABILITY_SUGGEST_SPLIT,
    DEFAULT_LANGUAGE_PAIR,
    SUPPORTED_LANGUAGE_PAIRS,
)
from services.jobs.api import (  # noqa: E402
    _gate_pair_post_edit,
    _gate_pair_suggest_split,
    _require_language_pair_capability,
)
from services.jobs.service import JobConflictError  # noqa: E402

ZH_EN = "zh-CN->en"
EN_ZH = "en->zh-CN"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _user(*, role="user", uid="uid-1"):
    return SimpleNamespace(id=uid, role=role, email="u@test.com", display_name="U")


def _admin_settings(*, enabled=False, allowlist_enabled=True, allowlist=None):
    """Lightweight stub for the entitlements unit tests, which read only the
    three language_pairs_* attributes directly."""
    return SimpleNamespace(
        language_pairs_enabled=enabled,
        language_pairs_user_allowlist_enabled=allowlist_enabled,
        language_pairs_allowlist=list(allowlist or []),
    )


def _real_admin(
    *, enabled=False, allowlist_enabled=True, allowlist=None,
    express_tts_provider=None,
):
    """A REAL AdminSettings (all other fields defaulted) — required when the
    patched load_settings is also consumed by compute_job_policy, which reads
    studio_tts_provider / etc.

    ``express_tts_provider``: CM-02 — the default ("cosyvoice") makes express
    clone-only (voice_strategy=express_auto_clone), whose consent gate fires
    BEFORE the language gate (§5 vs §5a in intercept_create_job, shipped
    behavior). Language-gate tests pass "mimo" to route express to
    preset_mapping so the subject under test is actually reached.
    """
    kwargs = {}
    if express_tts_provider is not None:
        kwargs["express_tts_provider"] = express_tts_provider
    return AdminSettings(
        language_pairs_enabled=enabled,
        language_pairs_user_allowlist_enabled=allowlist_enabled,
        language_pairs_allowlist=list(allowlist or []),
        **kwargs,
    )


# ===================================================================
# §2 — entitlements: get_effective_allowed_language_pairs
# ===================================================================

class TestLanguagePairEntitlements:
    def test_default_pair_always_allowed_even_when_disabled(self):
        pairs = get_effective_allowed_language_pairs(
            _user(), admin=_admin_settings(enabled=False)
        )
        assert DEFAULT_LANGUAGE_PAIR in pairs
        assert ZH_EN not in pairs

    def test_anonymous_user_gets_only_default(self):
        pairs = get_effective_allowed_language_pairs(None, admin=_admin_settings(enabled=True, allowlist_enabled=False))
        assert pairs == [DEFAULT_LANGUAGE_PAIR]

    def test_zh_en_denied_when_master_switch_off(self):
        pairs = get_effective_allowed_language_pairs(
            _user(role="admin"), admin=_admin_settings(enabled=False)
        )
        # Master switch off → even admin gets only the default pair.
        assert ZH_EN not in pairs

    def test_zh_en_allowed_for_all_when_allowlist_disabled(self):
        pairs = get_effective_allowed_language_pairs(
            _user(), admin=_admin_settings(enabled=True, allowlist_enabled=False)
        )
        assert ZH_EN in pairs

    def test_zh_en_denied_for_non_allowlisted_user(self):
        pairs = get_effective_allowed_language_pairs(
            _user(uid="bob"),
            admin=_admin_settings(enabled=True, allowlist_enabled=True, allowlist=["alice"]),
        )
        assert ZH_EN not in pairs

    def test_zh_en_allowed_for_allowlisted_user(self):
        pairs = get_effective_allowed_language_pairs(
            _user(uid="alice"),
            admin=_admin_settings(enabled=True, allowlist_enabled=True, allowlist=["alice"]),
        )
        assert ZH_EN in pairs

    def test_zh_en_allowed_for_admin_bypassing_allowlist(self):
        pairs = get_effective_allowed_language_pairs(
            _user(role="admin", uid="not-in-list"),
            admin=_admin_settings(enabled=True, allowlist_enabled=True, allowlist=["alice"]),
        )
        assert ZH_EN in pairs

    def test_fail_closed_when_admin_settings_unreadable(self):
        # admin=None forces a load_settings() call; make it raise → only default.
        with patch("admin_settings.load_settings", side_effect=RuntimeError("boom")):
            pairs = get_effective_allowed_language_pairs(_user())
        assert pairs == [DEFAULT_LANGUAGE_PAIR]


# ===================================================================
# §4 — Job API second-layer capability gate
# ===================================================================

class _FakeService:
    def __init__(self, language_pair):
        self._record = SimpleNamespace(job_id="j1", language_pair=language_pair)

    def require_job(self, job_id):
        return self._record


class TestLanguagePairCapabilityGate:
    def test_default_pair_allows_post_edit_and_suggest_split(self):
        rec = SimpleNamespace(language_pair=EN_ZH)
        # No raise == allowed.
        _require_language_pair_capability(rec, CAPABILITY_POST_EDIT)
        _require_language_pair_capability(rec, CAPABILITY_SUGGEST_SPLIT)

    def test_zh_en_rejects_post_edit(self):
        with pytest.raises(JobConflictError):
            _require_language_pair_capability(
                SimpleNamespace(language_pair=ZH_EN), CAPABILITY_POST_EDIT
            )

    def test_zh_en_rejects_suggest_split(self):
        with pytest.raises(JobConflictError):
            _require_language_pair_capability(
                SimpleNamespace(language_pair=ZH_EN), CAPABILITY_SUGGEST_SPLIT
            )

    def test_unknown_pair_falls_back_to_default_allows(self):
        # Empty / legacy pair_key → GA default profile (fully adapted) → allowed.
        _require_language_pair_capability(SimpleNamespace(language_pair=""), CAPABILITY_POST_EDIT)

    def test_gate_wrappers_load_record_and_reject_zh_en(self):
        svc = _FakeService(ZH_EN)
        with pytest.raises(JobConflictError):
            _gate_pair_post_edit(svc, "j1")
        with pytest.raises(JobConflictError):
            _gate_pair_suggest_split(svc, "j1")

    def test_gate_wrappers_allow_default_pair(self):
        svc = _FakeService(EN_ZH)
        _gate_pair_post_edit(svc, "j1")
        _gate_pair_suggest_split(svc, "j1")


# ===================================================================
# §3 — create-path validation + requires_review override
# ===================================================================

def _make_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    req.headers = {"content-type": "application/json"}
    req.method = "POST"
    req.url = MagicMock()
    req.url.path = "/job-api/jobs"
    req.query_params = {}
    return req


def _make_db():
    """Minimal AsyncSession mock — the create-path language gate returns before
    any DB write, so a count=0 default is enough for the rejection paths."""
    db = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    generic = MagicMock()
    generic.scalar.return_value = 0
    generic.scalar_one_or_none.return_value = None
    generic.all.return_value = []

    async def execute(stmt, *a, **k):
        s = str(stmt).lower()
        if "count" in s:
            return count_result
        return generic

    db.execute = execute
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestCreatePathLanguageGate:
    # CM-02（docs/plans/2026-07-02-commercialization-sprint-plan.md §2）:
    # express 默认 provider=cosyvoice → clone-only（express_auto_clone），其
    # consent 闸（§5）先于语言闸（§5a）返回 403 express_clone_consent_required
    # ——这是 express-clone 单元评审合并的既有生产行为，非本套件的被测对象。
    # 语言闸测试统一把 express 路由到 preset（mimo）隔离被测点；闸顺序本身由
    # test_express_cosyvoice_consent_gate_precedes_language_gate 文档化钉住。
    def test_unsupported_pair_returns_400_before_forward(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "estimated_duration_seconds": 60,
            "source_language": "klingon",
            "target_language": "zh-CN",
        })
        with patch(
            "admin_settings.load_settings",
            return_value=_real_admin(express_tts_provider="mimo"),
        ), patch("job_intercept.proxy_request", new_callable=AsyncMock) as proxy:
            resp = _run(intercept_create_job(req, _make_db(), _user()))
        assert resp.status_code == 400
        assert json.loads(resp.body)["error"] == "unsupported_language_pair"
        proxy.assert_not_called()

    def test_zh_en_not_allowed_returns_403_before_forward(self):
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "estimated_duration_seconds": 60,
            "source_language": "zh-CN",
            "target_language": "en",
        })
        with patch(
            "admin_settings.load_settings",
            return_value=_real_admin(enabled=False, express_tts_provider="mimo"),
        ), patch("job_intercept.proxy_request", new_callable=AsyncMock):
            resp = _run(intercept_create_job(req, _make_db(), _user(role="user")))
        assert resp.status_code == 403
        assert json.loads(resp.body)["error"] == "language_pair_not_allowed"

    def test_express_cosyvoice_consent_gate_precedes_language_gate(self):
        """文档化测试（CM-02）：express+cosyvoice（clone-only）的 consent 闸
        目前先于语言闸——对同时缺 consent 且语言对非法的请求，用户先看到
        express_clone_consent_required。这不是规格保证：若未来有意重排闸序，
        更新本测试并知会 langpair 侧（勿静默重排，见 CM-02 单元记录）。"""
        req = _make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
            "estimated_duration_seconds": 60,
            "source_language": "klingon",
            "target_language": "zh-CN",
        })
        with patch(
            "admin_settings.load_settings",
            return_value=_real_admin(express_tts_provider="cosyvoice"),
        ), patch("job_intercept.proxy_request", new_callable=AsyncMock) as proxy:
            resp = _run(intercept_create_job(req, _make_db(), _user()))
        assert resp.status_code == 403
        assert json.loads(resp.body)["error"] == "express_clone_consent_required"
        proxy.assert_not_called()
        proxy.assert_not_called()

    # NOTE on coverage strategy for the forward + requires_review override:
    # the 400/403 gates above return BEFORE the upstream forward, so they need
    # no proxy/probe mocking and are order-robust. The canonical-forward + D1
    # override happen AFTER the youtube probe + at the proxy boundary, which is
    # order-sensitive under the cross-file `database`-stub pollution documented
    # in feedback_test_database_stub_convention (~335 known pre-existing
    # failures). Rather than ship an order-flaky integration test, we pin those
    # two invariants with a deterministic source guard (part-1's guard style).
    def test_create_path_forward_and_override_invariants(self):
        src = (
            Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
        ).read_text(encoding="utf-8")
        # Canonical pair is injected into the proxied body so the Job API
        # JobRecord persists normalized values (the §4 gate reads them).
        assert 'request_data["source_language"] = resolved_pair.source_language' in src
        assert 'request_data["target_language"] = resolved_pair.target_language' in src
        # D1 non-interactive override, gated by the explicit pair set (not
        # is_default), set BEFORE request_data.update(policy) so it reaches both
        # the Job API forwarded body and the gateway PG row.
        assert "_NON_INTERACTIVE_LANGUAGE_PAIRS" in src
        assert 'policy["requires_review"] = False' in src
        # The override + forward-injection must precede the upstream forward.
        # NB: a comment above the override mentions `request_data.update(policy)`,
        # so match the LAST occurrence (the actual code line, which comes after
        # the override block) — not the comment mention.
        override_idx = src.index('policy["requires_review"] = False')
        forward_idx = src.rindex("request_data.update(policy)")
        assert override_idx < forward_idx, "requires_review override must precede the forward"
        # Override is SCOPED TO STUDIO (codex P2): clearing requires_review for
        # `smart` would break Smart's review-gated auto-review branch. The
        # service_mode guard sits in the condition right above the override.
        window = src[max(0, override_idx - 400):override_idx]
        assert '_SERVICE_MODE_STUDIO = "studio"' in src
        assert "service_mode == _SERVICE_MODE_STUDIO" in window, (
            "requires_review override must be Studio-scoped (codex P2)"
        )
        assert "_NON_INTERACTIVE_LANGUAGE_PAIRS" in window

    def test_partial_language_input_locks_default_not_400(self):
        # Only-source or only-target is an INCOMPLETE pair request → it must lock
        # to the GA default (zero-regression), NEVER a 400. The guard condition
        # uses OR so any missing/empty field takes the default branch (a
        # previously-ignored stray field must not start 400-ing).
        src = (
            Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
        ).read_text(encoding="utf-8")
        assert (
            "if _raw_source_language is None or _raw_target_language is None:" in src
        ), "partial language input must take the default branch (OR, not AND)"

    def test_registry_pipeline_ready_flags(self):
        # Code-level hard gate: the default pair is GA, zh-CN->en is now
        # canary-runnable when the admin/allowlist gate grants access.
        assert SUPPORTED_LANGUAGE_PAIRS[EN_ZH].pipeline_ready is True
        assert SUPPORTED_LANGUAGE_PAIRS[ZH_EN].pipeline_ready is True

    def test_zh_en_canary_still_has_code_level_pipeline_guard(self):
        # The guard remains in place for future registered pairs; zh-CN->en is
        # opened by changing the registry constant, not by bypassing the check.
        src = (
            Path(__file__).resolve().parent.parent / "gateway" / "job_intercept.py"
        ).read_text(encoding="utf-8")
        assert "if not resolved_pair.pipeline_ready:" in src
        assert "language_pair_not_yet_available" in src


# ===================================================================
# §5 — GET /api/language-facts
# ===================================================================

class TestLanguageFactsEndpoint:
    def test_anonymous_sees_only_default_pair(self):
        resp = _run(intercept_language_facts(user=None))
        data = json.loads(resp.body)["language_pairs"]
        keys = {p["pair_key"] for p in data}
        assert keys == {EN_ZH}
        default = data[0]
        assert default["label"] == "英文 → 中文"
        assert default["is_default"] is True
        assert default["workflow_capabilities"] == [
            "transcribe", "translate", "tts", "subtitles", "jianying",
        ]
        # D5: the display key is workflow_capabilities, NOT adapted_paid_capabilities.
        assert "adapted_paid_capabilities" not in default

    def test_admin_with_master_switch_sees_both_pairs(self):
        with patch("admin_settings.load_settings", return_value=_admin_settings(enabled=True)):
            resp = _run(intercept_language_facts(user=_user(role="admin")))
        data = json.loads(resp.body)["language_pairs"]
        keys = {p["pair_key"] for p in data}
        assert keys == {EN_ZH, ZH_EN}
        zh_en = next(p for p in data if p["pair_key"] == ZH_EN)
        assert zh_en["label"] == "中文 → 英文"
        assert zh_en["is_default"] is False
        # Hard-gate signal so the frontend can render 内测 / enable the option.
        assert zh_en["pipeline_ready"] is True
        assert next(p for p in data if p["pair_key"] == EN_ZH)["pipeline_ready"] is True

    def test_logged_in_non_allowlisted_user_sees_only_default(self):
        # Zero-regression: a regular logged-in user NOT in the allowlist must see
        # only the GA default even when the master switch is on (the facts
        # endpoint mirrors get_effective_allowed_language_pairs).
        with patch(
            "admin_settings.load_settings",
            return_value=_admin_settings(enabled=True, allowlist_enabled=True, allowlist=["alice"]),
        ):
            resp = _run(intercept_language_facts(user=_user(uid="bob")))
        data = json.loads(resp.body)["language_pairs"]
        assert {p["pair_key"] for p in data} == {EN_ZH}
        assert len(data) == 1


# ===================================================================
# §6 — cost-by-pair rollup (lightweight source guard; the rollup is a
# DB-bound admin handler, so a string anchor mirrors part-1's guard style)
# ===================================================================

class TestCostByPairGuard:
    def test_cost_management_exposes_by_pair_rollup(self):
        src = (Path(__file__).resolve().parent.parent / "gateway" / "cost_management.py").read_text(
            encoding="utf-8"
        )
        assert "cost_per_minute_by_pair" in src
        assert "by_pair_minutes" in src
        assert "by_pair_cost" in src
        # Reads the authoritative PG row language_pair, not a re-derived value.
        assert 'getattr(job, "language_pair"' in src
