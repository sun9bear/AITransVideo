"""Admin job monitor API: logs viewing + AI-powered log analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from config import settings
from internal_auth import internal_headers
from models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-job-monitor"])

# Job API upstream URL comes from gateway/config.py (env var AVT_JOB_API_UPSTREAM).
# No module-level constant for the URL — always read settings.job_api_upstream
# at call time so tests can monkeypatch without a module reload.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require_admin(user: User | None) -> None:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


# ---------------------------------------------------------------------------
# Endpoint 1: Admin job logs (bypasses ownership check)
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/logs")
async def admin_get_job_logs(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Get full event logs for any job (admin only)."""
    _require_admin(user)

    async with httpx.AsyncClient(timeout=15, headers=internal_headers()) as client:
        try:
            resp = await client.get(f"{settings.job_api_upstream}/jobs/{job_id}/logs")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail="获取日志失败")
        except Exception:
            raise HTTPException(status_code=502, detail="获取日志失败")


# ---------------------------------------------------------------------------
# Endpoint 2: AI-powered log analysis
# ---------------------------------------------------------------------------

# --- Log trimming ---

_CRITICAL_KEYWORDS = re.compile(
    r"fallback|error|retry|fail|timeout|降级|回退|exception|crash",
    re.IGNORECASE,
)

_MAX_EVENTS_FOR_AI = 200
_MAX_CHARS = 25_000


def _trim_events(events: list[dict]) -> tuple[list[dict], int]:
    """Smart trim: keep all warn/error, status events, stage changes,
    keyword matches, first/last anchors. Sample the rest.
    Returns (trimmed_events, original_count)."""
    total = len(events)
    if total <= _MAX_EVENTS_FOR_AI:
        return events, total

    kept_indices: set[int] = set()

    # 1. All error/warn events
    for i, ev in enumerate(events):
        if ev.get("level") in ("error", "warn"):
            kept_indices.add(i)

    # 2. All status events + stage-change events
    prev_stage = None
    for i, ev in enumerate(events):
        if ev.get("event_type") == "status":
            kept_indices.add(i)
        current_stage = ev.get("stage")
        if current_stage and current_stage != prev_stage:
            kept_indices.add(i)
        prev_stage = current_stage

    # 3. Keyword matches
    for i, ev in enumerate(events):
        msg = ev.get("message") or ""
        if _CRITICAL_KEYWORDS.search(msg):
            kept_indices.add(i)

    # 4. First 5 + last 10 anchors
    for i in range(min(5, total)):
        kept_indices.add(i)
    for i in range(max(0, total - 10), total):
        kept_indices.add(i)

    # 5. If still under budget, sample remaining info events evenly
    remaining_budget = _MAX_EVENTS_FOR_AI - len(kept_indices)
    if remaining_budget > 0:
        remaining_indices = [i for i in range(total) if i not in kept_indices]
        if remaining_indices and len(remaining_indices) > remaining_budget:
            step = len(remaining_indices) / remaining_budget
            for j in range(remaining_budget):
                kept_indices.add(remaining_indices[int(j * step)])
        else:
            kept_indices.update(remaining_indices)

    trimmed = [events[i] for i in sorted(kept_indices)]
    return trimmed, total


def _event_to_jsonl(ev: dict) -> str:
    """Convert a single event dict to a compact JSON line for AI input."""
    compact: dict = {"ts": ev.get("created_at", ""), "type": ev.get("event_type", "")}
    if ev.get("level"):
        compact["level"] = ev["level"]
    if ev.get("stage"):
        compact["stage"] = ev["stage"]
    if ev.get("message"):
        compact["msg"] = ev["message"]
    if ev.get("status"):
        compact["status"] = ev["status"]
    if ev.get("payload"):
        payload_str = json.dumps(ev["payload"], ensure_ascii=False)
        if len(payload_str) > 200:
            payload_str = payload_str[:200] + "..."
        compact["payload"] = payload_str
    return json.dumps(compact, ensure_ascii=False)


