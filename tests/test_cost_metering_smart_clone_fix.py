"""Cost-accounting fixes (2026-05-18).

== Background ==

Audit of job_14989c5e9ec44bdebc5f3f5d6111db54 admin cost view exposed
two systemic gaps:

1. **Voice clone metering not recorded.** Smart auto-clone path
   (auto_voice_review → CloneProvider.clone_voice) succeeded and the
   sidecar smart_decisions.jsonl logged it, but
   ``usage_meter.record_voice_clone(...)`` was never called for the
   fresh-clone case. Only ``record_voice_reuse`` (cache-hit path,
   billable=False) recorded anything. So
   ``usage_summary.voice_clone_call_count = 0`` for jobs that did
   pay MiniMax real money (¥9.9/clone). Admin /cost view showed
   ¥0 voice-clone cost → margin inflated ~10pp.

2. **Gemini 3.1 Pro priced too HIGH (user thought "too low",
   reality was opposite).** Configured at $2/$12 per M tokens; the
   actual Google official ≤200K-token tier is $1.25/$10. Most of
   our calls are well within 200K, so we over-estimated costs ~28%.

3. **USD-priced LLM rates create exchange-rate drift.** Cost view
   computed RMB by multiplying USD config × usd_to_rmb at render
   time. When official prices change OR exchange rate moves, admin
   cost report drifts away from actual provider bills. Switching
   to RMB-direct (per user request 2026-05-18) keeps reporting and
   billing in the same currency.

== This test ==

Pins both fixes:

A. Process.py smart CLONED branch calls ``usage_meter.record_voice_clone``
   for every successful fresh clone (billable=True).

B. cost_management._FALLBACK_CATALOG / DEFAULT_PRICE_CATALOG has
   RMB-direct fields (``_per_million_rmb``) for all USD-priced
   models. Gemini 3.1 Pro tuned to Google ≤200K tier.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_GATEWAY = _REPO / "gateway"
for p in (_SRC, _GATEWAY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestSmartClonedRecordsToMeter:
    """Pin process.py smart CLONED branch records to UsageMeter."""

    def _source(self) -> str:
        return _PROCESS_PY.read_text(encoding="utf-8")

    def test_smart_cloned_branch_calls_record_voice_clone(self):
        """The smart inline branch's ``if _dec.choice == CLONED:``
        handler must call ``usage_meter.record_voice_clone(...)``.
        Without this, fresh auto-clones (real MiniMax charges) show
        up as ¥0 in admin cost view. Real incident: job_14989c5e..."""
        source = self._source()
        # Find the smart inline CLONED handler. The anchor is
        # ``_dec.choice == VoiceReviewChoice.CLONED`` — there are
        # multiple sites; we want the one followed by the audit emit
        # (the smart inline branch processing loop, not the count
        # aggregation sites).
        anchor_pattern = re.compile(
            r"if\s+_dec\.choice\s*==\s*VoiceReviewChoice\.CLONED\s*:\s*\n"
            r"\s+_sp_entry\[\"voice_id\"\]"
        )
        match = anchor_pattern.search(source)
        assert match is not None, (
            "Smart inline CLONED handler not found. If the structure "
            "was refactored, update this test's anchor."
        )

        # Look 4000 chars after the anchor for record_voice_clone.
        post = source[match.start() : match.start() + 4000]
        assert "usage_meter.record_voice_clone(" in post, (
            "Smart inline CLONED handler must call "
            "``usage_meter.record_voice_clone(...)`` so admin cost "
            "view + usage_summary.voice_clone_call_count reflect "
            "real MiniMax charges. Missing this call means jobs that "
            "pay for clones show ¥0 cost (job_14989c5e... real "
            "incident: 2 × ¥9.9 = ¥19.80 missing from cost view)."
        )

    def test_record_voice_clone_passes_billable_true(self):
        """For fresh clones, billable=True (vs reuse path which uses
        billable=False). Without this distinction the cost rollup
        can't tell paid clones from free reuses."""
        source = self._source()
        anchor = "usage_meter.record_voice_clone("
        idx = source.find(anchor)
        assert idx >= 0
        # Find the matching close-paren / kwargs window (~1000 chars)
        window = source[idx : idx + 1500]
        assert "billable=True" in window, (
            "Smart CLONED branch's record_voice_clone call must "
            "pass billable=True (vs reuse path's billable=False). "
            "Otherwise cost rollup misses MiniMax charges. Window:\n"
            f"{window[:600]}"
        )

    def test_record_voice_clone_wrapped_in_try_except(self):
        """Metering failure must NOT block user-facing delivery.
        Pin the defensive try/except around the meter call."""
        source = self._source()
        anchor = "usage_meter.record_voice_clone("
        idx = source.find(anchor)
        assert idx >= 0
        # 200 chars before the call should contain `try:`
        pre = source[max(0, idx - 200) : idx]
        assert "try:" in pre, (
            "record_voice_clone call must be wrapped in try/except "
            "so a metering hiccup never blocks the pipeline. Pre-window:\n"
            f"{pre[-200:]}"
        )


