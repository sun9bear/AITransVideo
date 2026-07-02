#!/usr/bin/env python3
"""CM-03 zh->en natural_length_ratio calibration script.

Measures the real target/source length ratio for the ``zh-CN->en``
language pair by running the SAME translation entry point the pipeline
uses (``services.gemini.translator.GeminiTranslator.translate``) against
a small corpus of Chinese source clips, then compares the pooled p50
ratio against the provisional constant in
``services.language_registry.SUPPORTED_LANGUAGE_PAIRS`` (currently
``0.55``, see plan 2026-06-13-multilingual-mutual-translation-plan-v3.md
Phase 0 point 4).

HARD SAFETY RULE (CLAUDE.md paid-API constraint): this script NEVER
calls the paid Gemini translation API on its own. There are exactly two
modes:

* ``--estimate`` (also the DEFAULT with no flags) — pure offline. Reads
  the corpus, counts clips/segments/CJK characters, and prints a cost
  estimate table. Never imports/instantiates ``GeminiTranslator``.
* ``--run`` — actually calls the paid API, translating every clip in
  the corpus and computing the real ratio distribution. Requires BOTH
  ``--run`` AND ``--i-approve-paid-llm-calls`` to be passed together;
  either one alone is a no-op that prints the cost estimate and a
  warning, then exits non-zero. ``--estimate`` and ``--run`` are
  mutually exclusive — passing both exits 2 before anything else runs
  (an explicit offline request must never resolve into a paid call).
  This mirrors the two-switch pattern the project uses everywhere a
  paid external API is one command away from being invoked accidentally
  in a batch/loop context.

The engine measured by ``--run`` is resolved through the SAME
``llm_registry`` routing a production Studio zh->en job uses
(``_service_mode="studio"``, task=translate: flat default ``deepseek``,
``admin_settings.json::prompt_models["studio"]["translate"]`` override
wins when set, registry fallback chain applies). The effective route is
printed at run start and recorded in the report so the operator can
verify what was actually measured.

MEASUREMENT IS CONSTRAINT-NEUTRALIZED: the run path calls
``GeminiTranslator.translate_probe`` (the pipeline's purpose-built
constraint-free translation entry), NOT the regular ``translate()``.
The regular path derives per-segment ``min_chars~max_chars`` from the
very ``natural_length_ratio`` being calibrated and injects them into
the prompt as hard constraints — measuring through it would be
circular (a wrong 0.55 would still read back as ~0.55). The probe
prompt is the same dubbing-translation family, same s3_translate
routing, but carries no numeric length anchor at all, so the measured
ratio is the NATURAL unconstrained length ratio the v3 plan asks for.
See run_calibration's docstring for the full rationale.

Usage:
    # Offline cost estimate only (default; safe to run anytime).
    python scripts/calibrate_zh_en_ratio.py --corpus data/zh_clips

    # Explicit estimate mode (same as above).
    python scripts/calibrate_zh_en_ratio.py --corpus data/zh_clips --estimate

    # Real paid run — owner must pass BOTH switches explicitly.
    python scripts/calibrate_zh_en_ratio.py --corpus data/zh_clips \\
        --run --i-approve-paid-llm-calls

Corpus format — a directory containing EITHER:
    * ``*.json`` pipeline transcript/segments artifacts, each holding a
      ``segments`` or ``lines`` array of objects with a ``text`` /
      ``source_text`` / ``cn_text`` string key (one array item = one
      spoken line); or
    * ``*.txt`` plain-text files, one source line per file line (blank
      lines skipped).

Each file in the corpus directory is treated as one "clip".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# ---------------------------------------------------------------------------
# Cost estimate constants (documented source + date — keep these in sync with
# the model the pipeline actually uses, services.gemini.translator.DEFAULT_MODEL_NAME).
# ---------------------------------------------------------------------------

#: Must match services.gemini.translator.DEFAULT_MODEL_NAME. Checked via a
#: guard test (tests/test_cm03_calibration_guard.py) so this constant can't
#: silently drift from the translator's constructor default. NOTE: this is
#: the --estimate PRICING anchor (worst case) and the constructor default —
#: the engine that actually translates in --run mode is decided by
#: llm_registry routing (mode=studio, task=translate; flat default deepseek,
#: admin override may apply), exactly like production Studio zh->en. The
#: real route is printed at run time and recorded in the report.
CALIBRATION_MODEL_NAME = "gemini-3.1-pro-preview"

#: USD per 1,000,000 tokens. Source: public pricing trackers (pricepertoken.com,
#: devtk.ai) for gemini-3.1-pro-preview, checked 2026-07-02. Standard
#: (non-batch, <=200k context) rates are used deliberately as a conservative
#: UPPER bound — batch mode is ~50% cheaper, so real spend should come in at
#: or below this estimate.
GEMINI_INPUT_USD_PER_MILLION_TOKENS = 2.00
GEMINI_OUTPUT_USD_PER_MILLION_TOKENS = 12.00

#: Conservative token/character heuristics (NOT the real tokenizer — this
#: script never loads one, to keep --estimate fully offline). CJK ideographs
#: tend to map close to 1 token/char in Gemini's tokenizer (upper bound: some
#: are sub-token, none are >1); Latin text is ~4 chars/token on average
#: English prose. Both choices bias the estimate UP (more tokens => higher
#: cost) so this table over-, not under-, quotes.
CJK_CHARS_PER_TOKEN = 1.0
LATIN_CHARS_PER_TOKEN = 4.0

#: A translation prompt carries substantial fixed overhead beyond the raw
#: source text (glossary, instructions, JSON schema, few-shot scaffolding).
#: This multiplier inflates the raw-source-text token estimate to account for
#: that fixed cost per API call; conservative (i.e. deliberately generous).
PROMPT_OVERHEAD_MULTIPLIER = 3.0

#: English (target) output tends to run longer than the ratio would suggest
#: once JSON structure / field names / retries are included. Applied to the
#: *source* token estimate to project a conservative output token count.
OUTPUT_TOKEN_MULTIPLIER = 1.5

#: The provisional ratio currently in language_registry.py. Duplicated here
#: (rather than imported) so --estimate can run without importing anything
#: from services.language_registry that might transitively require network
#: config. Cross-checked against the registry by a guard test.
CURRENT_PROVISIONAL_RATIO = 0.55

#: If the measured pooled p50 ratio deviates from CURRENT_PROVISIONAL_RATIO by
#: more than this fraction, the report recommends updating the constant
#: instead of keeping it.
RATIO_DEVIATION_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# Corpus loading (pure offline — no paid API calls anywhere in this section)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipLine:
    """One spoken source line extracted from a corpus clip."""

    text: str


@dataclass(frozen=True)
class Clip:
    """One corpus file (one pipeline transcript JSON, or one .txt file)."""

    name: str
    lines: list[ClipLine]

    @property
    def source_text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _extract_text_from_line_payload(payload: object) -> str | None:
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    if not isinstance(payload, dict):
        return None
    for key in ("text", "source_text", "cn_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _load_json_clip(path: Path) -> Clip | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] skipping unreadable JSON corpus file {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"[warn] skipping {path}: top-level JSON is not an object", file=sys.stderr)
        return None
    raw_lines: list[object] = []
    for key in ("segments", "lines"):
        value = payload.get(key)
        if isinstance(value, list):
            raw_lines = value
            break
    lines: list[ClipLine] = []
    for item in raw_lines:
        text = _extract_text_from_line_payload(item)
        if text:
            lines.append(ClipLine(text=text))
    if not lines:
        print(
            f"[warn] skipping {path}: no usable text lines found (segments[]/lines[] with text/source_text/cn_text)",
            file=sys.stderr,
        )
        return None
    return Clip(name=path.name, lines=lines)


def _load_txt_clip(path: Path) -> Clip | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[warn] skipping unreadable text corpus file {path}: {exc}", file=sys.stderr)
        return None
    lines = [ClipLine(text=line.strip()) for line in raw.splitlines() if line.strip()]
    if not lines:
        print(f"[warn] skipping {path}: no non-blank lines", file=sys.stderr)
        return None
    return Clip(name=path.name, lines=lines)


def load_corpus(corpus_dir: Path) -> list[Clip]:
    """Load every ``*.json`` / ``*.txt`` file in ``corpus_dir`` as one clip.

    Pure offline — does not import or touch anything network-related.
    Files that fail to parse are skipped with a warning (recorded on
    stderr); the caller decides whether an empty result is fatal.
    """
    if not corpus_dir.is_dir():
        raise FileNotFoundError(f"--corpus directory does not exist: {corpus_dir}")
    clips: list[Clip] = []
    for path in sorted(corpus_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            clip = _load_json_clip(path)
        elif path.suffix.lower() == ".txt":
            clip = _load_txt_clip(path)
        else:
            continue
        if clip is not None:
            clips.append(clip)
    return clips


def count_cjk_chars(text: str) -> int:
    """Count CJK ideographs. Delegates to the SAME per-ideograph range check
    the pipeline uses (services.gemini.translator._count_source_words /
    src/pipeline/process.py PipelineRunner._count_source_words), so the
    calibration corpus stats and the pipeline's own source-word counting
    never silently diverge. Re-implemented inline (not imported) because the
    upstream helper is a module-private/static function bundled with heavy
    translator-module import machinery that --estimate must avoid; the guard
    test pins byte-identical behavior against the real pipeline helper.
    """
    return sum(1 for ch in (text or "") if "一" <= ch <= "鿿")


# ---------------------------------------------------------------------------
# --estimate mode: offline cost projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusStats:
    clip_count: int
    segment_count: int
    total_cjk_chars: int
    total_chars: int


def compute_corpus_stats(clips: list[Clip]) -> CorpusStats:
    segment_count = sum(len(clip.lines) for clip in clips)
    total_cjk_chars = sum(count_cjk_chars(clip.source_text) for clip in clips)
    total_chars = sum(len(clip.source_text) for clip in clips)
    return CorpusStats(
        clip_count=len(clips),
        segment_count=segment_count,
        total_cjk_chars=total_cjk_chars,
        total_chars=total_chars,
    )


@dataclass(frozen=True)
class CostEstimate:
    input_tokens: int
    output_tokens: int
    input_usd: float
    output_usd: float

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd


def estimate_cost(stats: CorpusStats) -> CostEstimate:
    # Non-CJK characters in the corpus (punctuation, numbers, stray Latin
    # terms) are billed at the more expensive Latin chars/token rate as a
    # conservative choice (fewer chars per token => more tokens => higher $).
    non_cjk_chars = max(0, stats.total_chars - stats.total_cjk_chars)
    raw_source_tokens = stats.total_cjk_chars / CJK_CHARS_PER_TOKEN + non_cjk_chars / LATIN_CHARS_PER_TOKEN
    input_tokens = int(raw_source_tokens * PROMPT_OVERHEAD_MULTIPLIER)
    output_tokens = int(raw_source_tokens * OUTPUT_TOKEN_MULTIPLIER)
    input_usd = input_tokens / 1_000_000 * GEMINI_INPUT_USD_PER_MILLION_TOKENS
    output_usd = output_tokens / 1_000_000 * GEMINI_OUTPUT_USD_PER_MILLION_TOKENS
    return CostEstimate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_usd=input_usd,
        output_usd=output_usd,
    )


def format_estimate_report(corpus_dir: Path, stats: CorpusStats, cost: CostEstimate) -> str:
    lines = [
        "=" * 72,
        "CM-03 zh->en calibration -- OFFLINE cost estimate (no API calls made)",
        "=" * 72,
        f"corpus dir       : {corpus_dir}",
        f"clips            : {stats.clip_count}",
        f"segments (lines) : {stats.segment_count}",
        f"CJK chars total  : {stats.total_cjk_chars}",
        f"all chars total  : {stats.total_chars}",
        "-" * 72,
        f"pricing model    : {CALIBRATION_MODEL_NAME} (conservative UPPER bound)",
        f"pricing source   : public pricing trackers, checked 2026-07-02 "
        f"(${GEMINI_INPUT_USD_PER_MILLION_TOKENS:.2f}/M in, "
        f"${GEMINI_OUTPUT_USD_PER_MILLION_TOKENS:.2f}/M out; standard non-batch rate)",
        "actual engine    : decided at --run time by llm_registry routing (mode=studio, task=translate;",
        "                   flat default is deepseek -- far cheaper than the Gemini rate above -- and an",
        "                   admin override in admin_settings.json may apply). The real model is printed",
        "                   by --run and recorded in the report; this table prices the WORST case.",
        f"est. input tokens  : ~{cost.input_tokens:,}",
        f"est. output tokens : ~{cost.output_tokens:,}",
        f"est. input cost    : ${cost.input_usd:.4f}",
        f"est. output cost   : ${cost.output_usd:.4f}",
        f"est. TOTAL cost    : ${cost.total_usd:.4f}",
        "-" * 72,
        "This is a PURE OFFLINE estimate. No LLM/network call has been made.",
        "To run the real paid calibration against this corpus, the owner must",
        "explicitly pass BOTH switches:",
        "",
        f"    python scripts/calibrate_zh_en_ratio.py --corpus {corpus_dir} --run --i-approve-paid-llm-calls",
        "",
        "=" * 72,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --run mode: real (paid) calibration. Only reachable behind the double gate
# in main(); nothing above this point imports GeminiTranslator.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipRatioResult:
    clip_name: str
    segment_count: int
    source_cjk_chars: int
    target_word_count: int
    ratio: float | None  # None when source had 0 CJK chars (skip from stats)
    error: str | None = None


def _build_transcript_lines(clip: Clip) -> list[Any]:
    """Build services.assemblyai.transcriber.TranscriptLine objects from a
    Clip so we can feed the same translate() entry point the pipeline uses.
    Import is local to keep this function unreachable from --estimate."""
    from services.assemblyai.transcriber import TranscriptLine

    now_ms = 0
    lines = []
    for idx, clip_line in enumerate(clip.lines):
        # Duration/timing is irrelevant for calibration (we only need the
        # translated text back); use a nominal per-line span so translate()'s
        # internal duration-budget math doesn't divide by zero.
        start_ms = now_ms
        end_ms = start_ms + 5_000
        now_ms = end_ms
        lines.append(
            TranscriptLine(
                index=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id="speaker_a",
                speaker_label="A",
                source_text=clip_line.text,
            )
        )
    return lines


#: The service mode whose LLM routing the calibration must mirror. zh->en
#: first release is Studio-only (v3 plan §6 DoD), so the ratio must be
#: measured on the exact engine a production Studio zh->en job would use.
CALIBRATION_SERVICE_MODE = "studio"


def _build_translator(api_key: str) -> Any:
    """Construct GeminiTranslator the same way process.py does (translator_kwargs
    shape mirrored from src/pipeline/process.py:3293-3311), minus the pipeline
    plumbing (usage meter / prompt overrides) this standalone calibration run
    doesn't need."""
    from services.gemini.translator import GeminiTranslator

    translator = GeminiTranslator(
        api_key=api_key,
        model_name=CALIBRATION_MODEL_NAME,
    )
    # Mirror process.py:3312 — `translator._service_mode = job_service_mode`.
    # This is what activates llm_registry-based model selection inside
    # _call_task_with_fallback (task s3_translate -> prompt_key "translate"),
    # i.e. the flat default `deepseek` plus any admin override in
    # admin_settings.json::prompt_models["studio"]["translate"] plus the
    # registry fallback chain. WITHOUT this line the translator would take the
    # legacy Gemini path and the calibration would measure a DIFFERENT engine
    # than production Studio zh->en (CodeX P1 finding, 2026-07-02).
    translator._service_mode = CALIBRATION_SERVICE_MODE
    # Mirror process.py:3317-3318 — seed the language pair on the translator
    # instance itself so _count_cn_chars() (target-script-aware counting)
    # dispatches to the Latin/word-count branch instead of the CJK default.
    translator._translate_source_language = "zh-CN"
    translator._translate_target_language = "en"
    return translator