def _build_ai_input(
    job_info: dict,
    events: list[dict],
    result_summary: dict | None,
) -> str:
    """Build the rich context string sent to DeepSeek as user message."""
    parts: list[str] = []

    # Section 1: Job metadata
    parts.append("== 任务信息 ==")
    for key in ("job_id", "status", "current_stage", "video_title",
                "service_mode", "tts_provider", "speakers", "created_at"):
        val = job_info.get(key)
        if val is not None:
            parts.append(f"{key}: {val}")
    for key in ("error_summary", "fallback_summary"):
        val = job_info.get(key)
        if val:
            parts.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
    parts.append("")

    # Section 2: Structured events (JSON Lines)
    trimmed, original_count = _trim_events(events)
    parts.append(f"== 事件日志（{len(trimmed)}/{original_count} 条）==")
    if len(trimmed) < original_count:
        parts.append(
            f"[系统] 原始事件共 {original_count} 条，已裁剪为 {len(trimmed)} 条"
            "（保留全部 warn/error/状态变更/阶段切换/关键词事件）"
        )
    for ev in trimmed:
        parts.append(_event_to_jsonl(ev))
    parts.append("")

    # Section 3: Result summary (if available)
    if result_summary:
        parts.append("== 结果摘要 ==")
        # Keep only top-level fields, skip deep nesting
        slim = {}
        for k, v in result_summary.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                slim[k] = v
            elif isinstance(v, dict) and len(json.dumps(v, ensure_ascii=False)) < 500:
                slim[k] = v
        parts.append(json.dumps(slim, ensure_ascii=False, indent=2))

    text = "\n".join(parts)

    # Hard limit: if still too long, truncate middle info events
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n... [截断]"

    return text


# --- System prompt ---

SYSTEM_PROMPT = """\
你是 AIVideoTrans 视频翻译/配音平台的运维分析专家。
用户会给你一个任务的元数据、结构化事件日志和结果摘要，请分析流程是否正常、有无异常。

## 平台架构

这是一个视频翻译/配音 SaaS，Pipeline 流程：
- S0 输入准备：下载视频、提取音频、分离人声
- S1 媒体理解：AssemblyAI 转录（说话人分离）、语言检测
- S2 说话人审核：三段式 LLM 审校
  - Pass 1（说话人识别）：Gemini + 音频，识别说话人身份、纠正 speaker 分配
  - Pass 2（文本修正）：Gemini 纯文本，修正转录错误、拆分过长段落、提取术语表
  - Pass 3（音色画像）：Gemini + 音频片段，为每个说话人生成音色描述
  - 失败自动降级到 legacy 单次审校
- S3 翻译审核：翻译（默认 DeepSeek）→ 等待用户确认翻译稿
- S4 草稿与配音：注入音色描述
- TTS 合成：MiniMax(studio) / CosyVoice(express) / VolcEngine
- 音频对齐：时长匹配 + 可能触发重写
- 输出：配音音频 + 字幕 + 下载包

## 两种模式

- Studio（工作台版）：需人工审核（翻译审核 + 音色选择），MiniMax TTS，支持声音克隆
- Express（快捷版）：跳过 Pass 1，无人工审核，CosyVoice TTS，自动匹配音色

## 审核暂停点

Studio 模式下 pipeline 会在以下阶段暂停等待用户操作：
- 翻译审核（translation_review）：用户确认翻译文本
- 音色选择审核（voice_selection_review）：用户为每个说话人选择或克隆音色

## 事件日志说明

事件日志为 JSON Lines 格式，每条包含：
- ts：时间戳
- type：事件类型，只有两种 ——  log（普通日志）和 status（状态变更）
- level：info / warn / error
- stage：所处流水线阶段（阶段推进通过 stage 字段值的变化体现）
- msg：消息文本
- status：任务状态变更（如有，常见值：running / waiting_for_review / succeeded / failed）
- payload：附加数据（如有，可能含模型名、重试次数、耗时等）

日志可能经过裁剪，会在开头注明原始/保留数量。全部 warn/error 事件已保留。

## 常见问题模式

1. Pass 1 JSON 解析失败：gemini-3.1-pro 输出截断，自动降级到 flash-lite。关注：是否频繁降级
2. Speaker 重复命名：两个 speaker_id 被识别为同一人名（ASR 把一个人拆成了两个 ID）
3. Edit distance 超限：文本修正幅度过大被拒绝，日志中会显示 ratio
4. TTS 音色失效：MiniMax 返回 status_code=2054，音色 ID 不存在
5. Split 偏差回退：word-level split 和 text-ratio split 时间偏差过大
6. S2 重复执行：pipeline 恢复后重跑了 S2（已修复，但旧任务可能有此问题）
7. 翻译段数不匹配：翻译返回的 segment 数与请求不一致

## 输出要求

请严格按以下 JSON 格式输出（不要输出 JSON 以外的内容）：

{
  "summary": "一两句话总结任务整体状况",
  "timeline": [
    { "stage": "阶段名", "start": "时间", "end": "时间", "duration": "耗时", "note": "备注（可选）" }
  ],
  "issues": [
    {
      "title": "问题标题",
      "severity": "high | medium | low",
      "detail": "问题描述",
      "evidence": "相关日志行或数据"
    }
  ],
  "suggestions": [
    "具体建议 1",
    "具体建议 2"
  ]
}

如果流程完全正常无异常，issues 和 suggestions 可以为空数组，summary 简要说明即可。
"""


