"""Admin cost management surfaces for per-job LLM/TTS/voice-clone metering.

This module is intentionally read-only. Pipeline writes usage facts; Gateway
loads those facts, applies a versioned price catalog, and returns estimates
that can be recalculated when provider prices change.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import CreditsLedger, Job, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/costs", tags=["admin-costs"])

JOB_TTS_BUCKETS = {
    "probe_tts",
    "first_tts",
    "post_tts_resynth",
    "post_edit_resynth",
}

DEFAULT_PRICE_CATALOG: dict[str, Any] = {
    "version": "2026-05-31-cosyvoice-v35-mimo-voiceclone-costs",
    "currency": "RMB",
    # Retained for backward compat — ``_rate_to_rmb`` still honors
    # ``_per_million_usd`` fields if present (multiplies by this rate).
    # All new entries should use ``_per_million_rmb`` directly so the
    # admin cost view shows the same currency the user is billed in
    # (no exchange-rate drift between admin reporting and pricing
    # updates). USD fields kept for any legacy override files.
    "usd_to_rmb": 7.2,
    "notes": (
        "Gateway-side estimate catalog. Override with "
        "AVT_COST_PRICE_CATALOG_PATH when provider prices change. "
        "2026-05-18 audit: LLM rates switched from USD to direct RMB "
        "to match billing currency. 2026-05-20: Gemini 3.5 Flash added; "
        "2026-05-21: Gemini 3.1 Flash Lite preview migrated to the GA "
        "gemini-3.1-flash-lite endpoint. 2026-05-31: CosyVoice v3.5 "
        "TTS rates and free voice enrollment metering added."
    ),
    "llm": {
        # DeepSeek 直接 RMB 计价。把原 USD 报价乘以基准汇率 7.2
        # 一次性固化下来，避免「美元单价 × 浮动汇率」让 admin 视图
        # 跟服务商账单飘移。如未来 DeepSeek 改 RMB 官方挂牌价，
        # 直接覆盖这里即可。
        # https://api.deepseek.com/pricing
        "deepseek:deepseek-v4-flash": {
            "input_per_million_rmb": 1.008,
            "output_per_million_rmb": 2.016,
            "cached_input_per_million_rmb": 0.02016,
            "source": "deepseek_pricing_2026-04-29_pinned_at_72cny_per_usd",
        },
        "deepseek:deepseek-v4-pro": {
            "input_per_million_rmb": 3.132,
            "output_per_million_rmb": 6.264,
            "cached_input_per_million_rmb": 0.0261,
            "source": "deepseek_pricing_2026-04-29_pinned_at_72cny_per_usd",
        },
        # Current entries below are pinned to Google official 2026-05-19
        # standard pricing; audio understanding fallback uses 32 tokens/s.
        "gemini:gemini-3.1-pro-preview": {
            "input_per_million_rmb": 14.4,
            "output_per_million_rmb": 86.4,
            "audio_input_per_million_rmb": 14.4,
            "audio_tokens_per_second": 32,
            "source": "google_gemini_official_standard_le200k_tier_rmb_2026-05-20",
        },
        "gemini:gemini-3.5-flash": {
            "input_per_million_rmb": 10.8,
            "output_per_million_rmb": 64.8,
            "audio_input_per_million_rmb": 10.8,
            "cached_input_per_million_rmb": 1.08,
            "audio_tokens_per_second": 32,
            "source": "google_gemini_official_standard_rmb_2026-05-20",
        },
        "gemini:gemini-2.5-flash-lite": {
            "input_per_million_rmb": 0.72,
            "output_per_million_rmb": 2.88,
            "audio_input_per_million_rmb": 2.16,
            "audio_tokens_per_second": 32,
            "source": "google_gemini_official_rmb_2026-05-20",
        },
        "gemini:gemini-3.1-flash-lite": {
            "input_per_million_rmb": 1.80,
            "output_per_million_rmb": 10.80,
            "audio_input_per_million_rmb": 3.60,
            "audio_tokens_per_second": 32,
            "source": "google_gemini_official_ga_rmb_2026-05-21",
        },
        # Keep the retiring preview key so historical metering rows still
        # render with a configured rate after the runtime migrates to GA.
        "gemini:gemini-3.1-flash-lite-preview": {
            "input_per_million_rmb": 1.80,
            "output_per_million_rmb": 10.80,
            "audio_input_per_million_rmb": 3.60,
            "audio_tokens_per_second": 32,
            "source": "google_gemini_preview_history_compat_rmb_2026-05-21",
        },
        # MiMo (Xiaomi) — RMB-direct 官方按量价（2026-05-27 调价，2026-05-29 查官网
        # 确认）。官方文档未单列音频输入计费：多模态 audio 按通用 input token
        # 计价，因此 audio_input 与 input 同价（有据，非猜测）。音频 token 数量
        # 在 PR 2 采集真实 usage 前，沿用引擎默认 25 tokens/s 估算。
        # mimo_omni（已弃用）也解析到 mimo-v2.5，命中本条目。
        "mimo:mimo-v2.5": {
            "input_per_million_rmb": 1.0,
            "cached_input_per_million_rmb": 0.02,
            "output_per_million_rmb": 2.0,
            "audio_input_per_million_rmb": 1.0,
            "source": "xiaomi_mimo_pay_as_you_go_2026-05-27",
        },
        "mimo:mimo-v2.5-pro": {
            "input_per_million_rmb": 3.0,
            "cached_input_per_million_rmb": 0.025,
            "output_per_million_rmb": 6.0,
            "audio_input_per_million_rmb": 3.0,
            "source": "xiaomi_mimo_pay_as_you_go_2026-05-27",
        },
    },
    "tts": {
        # Provider billing chars are already normalized by pipeline.
        "minimax:speech-2.8-turbo": {
            "rmb_per_10k_billed_chars": 2.0,
            "source": "cost_metering_plan",
        },
        "minimax:speech-02-turbo": {
            "rmb_per_10k_billed_chars": 2.0,
            "source": "cost_metering_plan_alias",
        },
        "minimax:speech-2.8-hd": {
            "rmb_per_10k_billed_chars": 3.5,
            "source": "cost_metering_plan",
        },
        "minimax:speech-02-hd": {
            "rmb_per_10k_billed_chars": 3.5,
            "source": "cost_metering_plan_alias",
        },
        "cosyvoice:cosyvoice-v3-flash": {
            "rmb_per_10k_billed_chars": 1.0,
            "source": "aliyun_bailian_model_pricing_2026-05-31",
        },
        "cosyvoice:cosyvoice-v3.5-flash": {
            "rmb_per_10k_billed_chars": 0.8,
            "source": "aliyun_bailian_model_pricing_2026-05-31",
        },
        "cosyvoice:cosyvoice-v3.5-plus": {
            "rmb_per_10k_billed_chars": 1.5,
            "source": "aliyun_bailian_model_pricing_2026-05-31",
        },
        "volcengine:seed-tts-2.0": {
            "rmb_per_10k_billed_chars": 3.0,
            "source": "cost_metering_plan",
        },
        "volcengine:seed-tts-1.1": {
            "rmb_per_10k_billed_chars": 3.0,
            "source": "cost_metering_plan_alias",
        },
        # MiMo TTS — 官方 2026-05-27 公告"限时免费"、无失效日期（plan Phase 3）。
        # token-based billing 且当前免费，billed_chars 保持 0；用 promotional
        # 标记让 admin 成本页显示"限免"而非 missing_rate / 长期 0 成本误导。
        # 多个 key 覆盖事件解析的几种 model 取值（resolver 默认 mimo-tts +
        # 实际 v2.5/v2 名，以及免费版 voiceclone 模型名）。
        "mimo:mimo-tts": {
            "rmb_per_10k_billed_chars": 0.0,
            "promotional": True,
            "promotional_note": "限时免费，失效日期未知/待确认",
            "source": "xiaomi_mimo_limited_free_2026-05-27",
        },
        "mimo:mimo-v2.5-tts": {
            "rmb_per_10k_billed_chars": 0.0,
            "promotional": True,
            "promotional_note": "限时免费，失效日期未知/待确认",
            "source": "xiaomi_mimo_limited_free_2026-05-27",
        },
        "mimo:mimo-v2.5-tts-voiceclone": {
            "rmb_per_10k_billed_chars": 0.0,
            "promotional": True,
            "promotional_note": "限时免费，失效日期未知/待确认",
            "source": "xiaomi_mimo_limited_free_2026-05-27",
        },
        "mimo:mimo-v2-tts": {
            "rmb_per_10k_billed_chars": 0.0,
            "promotional": True,
            "promotional_note": "限时免费，失效日期未知/待确认",
            "source": "xiaomi_mimo_limited_free_2026-05-27",
        },
    },
    "voice_clone": {
        "minimax:voice_clone": {
            "rmb_per_clone": 9.9,
            "source": "minimax_paygo_pricing_voice_cloning_2026-05-05",
            "billing_policy": "charged on first T2A synthesis with cloned voice",
        },
        "cosyvoice:voice_clone": {
            "rmb_per_clone": 0.0,
            "source": "aliyun_bailian_cosyvoice_voice_enrollment_free_2026-05-31",
            "billing_policy": "cosyvoice voice enrollment is free; TTS is billed per character",
        },
    },
}


@dataclass
class LLMRow:
    provider: str
    model: str
    model_id: str
    task: str
    phase: str
    calls: int = 0
    success_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0
    cached_input_tokens: int = 0
    audio_input_seconds: float = 0.0
    audio_input_bytes: int = 0
    cost_rmb: float | None = None
    rate_status: str = "missing_rate"
    rate_source: str = ""


@dataclass
class TTSRow:
    provider: str
    model: str
    bucket: str
    calls: int = 0
    input_chars: int = 0
    billed_chars: int = 0
    duration_ms: int = 0
    included_in_job_cost: bool = True
    cost_rmb: float | None = None
    rate_status: str = "missing_rate"
    rate_source: str = ""


@dataclass
class VoiceCloneRow:
    provider: str
    model: str
    bucket: str
    calls: int = 0
    success_calls: int = 0
    billable_clones: int = 0
    source_audio_seconds: float = 0.0
    source_audio_bytes: int = 0
    selected_segment_count: int = 0
    cost_rmb: float | None = None
    rate_status: str = "missing_rate"
    rate_source: str = ""
    billing_policy: str = ""


@dataclass
class JobCostBreakdown:
    llm_rows: list[LLMRow] = field(default_factory=list)
    tts_rows: list[TTSRow] = field(default_factory=list)
    voice_clone_rows: list[VoiceCloneRow] = field(default_factory=list)
    events_count: int = 0
    has_usage_events: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RevenueEstimate:
    credits: int | None
    source: str
    point_price_rmb: float
    revenue_rmb: float | None


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if (getattr(user, "role", None) or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _norm_provider(value: object) -> str:
    raw = _norm(value)
    if raw in {"minimax_tts", "minimax_voice_clone"}:
        return "minimax"
    if raw in {"cosyvoice_tts", "cosyvoice_voice_clone", "dashscope", "aliyun"}:
        return "cosyvoice"
    if raw in {"volcengine_tts", "doubao"}:
        return "volcengine"
    return raw or "unknown"


def _norm_model(value: object) -> str:
    raw = _norm(value)
    aliases = {
        "speech-02-turbo": "speech-2.8-turbo",
        "speech-02-hd": "speech-2.8-hd",
    }
    return aliases.get(raw, raw or "unknown")


def _price_key(provider: object, model: object) -> str:
    return f"{_norm_provider(provider)}:{_norm_model(model)}"


def _default_model_for_provider(provider: str, service_model: str = "") -> str:
    if provider == "cosyvoice":
        return "cosyvoice-v3-flash"
    if provider == "volcengine":
        return "seed-tts-2.0" if service_model == "studio" else "seed-tts-1.1"
    if provider == "minimax":
        return "speech-2.8-turbo"
    if provider == "mimo":
        return "mimo-tts"
    return "unknown"


def _model_belongs_to_provider(provider: str, model: str) -> bool:
    if not model or model == "unknown":
        return False
    if provider == "minimax":
        return model.startswith("speech-")
    if provider == "cosyvoice":
        return model.startswith("cosyvoice")
    if provider == "volcengine":
        return model.startswith("seed-tts")
    if provider == "mimo":
        return model.startswith("mimo")
    return True


def _infer_cosyvoice_model_from_event(event: dict[str, Any]) -> str:
    for field in ("worker_target_model", "target_model", "selected_voice", "voice_id"):
        value = _norm(event.get(field))
        if not value:
            continue
        if value.startswith("cosyvoice-v3.5-plus"):
            return "cosyvoice-v3.5-plus"
        if value.startswith("cosyvoice-v3.5-flash"):
            return "cosyvoice-v3.5-flash"
    return ""


def _allocate_int(total: int, weights: list[int]) -> list[int]:
    total = max(0, _coerce_int(total))
    positive_weights = [max(0, _coerce_int(weight)) for weight in weights]
    weight_sum = sum(positive_weights)
    if total <= 0 or weight_sum <= 0:
        return [0 for _ in positive_weights]
    raw_values = [total * weight / weight_sum for weight in positive_weights]
    base_values = [int(value) for value in raw_values]
    remainder = total - sum(base_values)
    if remainder > 0:
        order = sorted(
            range(len(base_values)),
            key=lambda index: raw_values[index] - base_values[index],
            reverse=True,
        )
        for index in order[:remainder]:
            base_values[index] += 1
    return base_values


def _snapshot_llm_distribution_rows(
    snapshot: dict[str, Any],
    warnings: list[str],
) -> list[LLMRow]:
    distribution = snapshot.get("llm_model_call_distribution")
    if not isinstance(distribution, dict):
        return []

    entries: list[dict[str, Any]] = []
    for raw_key, raw_calls in distribution.items():
        calls = _coerce_int(raw_calls)
        if calls <= 0:
            continue
        parts = str(raw_key or "").split(":", 2)
        if len(parts) != 3:
            warnings.append(f"snapshot_llm_distribution_key_unparseable:{raw_key}")
            continue
        provider, model_id, task = parts
        provider_key = _norm_provider(provider)
        model_key = _norm_model(model_id)
        task_key = _norm(task) or "unknown"
        if provider_key == "unknown" or model_key == "unknown":
            warnings.append(f"snapshot_llm_distribution_key_unparseable:{raw_key}")
            continue
        entries.append(
            {
                "provider": provider_key,
                "model": model_key,
                "model_id": model_key,
                "task": task_key,
                "calls": calls,
            }
        )

    if not entries:
        return []

    rows: list[LLMRow] = []
    missing_token_entries: list[dict[str, Any]] = []
    allocated_input = 0
    allocated_output = 0

    tasks = sorted({str(entry["task"]) for entry in entries})
    for task in tasks:
        task_entries = [entry for entry in entries if entry["task"] == task]
        weights = [_coerce_int(entry["calls"]) for entry in task_entries]
        task_input = _coerce_int(snapshot.get(f"{task}_llm_input_tokens"))
        task_output = _coerce_int(snapshot.get(f"{task}_llm_output_tokens"))
        if task_input <= 0 and task_output <= 0:
            missing_token_entries.extend(task_entries)
            continue

        input_allocations = _allocate_int(task_input, weights)
        output_allocations = _allocate_int(task_output, weights)
        allocated_input += sum(input_allocations)
        allocated_output += sum(output_allocations)
        for entry, input_tokens, output_tokens in zip(
            task_entries,
            input_allocations,
            output_allocations,
            strict=False,
        ):
            rows.append(
                LLMRow(
                    provider=str(entry["provider"]),
                    model=str(entry["model"]),
                    model_id=str(entry["model_id"]),
                    task=task,
                    phase="",
                    calls=_coerce_int(entry["calls"]),
                    success_calls=_coerce_int(entry["calls"]),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )

    remaining_input = max(0, _coerce_int(snapshot.get("llm_input_tokens")) - allocated_input)
    remaining_output = max(0, _coerce_int(snapshot.get("llm_output_tokens")) - allocated_output)
    if missing_token_entries and (remaining_input > 0 or remaining_output > 0):
        weights = [_coerce_int(entry["calls"]) for entry in missing_token_entries]
        input_allocations = _allocate_int(remaining_input, weights)
        output_allocations = _allocate_int(remaining_output, weights)
        for entry, input_tokens, output_tokens in zip(
            missing_token_entries,
            input_allocations,
            output_allocations,
            strict=False,
        ):
            rows.append(
                LLMRow(
                    provider=str(entry["provider"]),
                    model=str(entry["model"]),
                    model_id=str(entry["model_id"]),
                    task=str(entry["task"]),
                    phase="",
                    calls=_coerce_int(entry["calls"]),
                    success_calls=_coerce_int(entry["calls"]),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )
        warnings.append(
            "snapshot_llm_residual_tokens_allocated:"
            f"input={remaining_input},output={remaining_output}"
        )
    elif remaining_input > 0 or remaining_output > 0:
        warnings.append(
            "snapshot_llm_tokens_unallocated:"
            f"input={remaining_input},output={remaining_output}"
        )

    if rows:
        warnings.append("snapshot_llm_model_distribution_fallback")
    return sorted(rows, key=lambda row: (row.provider, row.model_id, row.task, row.phase))


def _resolve_tts_event_model(
    provider: str,
    event_model: object,
    *,
    default_tts_provider: str = "",
    default_tts_model: str = "",
    warnings: list[str] | None = None,
) -> str:
    model = _norm_model(event_model)
    default_provider = _norm_provider(default_tts_provider)
    default_model = _norm_model(default_tts_model)

    if _model_belongs_to_provider(provider, model):
        return model
    if provider == default_provider and _model_belongs_to_provider(provider, default_model):
        if model not in {"", "unknown"} and warnings is not None:
            warnings.append(
                f"corrected_tts_model_provider_mismatch:{provider}:{model}->{default_model}"
            )
        return default_model
    fallback = _default_model_for_provider(provider)
    if model not in {"", "unknown"} and warnings is not None:
        warnings.append(f"corrected_tts_model_provider_mismatch:{provider}:{model}->{fallback}")
    return fallback


def _catalog_path() -> Path:
    return Path(
        os.environ.get(
            "AVT_COST_PRICE_CATALOG_PATH",
            "/opt/aivideotrans/config/cost_price_catalog.json",
        )
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_price_catalog() -> dict[str, Any]:
    catalog = json.loads(json.dumps(DEFAULT_PRICE_CATALOG))
    path = _catalog_path()
    if not path.is_file():
        return catalog
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load cost price catalog %s: %s", path, exc)
        return catalog
    if not isinstance(override, dict):
        return catalog
    merged = _deep_merge(catalog, override)
    merged.setdefault("version", catalog["version"])
    merged.setdefault("currency", catalog["currency"])
    return merged


def _rate_to_rmb(rate: dict[str, Any], key: str, usd_to_rmb: float) -> float:
    rmb_key = f"{key}_rmb"
    usd_key = f"{key}_usd"
    if rmb_key in rate:
        return _coerce_float(rate.get(rmb_key))
    if usd_key in rate:
        return _coerce_float(rate.get(usd_key)) * usd_to_rmb
    return 0.0


def _llm_rate_for(catalog: dict[str, Any], provider: str, model_id: str, model: str) -> dict[str, Any] | None:
    llm_rates = catalog.get("llm") if isinstance(catalog.get("llm"), dict) else {}
    keys = [
        _price_key(provider, model_id),
        _price_key(provider, model),
    ]
    for key in keys:
        rate = llm_rates.get(key)
        if isinstance(rate, dict):
            return rate
    return None


def _tts_rate_for(catalog: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    tts_rates = catalog.get("tts") if isinstance(catalog.get("tts"), dict) else {}
    keys = [_price_key(provider, model)]
    raw_model = _norm(model)
    if raw_model == "speech-02-turbo":
        keys.append(f"{_norm_provider(provider)}:speech-2.8-turbo")
    elif raw_model == "speech-02-hd":
        keys.append(f"{_norm_provider(provider)}:speech-2.8-hd")
    for key in keys:
        rate = tts_rates.get(key)
        if isinstance(rate, dict):
            return rate
    return None


def _voice_clone_rate_for(catalog: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    clone_rates = (
        catalog.get("voice_clone")
        if isinstance(catalog.get("voice_clone"), dict)
        else {}
    )
    keys = [
        _price_key(provider, model),
        f"{_norm_provider(provider)}:voice_clone",
    ]
    for key in keys:
        rate = clone_rates.get(key)
        if isinstance(rate, dict):
            return rate
    return None


def apply_costs(breakdown: JobCostBreakdown, catalog: dict[str, Any]) -> None:
    usd_to_rmb = _coerce_float(catalog.get("usd_to_rmb")) or 7.2
    for row in breakdown.llm_rows:
        rate = _llm_rate_for(catalog, row.provider, row.model_id, row.model)
        if rate is None:
            row.rate_status = "missing_rate"
            row.cost_rmb = None
            continue
        input_price = _rate_to_rmb(rate, "input_per_million", usd_to_rmb)
        output_price = _rate_to_rmb(rate, "output_per_million", usd_to_rmb)
        audio_input_price = _rate_to_rmb(rate, "audio_input_per_million", usd_to_rmb) or input_price
        audio_output_price = _rate_to_rmb(rate, "audio_output_per_million", usd_to_rmb) or output_price
        audio_input_hour_price = _rate_to_rmb(rate, "audio_input_per_hour", usd_to_rmb)
        cached_price = _rate_to_rmb(rate, "cached_input_per_million", usd_to_rmb)
        audio_input_tokens = row.input_audio_tokens
        if audio_input_tokens == 0 and row.audio_input_seconds > 0 and audio_input_price > 0:
            audio_input_tokens = int(
                round(row.audio_input_seconds * (_coerce_float(rate.get("audio_tokens_per_second")) or 25.0))
            )
        cost = 0.0
        cost += row.input_tokens / 1_000_000 * input_price
        cost += row.output_tokens / 1_000_000 * output_price
        cost += audio_input_tokens / 1_000_000 * audio_input_price
        cost += row.output_audio_tokens / 1_000_000 * audio_output_price
        cost += row.cached_input_tokens / 1_000_000 * cached_price
        if audio_input_tokens == 0:
            cost += row.audio_input_seconds / 3600 * audio_input_hour_price
        row.cost_rmb = round(cost, 6)
        row.rate_status = "configured"
        row.rate_source = str(rate.get("source") or "")

    for row in breakdown.tts_rows:
        if not row.included_in_job_cost:
            row.cost_rmb = 0.0
            row.rate_status = "excluded_interactive"
            continue
        rate = _tts_rate_for(catalog, row.provider, row.model)
        if rate is None:
            row.rate_status = "missing_rate"
            row.cost_rmb = None
            continue
        price = _coerce_float(rate.get("rmb_per_10k_billed_chars"))
        row.cost_rmb = round(row.billed_chars / 10_000 * price, 6)
        # Promotional (limited-free) providers are marked distinctly so the
        # admin cost page does not read "configured 0 cost" as a permanent
        # price (plan 2026-05-27 Phase 3 — MiMo TTS limited-free).
        row.rate_status = "promotional" if rate.get("promotional") else "configured"
        row.rate_source = str(rate.get("source") or "")

    for row in breakdown.voice_clone_rows:
        rate = _voice_clone_rate_for(catalog, row.provider, row.model)
        if rate is None:
            row.rate_status = "missing_rate"
            row.cost_rmb = None
            continue
        price = _coerce_float(rate.get("rmb_per_clone"))
        row.cost_rmb = round(row.billable_clones * price, 6)
        row.rate_status = "configured"
        row.rate_source = str(rate.get("source") or "")
        row.billing_policy = str(rate.get("billing_policy") or row.billing_policy or "")


def _read_usage_events(project_dir: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not project_dir:
        return [], None
    path = Path(project_dir) / "metering" / "usage_events.jsonl"
    if not path.is_file():
        return [], str(path)
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    except Exception as exc:
        logger.warning("Failed to read usage events for %s: %s", project_dir, exc)
        return [], str(path)
    return events, str(path)


def _aggregate_usage_events(
    events: list[dict[str, Any]],
    *,
    default_tts_provider: str = "",
    default_tts_model: str = "",
) -> JobCostBreakdown:
    breakdown = JobCostBreakdown(
        events_count=len(events),
        has_usage_events=bool(events),
    )
    llm: dict[tuple[str, str, str, str, str], LLMRow] = {}
    tts: dict[tuple[str, str, str], TTSRow] = {}
    voice_clone: dict[tuple[str, str, str], VoiceCloneRow] = {}

    for event in events:
        kind = _norm(event.get("kind"))
        if kind == "llm":
            provider = _norm_provider(event.get("provider"))
            model = _norm_model(event.get("model"))
            model_id = _norm_model(event.get("model_id") or model)
            task = _norm(event.get("task")) or "unknown"
            phase = _norm(event.get("phase"))
            key = (provider, model, model_id, task, phase)
            row = llm.setdefault(
                key,
                LLMRow(
                    provider=provider,
                    model=model,
                    model_id=model_id,
                    task=task,
                    phase=phase,
                ),
            )
            row.calls += 1
            if bool(event.get("success", True)):
                row.success_calls += 1
            row.input_tokens += _coerce_int(
                event.get("input_text_tokens", event.get("input_tokens"))
            )
            row.output_tokens += _coerce_int(
                event.get("output_text_tokens", event.get("output_tokens"))
            )
            row.input_audio_tokens += _coerce_int(
                event.get("input_audio_tokens", event.get("audio_input_tokens"))
            )
            row.output_audio_tokens += _coerce_int(
                event.get("output_audio_tokens", event.get("audio_output_tokens"))
            )
            row.cached_input_tokens += _coerce_int(event.get("cached_input_tokens"))
            row.audio_input_seconds += _coerce_float(event.get("audio_input_seconds"))
            row.audio_input_bytes += _coerce_int(event.get("audio_input_bytes"))
        elif kind == "tts":
            provider = _norm_provider(event.get("provider") or default_tts_provider)
            event_model = event.get("model")
            inferred_model = (
                _infer_cosyvoice_model_from_event(event)
                if provider == "cosyvoice"
                else ""
            )
            if inferred_model and _norm_model(event_model) != inferred_model:
                breakdown.warnings.append(
                    "inferred_tts_model_from_voice:"
                    f"{provider}:{_norm_model(event_model)}->{inferred_model}"
                )
                event_model = inferred_model
            model = _resolve_tts_event_model(
                provider,
                event_model,
                default_tts_provider=default_tts_provider,
                default_tts_model=default_tts_model,
                warnings=breakdown.warnings,
            )
            bucket = _norm(event.get("bucket")) or "unknown"
            key = (provider, model, bucket)
            row = tts.setdefault(
                key,
                TTSRow(
                    provider=provider,
                    model=model,
                    bucket=bucket,
                    included_in_job_cost=bucket in JOB_TTS_BUCKETS,
                ),
            )
            row.calls += 1
            row.input_chars += _coerce_int(event.get("input_chars"))
            row.billed_chars += _coerce_int(event.get("billed_chars"))
            row.duration_ms += _coerce_int(event.get("duration_ms"))
        elif kind == "voice_clone":
            provider = _norm_provider(event.get("provider"))
            model = _norm_model(event.get("model") or "voice_clone")
            bucket = _norm(event.get("bucket")) or "voice_clone"
            key = (provider, model, bucket)
            row = voice_clone.setdefault(
                key,
                VoiceCloneRow(
                    provider=provider,
                    model=model,
                    bucket=bucket,
                    billing_policy=str(event.get("billing_policy") or ""),
                ),
            )
            row.calls += 1
            success = bool(event.get("success", True))
            if success:
                row.success_calls += 1
            clone_count = _coerce_int(event.get("clone_count"))
            if clone_count <= 0 and success:
                clone_count = 1
            if bool(event.get("billable", True)):
                row.billable_clones += max(0, clone_count)
            row.source_audio_seconds += _coerce_float(event.get("source_audio_seconds"))
            row.source_audio_bytes += _coerce_int(event.get("source_audio_bytes"))
            row.selected_segment_count += _coerce_int(event.get("selected_segment_count"))
            if not row.billing_policy:
                row.billing_policy = str(event.get("billing_policy") or "")

    breakdown.llm_rows = sorted(
        llm.values(),
        key=lambda row: (row.provider, row.model_id, row.task, row.phase),
    )
    breakdown.tts_rows = sorted(
        tts.values(),
        key=lambda row: (row.provider, row.model, row.bucket),
    )
    breakdown.voice_clone_rows = sorted(
        voice_clone.values(),
        key=lambda row: (row.provider, row.model, row.bucket),
    )
    if breakdown.warnings:
        breakdown.warnings = list(dict.fromkeys(breakdown.warnings))
    return breakdown


def _snapshot_breakdown(
    snapshot: dict[str, Any],
    *,
    default_tts_provider: str = "",
    default_tts_model: str = "",
) -> JobCostBreakdown:
    breakdown = JobCostBreakdown(
        events_count=0,
        has_usage_events=False,
        warnings=["usage_events artifact missing; using metering_snapshot fallback"],
    )
    input_tokens = _coerce_int(snapshot.get("llm_input_tokens"))
    output_tokens = _coerce_int(snapshot.get("llm_output_tokens"))
    breakdown.llm_rows = _snapshot_llm_distribution_rows(snapshot, breakdown.warnings)
    if (input_tokens or output_tokens) and not breakdown.llm_rows:
        breakdown.llm_rows.append(
            LLMRow(
                provider="unknown",
                model="unknown",
                model_id="unknown",
                task="snapshot_total",
                phase="",
                calls=_coerce_int(snapshot.get("llm_call_count")),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                audio_input_seconds=_coerce_float(snapshot.get("llm_audio_input_seconds")),
                rate_status="missing_rate",
            )
        )

    bucket_fields = {
        "probe_tts": "probe_tts_billed_chars",
        "first_tts": "first_tts_billed_chars",
        "post_tts_resynth": "post_tts_resynth_billed_chars",
        "post_edit_resynth": "post_edit_resynth_tts_billed_chars",
        "interactive_preview": "interactive_preview_tts_billed_chars",
    }
    for bucket, field_name in bucket_fields.items():
        billed = _coerce_int(snapshot.get(field_name))
        if billed <= 0:
            continue
        breakdown.tts_rows.append(
            TTSRow(
                provider=_norm_provider(default_tts_provider),
                model=_norm_model(default_tts_model),
                bucket=bucket,
                calls=_coerce_int(snapshot.get(f"{bucket}_call_count")),
                billed_chars=billed,
                included_in_job_cost=bucket in JOB_TTS_BUCKETS,
            )
        )

    voice_clone_count = _coerce_int(snapshot.get("voice_clone_billable_count"))
    if voice_clone_count > 0:
        breakdown.voice_clone_rows.append(
            VoiceCloneRow(
                provider="minimax",
                model="voice_clone",
                bucket="voice_clone",
                calls=_coerce_int(snapshot.get("voice_clone_call_count")),
                success_calls=_coerce_int(snapshot.get("voice_clone_success_call_count")),
                billable_clones=voice_clone_count,
                source_audio_seconds=_coerce_float(snapshot.get("voice_clone_source_audio_seconds")),
            )
        )
    return breakdown


def build_job_breakdown(
    *,
    project_dir: str | None,
    snapshot: dict[str, Any] | None,
    default_tts_provider: str = "",
    default_tts_model: str = "",
    catalog: dict[str, Any] | None = None,
) -> JobCostBreakdown:
    events, events_path = _read_usage_events(project_dir)
    if events:
        breakdown = _aggregate_usage_events(
            events,
            default_tts_provider=default_tts_provider,
            default_tts_model=default_tts_model,
        )
    else:
        breakdown = _snapshot_breakdown(
            snapshot or {},
            default_tts_provider=default_tts_provider,
            default_tts_model=default_tts_model,
        )
        if events_path:
            breakdown.warnings.append(f"usage_events not found: {events_path}")
    default_provider_key = _norm_provider(default_tts_provider)
    default_model_key = _norm_model(default_tts_model)
    if _model_belongs_to_provider(default_provider_key, default_model_key):
        for row in breakdown.tts_rows:
            if row.provider == default_provider_key and row.model != default_model_key:
                breakdown.warnings.append(
                    "job_tts_model_mismatch:"
                    f"job={default_provider_key}:{default_model_key}, "
                    f"usage={row.provider}:{row.model}; cost uses usage model"
                )
                break
    if breakdown.warnings:
        breakdown.warnings = list(dict.fromkeys(breakdown.warnings))
    apply_costs(breakdown, catalog or load_price_catalog())
    missing = [
        f"llm:{row.provider}:{row.model_id}:{row.task}"
        for row in breakdown.llm_rows
        if row.cost_rmb is None
    ]
    missing.extend(
        f"tts:{row.provider}:{row.model}:{row.bucket}"
        for row in breakdown.tts_rows
        if row.cost_rmb is None
    )
    missing.extend(
        f"voice_clone:{row.provider}:{row.model}:{row.bucket}"
        for row in breakdown.voice_clone_rows
        if row.cost_rmb is None
    )
    if missing:
        breakdown.warnings.append("missing_rate: " + ", ".join(missing[:8]))
    return breakdown


def _row_cost(row: LLMRow | TTSRow | VoiceCloneRow) -> float:
    return float(row.cost_rmb or 0.0)


def _job_minutes(job: Job) -> float | None:
    actual = _coerce_float(getattr(job, "actual_minutes", None))
    if actual > 0:
        return actual
    estimated = _coerce_float(getattr(job, "estimated_minutes", None))
    if estimated > 0:
        return estimated
    seconds = _coerce_float(getattr(job, "estimated_duration_seconds", None))
    if seconds > 0:
        return seconds / 60
    return None


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _point_price_from_runtime() -> tuple[float, str]:
    try:
        from pricing_runtime import get_runtime_pricing

        point_price = float(get_runtime_pricing().cost_model.point_price_rmb or 0.0)
        return point_price, "pricing_runtime.cost_model.point_price_rmb"
    except Exception:
        return 0.03, "pricing_schema.default_cost_model.point_price_rmb"


def _server_cost_from_runtime() -> tuple[float, str]:
    try:
        from pricing_runtime import get_runtime_pricing

        server_cost = float(get_runtime_pricing().cost_model.server_cost_rmb_per_src_min or 0.0)
        return server_cost, "pricing_runtime.cost_model.server_cost_rmb_per_src_min"
    except Exception:
        return 0.03, "pricing_schema.default_cost_model.server_cost_rmb_per_src_min"


def _derive_credits_from_minutes(job: Job, minutes: float | None) -> int:
    if not minutes:
        return 0
    snapshot = job.metering_snapshot if isinstance(job.metering_snapshot, dict) else {}
    try:
        from credits_service import estimate_credits

        return estimate_credits(
            minutes,
            service_mode=job.service_mode or snapshot.get("service_mode") or "express",
            quality_tier=snapshot.get("quality_tier") or "standard",
        )
    except Exception:
        return 0


def _estimate_job_revenue(
    job: Job,
    *,
    minutes: float | None,
    point_price_rmb: float,
    ledger_capture_credits: int | None = None,
) -> RevenueEstimate:
    snapshot = job.metering_snapshot if isinstance(job.metering_snapshot, dict) else {}
    credits = _coerce_int(ledger_capture_credits)
    source = "credits_ledger_capture"
    if credits <= 0:
        credits = _coerce_int(snapshot.get("credits_actual"))
        source = "credits_actual"
    if credits <= 0:
        if _norm(getattr(job, "status", "")) == "succeeded":
            credits = _derive_credits_from_minutes(job, minutes)
            source = "derived_from_actual_minutes"
    if credits <= 0:
        credits = _coerce_int(snapshot.get("credits_estimated"))
        source = "credits_estimated"
    if credits <= 0:
        credits = _derive_credits_from_minutes(job, minutes)
        source = "derived_from_minutes"
    if credits <= 0:
        return RevenueEstimate(
            credits=None,
            source="missing",
            point_price_rmb=point_price_rmb,
            revenue_rmb=None,
        )
    return RevenueEstimate(
        credits=credits,
        source=source,
        point_price_rmb=point_price_rmb,
        revenue_rmb=credits * point_price_rmb,
    )


def _llm_row_payload(row: LLMRow) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "model": row.model,
        "model_id": row.model_id,
        "task": row.task,
        "phase": row.phase,
        "calls": row.calls,
        "success_calls": row.success_calls,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "input_audio_tokens": row.input_audio_tokens,
        "output_audio_tokens": row.output_audio_tokens,
        "cached_input_tokens": row.cached_input_tokens,
        "audio_input_seconds": round(row.audio_input_seconds, 3),
        "audio_input_bytes": row.audio_input_bytes,
        "cost_rmb": _round_money(row.cost_rmb),
        "rate_status": row.rate_status,
        "rate_source": row.rate_source,
    }


def _tts_row_payload(row: TTSRow) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "model": row.model,
        "bucket": row.bucket,
        "calls": row.calls,
        "input_chars": row.input_chars,
        "billed_chars": row.billed_chars,
        "duration_ms": row.duration_ms,
        "included_in_job_cost": row.included_in_job_cost,
        "cost_rmb": _round_money(row.cost_rmb),
        "rate_status": row.rate_status,
        "rate_source": row.rate_source,
    }


def _voice_clone_row_payload(row: VoiceCloneRow) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "model": row.model,
        "bucket": row.bucket,
        "calls": row.calls,
        "success_calls": row.success_calls,
        "billable_clones": row.billable_clones,
        "source_audio_seconds": round(row.source_audio_seconds, 3),
        "source_audio_bytes": row.source_audio_bytes,
        "selected_segment_count": row.selected_segment_count,
        "cost_rmb": _round_money(row.cost_rmb),
        "rate_status": row.rate_status,
        "rate_source": row.rate_source,
        "billing_policy": row.billing_policy,
    }


def _job_payload(
    job: Job,
    owner: User | None,
    breakdown: JobCostBreakdown,
    *,
    point_price_rmb: float | None = None,
    point_price_source: str | None = None,
    server_cost_per_min_rmb: float | None = None,
    server_cost_source: str | None = None,
    ledger_capture_credits: int | None = None,
    ledger_job_capture_credits: int | None = None,
    ledger_voice_clone_capture_credits: int | None = None,
) -> dict[str, Any]:
    llm_cost = sum(_row_cost(row) for row in breakdown.llm_rows)
    tts_cost = sum(_row_cost(row) for row in breakdown.tts_rows)
    voice_clone_cost = sum(_row_cost(row) for row in breakdown.voice_clone_rows)
    total_cost = llm_cost + tts_cost + voice_clone_cost
    minutes = _job_minutes(job)
    if point_price_rmb is None:
        point_price_rmb, point_price_source = _point_price_from_runtime()
    else:
        point_price_source = point_price_source or "explicit"
    if server_cost_per_min_rmb is None:
        server_cost_per_min_rmb, server_cost_source = _server_cost_from_runtime()
    else:
        server_cost_source = server_cost_source or "explicit"
    server_overhead_cost = (minutes or 0.0) * server_cost_per_min_rmb
    margin_cost = total_cost + server_overhead_cost
    revenue = _estimate_job_revenue(
        job,
        minutes=minutes,
        point_price_rmb=point_price_rmb,
        ledger_capture_credits=ledger_capture_credits,
    )
    gross_profit = (
        revenue.revenue_rmb - margin_cost
        if revenue.revenue_rmb is not None
        else None
    )
    gross_margin_pct = (
        gross_profit / revenue.revenue_rmb * 100
        if revenue.revenue_rmb and revenue.revenue_rmb > 0 and gross_profit is not None
        else None
    )
    missing_rate_rows = sum(
        1
        for row in [*breakdown.llm_rows, *breakdown.tts_rows, *breakdown.voice_clone_rows]
        if row.cost_rmb is None
    )
    warnings = list(breakdown.warnings)
    snapshot = job.metering_snapshot if isinstance(job.metering_snapshot, dict) else {}
    if (
        _norm(getattr(job, "status", "")) == "succeeded"
        and _coerce_int(snapshot.get("credits_estimated")) > 0
        and ledger_job_capture_credits is not None
        and _coerce_int(ledger_job_capture_credits) <= 0
    ):
        warnings.append(
            "missing_job_capture: terminal job has credits_estimated but no job_capture ledger"
        )
    return {
        "job_id": job.job_id,
        "title": getattr(job, "display_name", None) or getattr(job, "title", None) or getattr(job, "source_ref", None) or job.job_id,
        "owner_email": getattr(owner, "email", None) if owner else None,
        "owner_display_name": getattr(owner, "display_name", None) if owner else None,
        "status": job.status,
        "current_stage": job.current_stage,
        "service_mode": job.service_mode,
        "tts_provider": job.tts_provider,
        "tts_model": job.tts_model,
        "plan_code_snapshot": job.plan_code_snapshot,
        "quality_tier": (job.metering_snapshot or {}).get("quality_tier") if job.metering_snapshot else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "minutes": round(minutes, 3) if minutes else None,
        "usage_events_count": breakdown.events_count,
        "has_usage_events": breakdown.has_usage_events,
        "llm_cost_rmb": _round_money(llm_cost),
        "tts_cost_rmb": _round_money(tts_cost),
        "voice_clone_cost_rmb": _round_money(voice_clone_cost),
        "total_cost_rmb": _round_money(total_cost),
        "cost_per_minute_rmb": _round_money(total_cost / minutes) if minutes else None,
        "credits_charged": revenue.credits,
        "credits_source": revenue.source,
        "job_credits_charged": ledger_job_capture_credits,
        "voice_clone_credits_charged": ledger_voice_clone_capture_credits,
        "point_price_rmb": _round_money(revenue.point_price_rmb),
        "point_price_source": point_price_source,
        "revenue_estimate_rmb": _round_money(revenue.revenue_rmb),
        "server_overhead_cost_rmb": _round_money(server_overhead_cost),
        "server_cost_per_min_rmb": _round_money(server_cost_per_min_rmb),
        "server_cost_source": server_cost_source,
        "margin_cost_rmb": _round_money(margin_cost),
        "gross_profit_rmb": _round_money(gross_profit),
        "gross_margin_pct": round(gross_margin_pct, 2) if gross_margin_pct is not None else None,
        "missing_rate_rows": missing_rate_rows,
        "warnings": warnings,
        "llm": [_llm_row_payload(row) for row in breakdown.llm_rows],
        "tts": [_tts_row_payload(row) for row in breakdown.tts_rows],
        "voice_clone": [_voice_clone_row_payload(row) for row in breakdown.voice_clone_rows],
    }


def _parse_window(window: str) -> tuple[int, datetime]:
    try:
        days = max(1, min(180, int(window)))
    except (TypeError, ValueError):
        days = 7
    return days, datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/rates")
async def cost_rates(
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    catalog = load_price_catalog()
    return {
        "version": catalog.get("version"),
        "currency": catalog.get("currency", "RMB"),
        "usd_to_rmb": catalog.get("usd_to_rmb"),
        "catalog_path": str(_catalog_path()),
        "llm": catalog.get("llm", {}),
        "tts": catalog.get("tts", {}),
        "voice_clone": catalog.get("voice_clone", {}),
        "notes": catalog.get("notes", ""),
    }


@router.get("/jobs")
async def cost_jobs(
    window: str = Query("7", description="Lookback window in days, 1-180"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    days, cutoff = _parse_window(window)
    catalog = load_price_catalog()

    result = await db.execute(
        select(Job, User)
        .outerjoin(User, Job.user_id == User.id)
        .where(Job.created_at >= cutoff)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    job_ids = [job.job_id for job, _owner in rows if job.job_id]
    ledger_capture_by_job: dict[str, int] = {}
    ledger_job_capture_by_job: dict[str, int] = {}
    ledger_voice_clone_capture_by_job: dict[str, int] = {}
    if job_ids:
        ledger_result = await db.execute(
            select(
                CreditsLedger.related_job_id,
                CreditsLedger.reason_code,
                func.coalesce(func.sum(-CreditsLedger.credits_delta), 0),
            )
            .where(
                CreditsLedger.related_job_id.in_(job_ids),
                CreditsLedger.direction == "capture",
            )
            .group_by(CreditsLedger.related_job_id, CreditsLedger.reason_code)
        )
        for job_id, reason_code, credits in ledger_result.all():
            if not job_id:
                continue
            key = str(job_id)
            amount = int(credits or 0)
            ledger_capture_by_job[key] = ledger_capture_by_job.get(key, 0) + amount
            if reason_code in {"job_capture", "capture_additional", "capture_overdraft"}:
                ledger_job_capture_by_job[key] = ledger_job_capture_by_job.get(key, 0) + amount
            elif reason_code == "voice_clone_capture":
                ledger_voice_clone_capture_by_job[key] = ledger_voice_clone_capture_by_job.get(key, 0) + amount
    jobs: list[dict[str, Any]] = []
    total_llm = 0.0
    total_tts = 0.0
    total_voice_clone = 0.0
    total_revenue = 0.0
    total_server_overhead = 0.0
    total_minutes = 0.0
    jobs_with_usage_events = 0
    jobs_with_missing_rates = 0
    total_missing_rate_rows = 0
    point_price_rmb, point_price_source = _point_price_from_runtime()
    server_cost_per_min_rmb, server_cost_source = _server_cost_from_runtime()

    for job, owner in rows:
        snapshot = job.metering_snapshot if isinstance(job.metering_snapshot, dict) else {}
        default_tts_provider = job.tts_provider or snapshot.get("tts_provider") or ""
        default_tts_model = job.tts_model or snapshot.get("tts_model") or ""
        breakdown = build_job_breakdown(
            project_dir=job.project_dir,
            snapshot=snapshot,
            default_tts_provider=default_tts_provider,
            default_tts_model=default_tts_model,
            catalog=catalog,
        )
        payload = _job_payload(
            job,
            owner,
            breakdown,
            point_price_rmb=point_price_rmb,
            point_price_source=point_price_source,
            server_cost_per_min_rmb=server_cost_per_min_rmb,
            server_cost_source=server_cost_source,
            ledger_capture_credits=ledger_capture_by_job.get(job.job_id),
            ledger_job_capture_credits=ledger_job_capture_by_job.get(job.job_id, 0),
            ledger_voice_clone_capture_credits=ledger_voice_clone_capture_by_job.get(job.job_id, 0),
        )
        jobs.append(payload)
        total_llm += float(payload["llm_cost_rmb"] or 0.0)
        total_tts += float(payload["tts_cost_rmb"] or 0.0)
        total_voice_clone += float(payload["voice_clone_cost_rmb"] or 0.0)
        total_revenue += float(payload["revenue_estimate_rmb"] or 0.0)
        total_server_overhead += float(payload["server_overhead_cost_rmb"] or 0.0)
        total_minutes += float(payload["minutes"] or 0.0)
        if payload["has_usage_events"]:
            jobs_with_usage_events += 1
        if payload["missing_rate_rows"]:
            jobs_with_missing_rates += 1
            total_missing_rate_rows += int(payload["missing_rate_rows"])

    total_cost = total_llm + total_tts + total_voice_clone
    margin_cost = total_cost + total_server_overhead
    return {
        "window_days": days,
        "limit": limit,
        "currency": catalog.get("currency", "RMB"),
        "pricing_version": catalog.get("version"),
        "catalog_path": str(_catalog_path()),
        "totals": {
            "jobs": len(jobs),
            "jobs_with_usage_events": jobs_with_usage_events,
            "jobs_with_missing_rates": jobs_with_missing_rates,
            "missing_rate_rows": total_missing_rate_rows,
            "minutes": round(total_minutes, 3),
            "llm_cost_rmb": _round_money(total_llm),
            "tts_cost_rmb": _round_money(total_tts),
            "voice_clone_cost_rmb": _round_money(total_voice_clone),
            "total_cost_rmb": _round_money(total_cost),
            "revenue_estimate_rmb": _round_money(total_revenue),
            "server_overhead_cost_rmb": _round_money(total_server_overhead),
            "server_cost_per_min_rmb": _round_money(server_cost_per_min_rmb),
            "server_cost_source": server_cost_source,
            "margin_cost_rmb": _round_money(margin_cost),
            "gross_profit_rmb": _round_money(total_revenue - margin_cost)
            if total_revenue > 0
            else None,
            "gross_margin_pct": round((total_revenue - margin_cost) / total_revenue * 100, 2)
            if total_revenue > 0
            else None,
            "point_price_rmb": _round_money(point_price_rmb),
            "point_price_source": point_price_source,
            "cost_per_minute_rmb": _round_money(total_cost / total_minutes)
            if total_minutes > 0
            else None,
        },
        "jobs": jobs,
    }
