#!/usr/bin/env python3
"""Run translation model quality benchmarks on prepared real-prompt samples.

The script performs live provider calls only when executed directly.  It is not
imported by the default application path and the tests use fake callers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from services.gemini.translator import GeminiTranslator, TranslationError
from services.llm_registry import MODEL_REGISTRY, get_api_key, resolve_model_id

RUN_VERSION = "translation_quality_benchmark.v1"
DEFAULT_SAMPLES_PATH = Path("reports/benchmark/translation_eval_samples_20260426.json")
DEFAULT_OUTPUT_PATH = Path("reports/benchmark/translation_quality_benchmark_run.json")
DEFAULT_MODELS = (
    "deepseek",
    "deepseek_v4_pro",
    "mimo_v25",
    "mimo_v25_pro",
    "gemini_31_flash_lite",
    "gemini_pro",
)
DEFAULT_JUDGE_MODEL = "gpt54"
NON_SPOKEN_CHAR_RE = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


class ModelCaller(Protocol):
    def call(self, model_name: str, prompt: str, *, json_mode: bool) -> str:
        ...


@dataclass(frozen=True)
class BenchmarkConfig:
    samples_path: Path = DEFAULT_SAMPLES_PATH
    output_path: Path = DEFAULT_OUTPUT_PATH
    models: tuple[str, ...] = DEFAULT_MODELS
    judge_model: str = DEFAULT_JUDGE_MODEL
    sample_limit: int | None = None
    sample_ids: tuple[str, ...] = ()
    skip_judge: bool = False
    force: bool = False


class RegistryModelCaller:
    """Live model caller using the project's existing registry dispatch."""

    def __init__(self) -> None:
        self._text_translator: GeminiTranslator | None = None
        self._gemini_translator: GeminiTranslator | None = None

    def call(self, model_name: str, prompt: str, *, json_mode: bool) -> str:
        provider = MODEL_REGISTRY.get(model_name, {}).get("provider")
        if provider == "openai":
            return self._call_openai(model_name, prompt, json_mode=json_mode)
        translator = self._translator_for_provider(provider)
        return translator._call_by_model(model_name, prompt, json_mode=json_mode)

    def _translator_for_provider(self, provider: str | None) -> GeminiTranslator:
        if provider == "gemini":
            if self._gemini_translator is None:
                api_key = (
                    os.environ.get("GEMINI_API_KEY", "").strip()
                    or os.environ.get("VERTEX_AI_EXPRESS_KEY", "").strip()
                    or "benchmark-key"
                )
                self._gemini_translator = GeminiTranslator(api_key)
            return self._gemini_translator
        if self._text_translator is None:
            self._text_translator = GeminiTranslator("benchmark-key", _skip_init=True)
        return self._text_translator

    @staticmethod
    def _call_openai(model_name: str, prompt: str, *, json_mode: bool) -> str:
        info = MODEL_REGISTRY.get(model_name, {})
        api_key = get_api_key(model_name) or ""
        if not api_key:
            raise TranslationError(f"{model_name} API key not configured")
        model_id = resolve_model_id(model_name)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if str(model_id).startswith("gpt-5"):
            payload["max_completion_tokens"] = 8192
        else:
            payload["max_tokens"] = 8192
        request_overrides = info.get("request_overrides")
        if isinstance(request_overrides, dict):
            payload.update(request_overrides)
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def spoken_char_count(text: str) -> int:
    return len(NON_SPOKEN_CHAR_RE.sub("", text or ""))