# --- Schema validation ---

def _validate_analysis(data: dict) -> dict:
    """Validate and normalize the AI analysis JSON.
    Ensures all fields have correct types. Returns a clean dict or raises ValueError."""
    if not isinstance(data, dict) or "summary" not in data:
        raise ValueError("missing summary field")

    raw_timeline = data.get("timeline")
    raw_issues = data.get("issues")
    raw_suggestions = data.get("suggestions")

    # Normalize timeline: must be list of dicts with at least "stage"
    timeline: list[dict] = []
    if isinstance(raw_timeline, list):
        for item in raw_timeline:
            if isinstance(item, dict) and "stage" in item:
                timeline.append({
                    "stage": str(item.get("stage", "")),
                    "start": str(item.get("start", "")),
                    "end": str(item.get("end", "")),
                    "duration": str(item.get("duration", "")),
                    "note": str(item.get("note", "")) if item.get("note") else None,
                })

    # Normalize issues: must be list of dicts with at least "title"
    issues: list[dict] = []
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if isinstance(item, dict) and "title" in item:
                severity = str(item.get("severity", "low"))
                if severity not in ("high", "medium", "low"):
                    severity = "low"
                issues.append({
                    "title": str(item.get("title", "")),
                    "severity": severity,
                    "detail": str(item.get("detail", "")),
                    "evidence": str(item.get("evidence", "")),
                })

    # Normalize suggestions: must be list of strings
    suggestions: list[str] = []
    if isinstance(raw_suggestions, list):
        for item in raw_suggestions:
            if isinstance(item, str) and item.strip():
                suggestions.append(item.strip())

    return {
        "summary": str(data["summary"]),
        "timeline": timeline,
        "issues": issues,
        "suggestions": suggestions,
    }


# --- Main endpoint ---

@router.post("/jobs/{job_id}/analyze-logs")
async def analyze_job_logs(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """AI-powered log analysis for a job (admin only)."""
    _require_admin(user)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        return {"error": "未配置 OPENAI_API_KEY"}

    # Parallel fetch: logs + job info + result-summary
    async with httpx.AsyncClient(timeout=15, headers=internal_headers()) as client:
        logs_coro = client.get(f"{settings.job_api_upstream}/jobs/{job_id}/logs")
        job_coro = client.get(f"{settings.job_api_upstream}/jobs/{job_id}")
        result_coro = client.get(f"{settings.job_api_upstream}/jobs/{job_id}/result-summary")

        logs_resp, job_resp, result_resp = await asyncio.gather(
            logs_coro, job_coro, result_coro,
            return_exceptions=True,
        )

    # Parse logs (required)
    if isinstance(logs_resp, Exception) or not hasattr(logs_resp, "status_code"):
        return {"error": "获取日志失败"}
    if logs_resp.status_code != 200:
        return {"error": "获取日志失败"}
    logs_data = logs_resp.json()
    events: list[dict] = logs_data.get("events", [])

    # Parse job info (required)
    if isinstance(job_resp, Exception) or not hasattr(job_resp, "status_code"):
        return {"error": "获取任务信息失败"}
    if job_resp.status_code != 200:
        return {"error": "获取任务信息失败"}
    job_info: dict = job_resp.json()

    # Parse result-summary (optional, failure = None)
    result_summary: dict | None = None
    if (
        not isinstance(result_resp, Exception)
        and hasattr(result_resp, "status_code")
        and result_resp.status_code == 200
    ):
        try:
            result_summary = result_resp.json()
        except Exception:
            pass

    # Build AI input
    user_message = _build_ai_input(job_info, events, result_summary)
    logger.info(
        "AI analysis for job %s: %d events -> %d chars input",
        job_id, len(events), len(user_message),
    )

    # Call OpenAI GPT-5.4 thinking
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-5.4",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    "reasoning_effort": "medium",
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("OpenAI API call failed for job %s", job_id)
            return {"error": "分析失败，请稍后重试"}

    # Parse and validate response
    try:
        ai_body = resp.json()
        content = ai_body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        analysis = _validate_analysis(parsed)
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("OpenAI returned invalid JSON for job %s", job_id)
        return {"error": "AI 返回格式异常，请重试"}

    return {"analysis": analysis}