class TestRmbDirectPricing:
    """Pin LLM pricing uses RMB-direct fields (not USD with conversion)."""

    def _catalog(self):
        from cost_management import DEFAULT_PRICE_CATALOG
        return DEFAULT_PRICE_CATALOG

    def test_gemini_3_1_pro_uses_rmb_direct(self):
        rate = self._catalog()["llm"]["gemini:gemini-3.1-pro-preview"]
        assert "input_per_million_rmb" in rate
        assert "output_per_million_rmb" in rate
        # Tuned to Google official ≤200K tier ($1.25/$10) × 7.2 = ¥9/¥72
        assert rate["input_per_million_rmb"] == 9.0, (
            f"Gemini 3.1 Pro input price should be ¥9.0/M (Google "
            f"≤200K tier $1.25 × 7.2). Got {rate['input_per_million_rmb']}"
        )
        assert rate["output_per_million_rmb"] == 72.0, (
            f"Gemini 3.1 Pro output price should be ¥72.0/M "
            f"($10 × 7.2). Got {rate['output_per_million_rmb']}"
        )

    def test_all_llm_models_have_rmb_direct_fields(self):
        """No LLM model should rely SOLELY on USD fields anymore.
        At minimum each must define ``input_per_million_rmb`` and
        ``output_per_million_rmb`` directly."""
        llm = self._catalog()["llm"]
        usd_only_models: list[str] = []
        for key, rate in llm.items():
            has_rmb_input = "input_per_million_rmb" in rate
            has_rmb_output = "output_per_million_rmb" in rate
            if not (has_rmb_input and has_rmb_output):
                usd_only_models.append(key)
        assert not usd_only_models, (
            "These LLM models still lack RMB-direct pricing (will "
            "drift with exchange rate). Add ``input_per_million_rmb`` "
            "and ``output_per_million_rmb`` fields per user request "
            f"2026-05-18:\n  - " + "\n  - ".join(usd_only_models)
        )

    def test_rate_to_rmb_helper_prefers_rmb_over_usd(self):
        """The ``_rate_to_rmb`` helper must read ``_rmb`` field
        directly when present (no usd_to_rmb multiplication)."""
        from cost_management import _rate_to_rmb

        rate_rmb = {"input_per_million_rmb": 9.0}
        assert _rate_to_rmb(rate_rmb, "input_per_million", 7.2) == 9.0

        # Fall back to USD × rate if no RMB field
        rate_usd = {"input_per_million_usd": 1.25}
        assert _rate_to_rmb(rate_usd, "input_per_million", 7.2) == 1.25 * 7.2

        # Missing → 0
        assert _rate_to_rmb({}, "input_per_million", 7.2) == 0.0

    def test_gemini_3_1_pro_audio_input_priced(self):
        """Smart S2 Pass 1/3 send audio chunks to Gemini Pro. Audio
        input pricing must be present so the cost view reflects
        multimodal cost. Audio is billed per audio-token = 25/s."""
        rate = self._catalog()["llm"]["gemini:gemini-3.1-pro-preview"]
        assert "audio_input_per_million_rmb" in rate
        assert "audio_tokens_per_second" in rate
        assert rate["audio_tokens_per_second"] == 25

    def test_concrete_recomputation_matches_expected_rmb(self):
        """Sanity: recompute the largest LLM row from job_14989c5e
        with new pricing.

        Original (USD config): ¥3.82 for 14 calls 69,955/32,519 tokens.
        New (RMB config ≤200K tier): expected ¥2.97.
        """
        from cost_management import _rate_to_rmb

        rate = self._catalog()["llm"]["gemini:gemini-3.1-pro-preview"]
        input_price = _rate_to_rmb(rate, "input_per_million", 7.2)
        output_price = _rate_to_rmb(rate, "output_per_million", 7.2)

        input_cost = 69_955 * input_price / 1_000_000
        output_cost = 32_519 * output_price / 1_000_000
        total = input_cost + output_cost

        # Expected ≈ 9.0 × 69955/1M + 72.0 × 32519/1M
        #         = 0.6296 + 2.3414 = 2.971
        assert 2.90 <= total <= 3.05, (
            f"Recomputed Gemini Pro cost for 69955 in / 32519 out "
            f"should be ~¥2.97 (down from old ¥3.82). Got ¥{total:.2f}"
        )