def strip_markdown_fence(text: str) -> str:
    normalized = text.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*```$", "", normalized)
    return normalized.strip()


def parse_translation_response(response_text: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(strip_markdown_fence(response_text))
    except json.JSONDecodeError as exc:
        return [], f"invalid_json: {exc}"
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        payload = payload["segments"]
    if not isinstance(payload, list):
        return [], "translation response must be a JSON array or {segments: [...]}"
    parsed: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            return [], "translation item must be an object"
        if "segment_id" not in item or "cn_text" not in item:
            return [], "translation item missing segment_id or cn_text"
        parsed.append({"segment_id": item["segment_id"], "cn_text": normalize_text(item["cn_text"])})
    return parsed, None


def _segment_id(value: Any) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def evaluate_constraints(sample: dict[str, Any], response_text: str) -> dict[str, Any]:
    parsed, error = parse_translation_response(response_text)
    groups = sample.get("groups", [])
    expected_ids = [_segment_id(group.get("segment_id")) for group in groups]
    by_id = {_segment_id(item.get("segment_id")): item for item in parsed}
    missing_ids = [segment_id for segment_id in expected_ids if segment_id not in by_id]
    extra_ids = [segment_id for segment_id in by_id if segment_id not in set(expected_ids)]

    segment_checks: list[dict[str, Any]] = []
    for group in groups:
        segment_id = _segment_id(group.get("segment_id"))
        item = by_id.get(segment_id)
        cn_text = normalize_text(item.get("cn_text")) if item else ""
        chars = spoken_char_count(cn_text)
        min_chars = int(group.get("min_chars") or 0)
        max_chars = int(group.get("max_chars") or 0)
        in_range = bool(cn_text) and min_chars <= chars <= max_chars
        segment_checks.append(
            {
                "segment_id": segment_id,
                "spoken_chars": chars,
                "min_chars": min_chars,
                "max_chars": max_chars,
                "in_range": in_range,
            }
        )

    glossary = sample.get("glossary", {})
    source_text = " ".join(normalize_text(group.get("source_text")) for group in groups)
    output_text = "".join(item.get("cn_text", "") for item in parsed)
    glossary_checks: list[dict[str, Any]] = []
    if isinstance(glossary, dict):
        source_lower = source_text.lower()
        for source_term, target_term in glossary.items():
            source_term_text = normalize_text(source_term)
            target_term_text = normalize_text(target_term)
            if not source_term_text or not target_term_text:
                continue
            if source_term_text.lower() not in source_lower:
                continue
            glossary_checks.append(
                {
                    "source": source_term_text,
                    "target": target_term_text,
                    "present": target_term_text in output_text,
                }
            )

    format_ok = error is None and not missing_ids and not extra_ids
    range_pass_count = sum(1 for item in segment_checks if item["in_range"])
    glossary_pass_count = sum(1 for item in glossary_checks if item["present"])
    char_range_score = (
        100.0 * range_pass_count / len(segment_checks)
        if segment_checks
        else 100.0
    )
    glossary_score = (
        100.0 * glossary_pass_count / len(glossary_checks)
        if glossary_checks
        else 100.0
    )
    format_score = 100.0 if format_ok else (50.0 if error is None else 0.0)
    constraint_score = round(format_score * 0.40 + char_range_score * 0.35 + glossary_score * 0.25, 2)

    return {
        "format_ok": format_ok,
        "parse_error": error,
        "missing_segment_ids": missing_ids,
        "extra_segment_ids": extra_ids,
        "segment_checks": segment_checks,
        "char_range_pass_rate": round(char_range_score / 100.0, 4),
        "glossary_checks": glossary_checks,
        "glossary_pass_rate": round(glossary_score / 100.0, 4),
        "constraint_score": constraint_score,
        "parsed_segments": parsed,
    }


def build_judge_prompt(
    *,
    sample: dict[str, Any],
    model_name: str,
    response_text: str,
    constraints: dict[str, Any],
) -> str:
    judge_payload = {
        "sample_id": sample.get("sample_id"),
        "video_title": sample.get("video_title"),
        "selection_reasons": sample.get("selection_reasons", []),
        "glossary": sample.get("glossary", {}),
        "groups": sample.get("groups", []),
        "candidate_model": model_name,
        "candidate_output": response_text,
        "deterministic_checks": {
            "format_ok": constraints.get("format_ok"),
            "missing_segment_ids": constraints.get("missing_segment_ids"),
            "extra_segment_ids": constraints.get("extra_segment_ids"),
            "segment_checks": constraints.get("segment_checks"),
            "glossary_checks": constraints.get("glossary_checks"),
        },
        "reference_translations": sample.get("reference_translations", []),
    }
    return (
        "你是视频配音翻译质量评审。请根据英文源文、术语表、时长/字数约束和候选译文评分。\n"
        "参考译文只作为上下文参考，不是唯一标准；不要因为表达不同但准确自然就扣分。\n\n"
        "评分维度：\n"
        "- semantic_completeness: 0-25，核心语义是否完整、是否漏译/误译。\n"
        "- terminology: 0-15，术语表和专名是否遵循。\n"
        "- length_fit: 0-15，是否符合 min_chars/max_chars 和配音时长目标。\n"
        "- oral_naturalness: 0-15，中文是否自然、适合口播，不是字幕腔。\n"
        "- context_consistency: 0-10，跨 segment 上下文、代词、人物关系是否一致。\n"
        "- format_compliance: 0-10，JSON 格式、segment_id、字段是否合规。\n"
        "- tts_readiness: 0-10，是否适合后续 TTS 与少量重写。\n\n"
        "只输出 JSON 对象，不要 markdown。格式：\n"
        "{\n"
        '  "semantic_completeness": 0,\n'
        '  "terminology": 0,\n'
        '  "length_fit": 0,\n'
        '  "oral_naturalness": 0,\n'
        '  "context_consistency": 0,\n'
        '  "format_compliance": 0,\n'
        '  "tts_readiness": 0,\n'
        '  "quality_score": 0,\n'
        '  "major_issues": ["..."],\n'
        '  "brief_reason": "..."\n'
        "}\n\n"
        f"评测数据：\n{json.dumps(judge_payload, ensure_ascii=False, indent=2)}"
    )


def parse_judge_response(response_text: str) -> dict[str, Any]:
    payload = json.loads(strip_markdown_fence(response_text))
    if not isinstance(payload, dict):
        raise ValueError("judge response must be a JSON object")
    dims = [
        "semantic_completeness",
        "terminology",
        "length_fit",
        "oral_naturalness",
        "context_consistency",
        "format_compliance",
        "tts_readiness",
    ]
    for key in dims + ["quality_score"]:
        try:
            payload[key] = float(payload.get(key, 0))
        except (TypeError, ValueError):
            payload[key] = 0.0
    if not payload.get("quality_score"):
        payload["quality_score"] = sum(payload[key] for key in dims)
    payload["quality_score"] = max(0.0, min(100.0, float(payload["quality_score"])))
    return payload


def _model_meta(model_name: str) -> dict[str, Any]:
    info = MODEL_REGISTRY.get(model_name, {})
    return {
        "model": model_name,
        "api_model_id": resolve_model_id(model_name),
        "provider": info.get("provider", ""),
        "label": info.get("label", model_name),
        "cost_hint": info.get("cost_hint", ""),
        "cost_rank": info.get("cost_rank", 99),
    }


def select_samples(payload: dict[str, Any], *, limit: int | None, sample_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    samples = list(payload.get("samples", []))
    if sample_ids:
        wanted = set(sample_ids)
        samples = [sample for sample in samples if sample.get("sample_id") in wanted]
    if limit is not None:
        samples = samples[: max(0, limit)]
    return samples


def run_single_translation(
    *,
    caller: ModelCaller,
    sample: dict[str, Any],
    model_name: str,
) -> dict[str, Any]:
    prompt = str(sample["translation_prompt"])
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        response_text = caller.call(model_name, prompt, json_mode=False)
        latency_ms = round((time.monotonic() - started) * 1000)
        constraints = evaluate_constraints(sample, response_text)
        return {
            "sample_id": sample.get("sample_id"),
            "model": model_name,
            "model_meta": _model_meta(model_name),
            "status": "ok",
            "started_at": started_at,
            "latency_ms": latency_ms,
            "prompt_chars": len(prompt),
            "response_chars": len(response_text),
            "response_text": response_text,
            "constraints": constraints,
        }
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        return {
            "sample_id": sample.get("sample_id"),
            "model": model_name,
            "model_meta": _model_meta(model_name),
            "status": "error",
            "started_at": started_at,
            "latency_ms": latency_ms,
            "prompt_chars": len(prompt),
            "response_chars": 0,
            "response_text": "",
            "error": f"{type(exc).__name__}: {exc}",
            "constraints": {
                "format_ok": False,
                "constraint_score": 0.0,
                "char_range_pass_rate": 0.0,
                "glossary_pass_rate": 0.0,
            },
        }


def judge_translation(
    *,
    caller: ModelCaller,
    sample: dict[str, Any],
    result: dict[str, Any],
    judge_model: str,
) -> dict[str, Any]:
    if result.get("status") != "ok":
        return {"status": "skipped", "reason": "translation_failed"}
    prompt = build_judge_prompt(
        sample=sample,
        model_name=str(result["model"]),
        response_text=str(result["response_text"]),
        constraints=dict(result.get("constraints", {})),
    )
    started = time.monotonic()
    try:
        response_text = caller.call(judge_model, prompt, json_mode=True)
        latency_ms = round((time.monotonic() - started) * 1000)
        parsed = parse_judge_response(response_text)
        return {
            "status": "ok",
            "judge_model": judge_model,
            "judge_model_meta": _model_meta(judge_model),
            "latency_ms": latency_ms,
            "prompt_chars": len(prompt),
            "response_text": response_text,
            "parsed": parsed,
        }
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        return {
            "status": "error",
            "judge_model": judge_model,
            "latency_ms": latency_ms,
            "prompt_chars": len(prompt),
            "error": f"{type(exc).__name__}: {exc}",
            "parsed": {"quality_score": 0.0},
        }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_model[str(result["model"])].append(result)

    avg_latency_by_model: dict[str, float] = {}
    for model, items in by_model.items():
        ok_items = [item for item in items if item.get("status") == "ok"]
        if ok_items:
            avg_latency_by_model[model] = sum(float(item.get("latency_ms", 0)) for item in ok_items) / len(ok_items)
        else:
            avg_latency_by_model[model] = 0.0
    positive_latencies = [value for value in avg_latency_by_model.values() if value > 0]
    min_latency = min(positive_latencies) if positive_latencies else 0.0
    max_latency = max(positive_latencies) if positive_latencies else min_latency

    model_summaries: list[dict[str, Any]] = []
    for model, items in by_model.items():
        attempts = len(items)
        ok_items = [item for item in items if item.get("status") == "ok"]
        success_rate = len(ok_items) / attempts if attempts else 0.0
        constraint_avg = (
            sum(float(item.get("constraints", {}).get("constraint_score", 0.0)) for item in items) / attempts
            if attempts
            else 0.0
        )
        judge_items = [
            item.get("judge", {}).get("parsed", {})
            for item in ok_items
            if item.get("judge", {}).get("status") == "ok"
        ]
        quality_avg = (
            sum(float(item.get("quality_score", 0.0)) for item in judge_items) / len(judge_items)
            if judge_items
            else 0.0
        )
        avg_latency = avg_latency_by_model.get(model, 0.0)
        if avg_latency <= 0:
            speed_score = 0.0
        elif max_latency <= min_latency:
            speed_score = 100.0
        else:
            speed_score = max(40.0, 100.0 - 60.0 * (avg_latency - min_latency) / (max_latency - min_latency))
        reliability_score = success_rate * 100.0
        overall = (
            quality_avg * 0.65
            + constraint_avg * 0.15
            + speed_score * 0.10
            + reliability_score * 0.10
        )
        model_summaries.append(
            {
                **_model_meta(model),
                "attempts": attempts,
                "successes": len(ok_items),
                "success_rate": round(success_rate, 4),
                "avg_latency_ms": round(avg_latency),
                "avg_quality_score": round(quality_avg, 2),
                "avg_constraint_score": round(constraint_avg, 2),
                "speed_score": round(speed_score, 2),
                "reliability_score": round(reliability_score, 2),
                "overall_score": round(overall, 2),
                "error_count": attempts - len(ok_items),
            }
        )
    model_summaries.sort(key=lambda item: item["overall_score"], reverse=True)
    return {
        "models": model_summaries,
        "status_counts": dict(Counter(result.get("status", "unknown") for result in results)),
        "weights": {
            "judge_quality": 0.65,
            "deterministic_constraints": 0.15,
            "processing_speed": 0.10,
            "reliability": 0.10,
        },
    }


def run_benchmark(config: BenchmarkConfig, *, caller: ModelCaller | None = None) -> dict[str, Any]:
    if config.output_path.exists() and not config.force:
        raise FileExistsError(f"{config.output_path} exists; pass --force to overwrite")
    caller = caller or RegistryModelCaller()
    sample_payload = load_json(config.samples_path)
    samples = select_samples(sample_payload, limit=config.sample_limit, sample_ids=config.sample_ids)
    if not samples:
        raise ValueError("No samples selected")
    unknown_models = [model for model in (*config.models, config.judge_model) if model not in MODEL_REGISTRY]
    if unknown_models:
        raise ValueError(f"Unknown models: {unknown_models}")

    results: list[dict[str, Any]] = []
    for sample in samples:
        for model_name in config.models:
            result = run_single_translation(caller=caller, sample=sample, model_name=model_name)
            if not config.skip_judge:
                result["judge"] = judge_translation(
                    caller=caller,
                    sample=sample,
                    result=result,
                    judge_model=config.judge_model,
                )
            results.append(result)
            write_json(
                config.output_path,
                {
                    "version": RUN_VERSION,
                    "status": "running",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "config": config_to_json(config),
                    "sample_source": {
                        "path": config.samples_path.as_posix(),
                        "version": sample_payload.get("version"),
                        "prompt_sources": sample_payload.get("prompt_sources", {}),
                    },
                    "results": results,
                    "summary": summarize_results(results),
                },
            )

    payload = {
        "version": RUN_VERSION,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config": config_to_json(config),
        "sample_source": {
            "path": config.samples_path.as_posix(),
            "version": sample_payload.get("version"),
            "prompt_sources": sample_payload.get("prompt_sources", {}),
            "coverage": sample_payload.get("coverage", {}),
        },
        "results": results,
        "summary": summarize_results(results),
    }
    write_json(config.output_path, payload)
    write_markdown_report(config.output_path.with_suffix(".md"), payload)
    return payload


def config_to_json(config: BenchmarkConfig) -> dict[str, Any]:
    return {
        "samples_path": config.samples_path.as_posix(),
        "output_path": config.output_path.as_posix(),
        "models": list(config.models),
        "judge_model": config.judge_model,
        "sample_limit": config.sample_limit,
        "sample_ids": list(config.sample_ids),
        "skip_judge": config.skip_judge,
    }


def write_markdown_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Translation Quality Benchmark",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Judge: `{payload.get('config', {}).get('judge_model')}`",
        f"- Samples: `{len(set(result.get('sample_id') for result in payload.get('results', [])))}`",
        "",
        "## Model Ranking",
        "",
        "| Rank | Model | Overall | Quality | Constraints | Speed | Reliability | Avg Latency | Cost |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for rank, item in enumerate(payload.get("summary", {}).get("models", []), start=1):
        lines.append(
            "| {rank} | {label} (`{model}`) | {overall:.2f} | {quality:.2f} | {constraints:.2f} | {speed:.2f} | {reliability:.2f} | {latency} ms | {cost} |".format(
                rank=rank,
                label=str(item.get("label", "")).replace("|", "\\|"),
                model=item.get("model"),
                overall=float(item.get("overall_score", 0.0)),
                quality=float(item.get("avg_quality_score", 0.0)),
                constraints=float(item.get("avg_constraint_score", 0.0)),
                speed=float(item.get("speed_score", 0.0)),
                reliability=float(item.get("reliability_score", 0.0)),
                latency=item.get("avg_latency_ms", 0),
                cost=str(item.get("cost_hint", "")).replace("|", "\\|"),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> BenchmarkConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--judge", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--sample-limit", type=int, default=None)
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    models = tuple(item.strip() for item in args.models.split(",") if item.strip())
    sample_ids = tuple(item.strip() for item in args.sample_ids.split(",") if item.strip())
    return BenchmarkConfig(
        samples_path=args.samples,
        output_path=args.output,
        models=models,
        judge_model=args.judge,
        sample_limit=args.sample_limit,
        sample_ids=sample_ids,
        skip_judge=args.skip_judge,
        force=args.force,
    )


def main() -> int:
    payload = run_benchmark(parse_args())
    print(
        json.dumps(
            {
                "output": payload["config"]["output_path"],
                "summary": Path(payload["config"]["output_path"]).with_suffix(".md").as_posix(),
                "ranking": payload["summary"]["models"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