def _resolve_effective_translate_route() -> dict[str, Any]:
    """Resolve the SAME task=translate routing a production Studio zh->en job
    would get, so the operator can verify which engine the calibration is
    actually measuring. Imported lazily — only the --run path calls this."""
    from services.llm_registry import (
        MODEL_REGISTRY,
        get_fallback_candidates,
        get_prompt_model,
        resolve_model_id,
    )

    model_name = get_prompt_model(CALIBRATION_SERVICE_MODE, "translate")
    info = MODEL_REGISTRY.get(model_name, {})
    fallbacks = get_fallback_candidates(model_name, requires_audio=False)
    return {
        "service_mode": CALIBRATION_SERVICE_MODE,
        "prompt_key": "translate",
        "model_name": model_name,
        "api_model_id": resolve_model_id(model_name),
        "provider": str(info.get("provider", "")),
        "api_key_env": str(info.get("api_key_env", "")),
        "fallback_candidates": list(fallbacks),
    }


#: Probe batch size. translate_probe's design envelope is "probe batches are
#: small (<=10 segments)" (translator.py docstring) — one prompt per batch, no
#: checkpointing, no length retry. Clips longer than this are chunked.
PROBE_BATCH_SIZE = 10


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def run_calibration(corpus_dir: Path, clips: list[Clip], output_dir: Path) -> int:
    """Real paid-API calibration run. Only called after the double-switch
    gate in main() has been checked. Returns process exit code.

    MEASUREMENT ENTRY = ``GeminiTranslator.translate_probe`` (constraint-free),
    NOT the regular ``translate()`` (@codex P1, PR #99): the regular path
    derives ``target_chars = source_word_count x natural_length_ratio`` and
    injects ``min_chars~max_chars`` (a +/-15% band around it) into the prompt
    as HARD constraints, plus a length-retry gate on the same band — i.e. the
    very 0.55 prior being calibrated would clamp the measurement (circular:
    a wrong 0.55 would still report "maintain 0.55"). No finite patched ratio
    can neutralize that band (min scales with target: tiny ratio collapses to
    a 1-word floor, huge ratio forces overlong output), so instead of
    patching, calibration uses the pipeline's own purpose-built unconstrained
    branch: ``_build_probe_groups`` "deliberately omit[s] min_chars/max_chars
    to avoid the ... assumption polluting the calibration. The LLM translates
    by feel" — same dubbing prompt family (dedicated zh-CN->en probe
    template, no numeric length anchor at all), same s3_translate task, and
    therefore the same llm_registry routing mirrored by _build_translator.
    What this measures is the v3-plan sense of "natural translation length
    ratio": the unconstrained natural output length, as opposed to
    production's constrained output (which is clamped by the ratio by
    design).
    """
    import os

    from services.gemini.translator import _count_source_words
    from services.language_registry import LANG_EN, LANG_ZH_CN
    from services.llm_registry import get_api_key

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print(
            "[error] GEMINI_API_KEY is not set. GeminiTranslator requires it at "
            "construction time (and it backs the registry fallback chain).",
            file=sys.stderr,
        )
        return 1

    # Resolve and surface the EXACT engine this run will use — the same
    # llm_registry routing (mode=studio, task=translate, admin overrides
    # included) a production Studio zh->en job resolves (CodeX P1).
    route = _resolve_effective_translate_route()
    print(
        "[route] task=translate service_mode={service_mode} -> model={model_name} "
        "(api_model_id={api_model_id}, provider={provider}); fallbacks={fallback_candidates}".format(**route)
    )
    if route["provider"] and route["provider"] != "gemini" and not get_api_key(route["model_name"]):
        print(
            f"[error] the effective translate route is {route['model_name']} "
            f"(provider={route['provider']}) but its API key is not configured "
            f"(check {route['api_key_env']} or admin settings). Refusing to start "
            "a run that would fail per-clip.",
            file=sys.stderr,
        )
        return 1

    translator = _build_translator(api_key)

    clip_results: list[ClipRatioResult] = []
    failed_clips: list[tuple[str, str]] = []

    for clip in clips:
        try:
            lines = _build_transcript_lines(clip)
            segments = []
            # Chunk to translate_probe's design envelope (<=10 segments per
            # prompt); each batch is one constraint-free probe-translation
            # call through the production s3_translate routing.
            for batch in _batched(lines, PROBE_BATCH_SIZE):
                segments.extend(
                    translator.translate_probe(
                        batch,
                        source_language=LANG_ZH_CN,
                        target_language=LANG_EN,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - record and continue, never abort silently
            print(f"[error] clip {clip.name} failed: {exc}", file=sys.stderr)
            failed_clips.append((clip.name, str(exc)))
            clip_results.append(
                ClipRatioResult(
                    clip_name=clip.name,
                    segment_count=len(clip.lines),
                    source_cjk_chars=count_cjk_chars(clip.source_text),
                    target_word_count=0,
                    ratio=None,
                    error=str(exc),
                )
            )
            continue

        source_cjk_chars = _count_source_words(clip.source_text, source_script="cjk")
        target_text = "\n".join(seg.cn_text for seg in segments)
        target_word_count = translator._count_cn_chars(target_text)
        ratio = (target_word_count / source_cjk_chars) if source_cjk_chars > 0 else None
        clip_results.append(
            ClipRatioResult(
                clip_name=clip.name,
                segment_count=len(clip.lines),
                source_cjk_chars=source_cjk_chars,
                target_word_count=target_word_count,
                ratio=ratio,
            )
        )

    if len(failed_clips) == len(clip_results) and clip_results:
        print(
            f"[fatal] all {len(clip_results)} clip(s) failed calibration translation; no ratio data produced.",
            file=sys.stderr,
        )
        _write_reports(corpus_dir, clip_results, output_dir, route=route, fatal=True)
        return 1

    _write_reports(corpus_dir, clip_results, output_dir, route=route, fatal=False)
    return 0


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def _pct(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        k = (len(ordered) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(ordered) - 1)
        frac = k - lo
        return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

    return {
        "p10": _pct(0.10),
        "p25": _pct(0.25),
        "p50": _pct(0.50),
        "p75": _pct(0.75),
        "p90": _pct(0.90),
        "mean": statistics.fmean(ordered),
        "n": float(len(ordered)),
    }


def _write_reports(
    corpus_dir: Path,
    clip_results: list[ClipRatioResult],
    output_dir: Path,
    *,
    route: dict[str, Any],
    fatal: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    pooled_ratios = [r.ratio for r in clip_results if r.ratio is not None]
    pooled_stats = _percentiles(pooled_ratios)
    per_clip_stats = {r.clip_name: _percentiles([r.ratio]) if r.ratio is not None else {} for r in clip_results}
    failed = [r for r in clip_results if r.error is not None]

    pooled_p50 = pooled_stats.get("p50")
    if pooled_p50 is None:
        conclusion = "insufficient_data"
        recommendation = "No successful clips produced a ratio; cannot conclude. Re-run with a valid corpus."
    else:
        deviation = abs(pooled_p50 - CURRENT_PROVISIONAL_RATIO) / CURRENT_PROVISIONAL_RATIO
        if deviation <= RATIO_DEVIATION_THRESHOLD:
            conclusion = "maintain_0.55"
            recommendation = (
                f"Pooled p50 ({pooled_p50:.2f}) is within "
                f"{RATIO_DEVIATION_THRESHOLD:.0%} of the provisional 0.55 -- "
                "recommend MAINTAINING natural_length_ratio=0.55."
            )
        else:
            suggested = round(pooled_p50, 2)
            conclusion = "update_ratio"
            recommendation = (
                f"Pooled p50 ({pooled_p50:.2f}) deviates from provisional 0.55 by "
                f"{deviation:.0%} (> {RATIO_DEVIATION_THRESHOLD:.0%} threshold) -- "
                f"recommend UPDATING natural_length_ratio to {suggested}. This "
                "affects two downstream consumers: (1) translator.py length "
                "budget (_estimate_dynamic_target_chars / _count_cn_chars retry "
                "gate) and (2) process.py voice-speed cps metadata "
                "(target_chars_per_second)."
            )

    report: dict[str, Any] = {
        "generated_at_utc": timestamp,
        "corpus_dir": str(corpus_dir),
        # The engine that ACTUALLY produced the translations — resolved from
        # the same llm_registry routing production Studio zh->en uses
        # (admin overrides included). The Gemini model constant above it is
        # only the constructor default / estimate pricing anchor.
        "effective_translate_route": route,
        # @codex P1 (PR #99): the measurement entry is translate_probe, whose
        # prompt carries NO length constraints (no target_chars / min_chars /
        # max_chars derived from the 0.55 prior) — the measured ratio is the
        # NATURAL, unconstrained output length, not production's
        # ratio-clamped output. See run_calibration docstring.
        "constraint_neutralized": True,
        "measurement_entry": "GeminiTranslator.translate_probe",
        "length_constraint_mode": "none (probe template omits target_chars/min_chars/max_chars entirely)",
        "estimate_pricing_model": CALIBRATION_MODEL_NAME,
        "current_provisional_ratio": CURRENT_PROVISIONAL_RATIO,
        "deviation_threshold": RATIO_DEVIATION_THRESHOLD,
        "clip_count": len(clip_results),
        "failed_clip_count": len(failed),
        "failed_clips": [{"clip_name": r.clip_name, "error": r.error} for r in failed],
        "pooled_stats": pooled_stats,
        "per_clip_stats": per_clip_stats,
        "clip_results": [
            {
                "clip_name": r.clip_name,
                "segment_count": r.segment_count,
                "source_cjk_chars": r.source_cjk_chars,
                "target_word_count": r.target_word_count,
                "ratio": r.ratio,
                "error": r.error,
            }
            for r in clip_results
        ],
        "conclusion": conclusion,
        "recommendation": recommendation,
        "fatal": fatal,
    }

    json_path = output_dir / f"{timestamp}-cm03-zh-en-ratio-calibration.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    md_lines = [
        "# CM-03 zh->en `natural_length_ratio` calibration report",
        "",
        f"- Generated: {timestamp}",
        f"- Corpus: `{corpus_dir}`",
        "- Effective translate route (same llm_registry routing as production "
        f"Studio zh->en, admin overrides included): `{route['model_name']}` "
        f"(api_model_id=`{route['api_model_id']}`, provider=`{route['provider']}`, "
        f"fallbacks={route['fallback_candidates']})",
        "- Measurement entry: `GeminiTranslator.translate_probe` — "
        "**constraint-neutralized**: the probe prompt carries no "
        "target_chars/min_chars/max_chars (which in the regular translate() "
        "path are derived from the very 0.55 prior under calibration and "
        "injected as hard constraints). The measured ratio below is the "
        "NATURAL unconstrained length ratio (v3 plan semantics); production "
        "output is additionally clamped to +/-15% of the ratio by design.",
        f"- Clips: {len(clip_results)} ({len(failed)} failed)",
        "",
        "## Pooled ratio distribution (target word count / source CJK char count)",
        "",
    ]
    if pooled_stats:
        md_lines.append(
            "| n | p10 | p25 | p50 | p75 | p90 | mean |\n"
            "|---|---|---|---|---|---|---|\n"
            f"| {int(pooled_stats['n'])} | {pooled_stats['p10']:.3f} | "
            f"{pooled_stats['p25']:.3f} | {pooled_stats['p50']:.3f} | "
            f"{pooled_stats['p75']:.3f} | {pooled_stats['p90']:.3f} | "
            f"{pooled_stats['mean']:.3f} |"
        )
    else:
        md_lines.append("(no successful clips -- no ratio data)")
    md_lines += [
        "",
        "## Per-clip",
        "",
        "| clip | segments | source CJK chars | target words | ratio | error |",
        "|---|---|---|---|---|---|",
    ]
    for r in clip_results:
        ratio_str = f"{r.ratio:.3f}" if r.ratio is not None else "-"
        error_str = r.error or ""
        md_lines.append(
            f"| {r.clip_name} | {r.segment_count} | {r.source_cjk_chars} | "
            f"{r.target_word_count} | {ratio_str} | {error_str} |"
        )
    md_lines += [
        "",
        "## Conclusion",
        "",
        f"**{conclusion}**",
        "",
        recommendation,
        "",
        "### Impact on downstream consumers if the ratio changes",
        "",
        "1. Length budget: `services/gemini/translator.py` "
        "`_estimate_dynamic_target_chars` (5 call sites) and the "
        "`_count_cn_chars` retry gate consume `natural_length_ratio` to size "
        "the translation length budget per segment.",
        "2. Voice-speed cps: `src/pipeline/process.py` derives "
        "`target_chars_per_second` (DubbingSegment) from the source "
        "words/second times the ratio; zh->en currently ships with the speed "
        "dimension explicitly DISABLED (plan Phase 4 point 2), so this "
        "consumer is dormant until that is revisited.",
        "",
    ]
    md_path = output_dir / f"{timestamp}-cm03-zh-en-ratio-calibration.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"[ok] wrote {json_path}")
    print(f"[ok] wrote {md_path}")
    print(f"[ok] conclusion: {conclusion}")
    print(recommendation)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CM-03 zh->en natural_length_ratio calibration (offline estimate by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--corpus",
        required=True,
        type=Path,
        help="Directory of corpus clips: pipeline transcript JSON (segments[]/lines[] "
        "with text/source_text/cn_text) or plain .txt files (one source line per line).",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Offline cost estimate only. This is the default behavior; the flag exists "
        "for explicitness in scripted/documented invocations. Mutually exclusive "
        "with --run (giving both exits 2 without any paid call).",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Actually call the paid Gemini translation API and compute the real ratio. "
        "MUST be combined with --i-approve-paid-llm-calls.",
    )
    parser.add_argument(
        "--i-approve-paid-llm-calls",
        dest="i_approve_paid_llm_calls",
        action="store_true",
        help="Explicit second switch required alongside --run. Confirms the operator has "
        "seen the cost estimate and approves the paid API spend.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "reports",
        help="Directory to write the JSON + Markdown calibration report to (--run mode only). Default: docs/reports.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Fail-closed (CodeX P2): --estimate is an explicit statement of "offline
    # only" intent — combining it with --run is contradictory and must never
    # resolve in favor of the paid branch. Checked FIRST, before any corpus
    # I/O, so the contradiction can't reach run_calibration regardless of
    # what other flags are present.
    if args.estimate and args.run:
        print(
            "[blocked] --estimate and --run are mutually exclusive. --estimate "
            "explicitly requests the offline path; refusing to enter the paid "
            "run. Drop --estimate if you intend a real calibration run.",
            file=sys.stderr,
        )
        return 2

    try:
        clips = load_corpus(args.corpus)
    except FileNotFoundError as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1

    if not clips:
        print(
            f"[fatal] no usable clips found under {args.corpus} (expected *.json pipeline transcripts or *.txt files).",
            file=sys.stderr,
        )
        return 1

    stats = compute_corpus_stats(clips)
    cost = estimate_cost(stats)

    # --run requires BOTH switches. Any other combination (including --run
    # alone) falls through to the safe --estimate path plus a loud warning.
    if args.run and args.i_approve_paid_llm_calls:
        print(format_estimate_report(args.corpus, stats, cost))
        print()
        print(
            "[run] both --run and --i-approve-paid-llm-calls are set -- proceeding with the REAL paid calibration run."
        )
        return run_calibration(args.corpus, clips, args.output_dir)

    print(format_estimate_report(args.corpus, stats, cost))
    if args.run and not args.i_approve_paid_llm_calls:
        print(
            "\n[blocked] --run was passed WITHOUT --i-approve-paid-llm-calls. "
            "Refusing to spend money. Re-run with BOTH flags to execute the "
            "real calibration:\n"
            f"    python scripts/calibrate_zh_en_ratio.py --corpus {args.corpus} "
            "--run --i-approve-paid-llm-calls",
            file=sys.stderr,
        )
        return 2
    if args.i_approve_paid_llm_calls and not args.run:
        print(
            "\n[blocked] --i-approve-paid-llm-calls was passed WITHOUT --run. "
            "No paid call was made; add --run to execute.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
