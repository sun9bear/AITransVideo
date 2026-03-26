"""Gemini 辅助说话人标签纠正。

AssemblyAI 的 speaker diarization 在短句和频繁切换时容易标反。
本模块在说话人审核前用 Gemini 分析对话内容，自动纠正明显的标签错误。

工作流：
1. 从 transcript lines 提取每个说话人的样本
2. 发送给 Gemini，请求识别说话人身份
3. 检测并修正标签交叉错误
"""
import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_API_KEY_ENV = "GEMINI_API_KEY"


@dataclass
class SpeakerIdentity:
    speaker_id: str  # "speaker_a", "speaker_b"
    name: str  # "Charlie Munger"
    role: str  # "被采访者", "主持人", "旁白"


@dataclass
class CorrectionResult:
    identities: list[SpeakerIdentity]
    corrections: dict[int, str]  # line_index -> corrected_speaker_id
    correction_count: int
    confidence: str  # "high", "medium", "low"


def correct_speakers(
    lines: list[dict],
    video_title: str = "",
    video_description: str = "",
) -> CorrectionResult | None:
    """用 Gemini 分析并纠正说话人标签。

    Args:
        lines: transcript.json 的 lines 列表
        video_title: 视频标题（辅助识别）
        video_description: 视频描述

    Returns:
        CorrectionResult 或 None（如果纠正失败或不需要）
    """
    if not lines or len(lines) < 3:
        return None

    # 收集说话人信息
    speakers = {}
    for line in lines:
        spk = line.get("speaker_id", "")
        if spk not in speakers:
            speakers[spk] = []
        speakers[spk].append(line)

    if len(speakers) < 2:
        logger.info("[Speaker Corrector] 单说话人，无需纠正")
        return None

    # 构建 prompt
    prompt = _build_correction_prompt(lines, speakers, video_title, video_description)

    # 调用 Gemini
    try:
        response_text = _call_gemini(prompt)
    except Exception:
        logger.exception("[Speaker Corrector] Gemini 调用失败")
        return None

    # 解析结果
    try:
        return _parse_correction_response(response_text, lines, speakers)
    except Exception:
        logger.exception("[Speaker Corrector] 解析纠正结果失败")
        return None


def apply_corrections(lines: list[dict], result: CorrectionResult) -> list[dict]:
    """将纠正结果应用到 transcript lines。"""
    if not result or not result.corrections:
        return lines

    # 构建 speaker_id -> identity 映射
    identity_map = {sid.speaker_id: sid for sid in result.identities}

    corrected = []
    for line in lines:
        new_line = dict(line)
        idx = line.get("index", 0)

        if idx in result.corrections:
            new_spk = result.corrections[idx]
            new_line["speaker_id"] = new_spk
            # 更新 speaker_label
            if new_spk in identity_map:
                label = "A" if new_spk == "speaker_a" else "B"
                new_line["speaker_label"] = label

        # 更新说话人显示名称（如果 identity 有的话）
        spk_id = new_line.get("speaker_id", "")
        if spk_id in identity_map:
            ident = identity_map[spk_id]
            if ident.name:
                new_line["display_name"] = ident.name

        corrected.append(new_line)

    logger.info(
        "[Speaker Corrector] 已应用 %d 处纠正",
        result.correction_count,
    )
    return corrected


def _build_correction_prompt(
    lines: list[dict],
    speakers: dict[str, list[dict]],
    video_title: str,
    video_description: str,
) -> str:
    parts = []
    parts.append("你是一个视频转录稿的说话人识别专家。")
    parts.append("")

    if video_title:
        parts.append(f"视频标题：{video_title}")
    if video_description:
        parts.append(f"视频描述：{video_description[:300]}")
    parts.append("")

    parts.append("以下是一段视频的转录稿（已用 AI 做了说话人标注，但可能有错误）。")
    parts.append("请分析对话内容，完成两个任务：")
    parts.append("")
    parts.append("任务一：识别每个说话人的身份（姓名、角色）")
    parts.append("任务二：找出明显标注错误的片段并纠正")
    parts.append("")
    parts.append("判断依据：")
    parts.append("- 采访中，提问者通常说短句，回答者说长段")
    parts.append("- 相邻的短回应（如 Yeah, Sure, Right）通常属于对方")
    parts.append("- 旁白/介绍通常是主持人")
    parts.append("- 同一个话题的连续长段通常是同一个人")
    parts.append("")

    # 展示前 50 行的完整对话
    sample_count = min(50, len(lines))
    parts.append(f"转录稿（前 {sample_count} 段）：")
    parts.append("")
    for line in lines[:sample_count]:
        spk = line.get("speaker_id", "?")
        idx = line.get("index", 0)
        text = line.get("source_text", "")[:200]
        parts.append(f"[{idx}] {spk}: {text}")
    parts.append("")

    parts.append("请用以下 JSON 格式回复（不要加 markdown 代码块）：")
    parts.append("""{
  "identities": [
    {"speaker_id": "speaker_a", "name": "说话人真名", "role": "主持人/被采访者/旁白"},
    {"speaker_id": "speaker_b", "name": "说话人真名", "role": "主持人/被采访者/旁白"}
  ],
  "corrections": [
    {"index": 4, "from": "speaker_a", "to": "speaker_b", "reason": "简短回应，应该是对方说的"}
  ],
  "confidence": "high/medium/low"
}""")
    parts.append("")
    parts.append("如果没有需要纠正的，corrections 返回空数组。")

    return "\n".join(parts)


def _call_gemini(prompt: str) -> str:
    """调用 Gemini API。"""
    api_key = os.environ.get(DEFAULT_GEMINI_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {DEFAULT_GEMINI_API_KEY_ENV} 未设置")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        # Fallback to REST API
        return _call_gemini_rest(prompt, api_key)

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
        ),
    )
    return response.text


def _call_gemini_rest(prompt: str, api_key: str) -> str:
    """REST API fallback。"""
    import urllib.request

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{DEFAULT_MODEL}:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }).encode()

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    return data["candidates"][0]["content"]["parts"][0]["text"]


def _parse_correction_response(
    response_text: str,
    lines: list[dict],
    speakers: dict[str, list[dict]],
) -> CorrectionResult | None:
    """解析 Gemini 返回的 JSON。"""
    # 清理 markdown 代码块
    cleaned = response_text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    data = json.loads(cleaned)

    # 解析 identities
    identities = []
    for ident in data.get("identities", []):
        identities.append(SpeakerIdentity(
            speaker_id=ident.get("speaker_id", ""),
            name=ident.get("name", ""),
            role=ident.get("role", ""),
        ))

    # 解析 corrections
    corrections = {}
    for corr in data.get("corrections", []):
        idx = corr.get("index", 0)
        to_spk = corr.get("to", "")
        if idx > 0 and to_spk:
            corrections[idx] = to_spk
            logger.info(
                "[Speaker Corrector] 纠正 #%d: %s → %s (%s)",
                idx, corr.get("from", "?"), to_spk, corr.get("reason", ""),
            )

    confidence = data.get("confidence", "medium")

    if not identities:
        return None

    return CorrectionResult(
        identities=identities,
        corrections=corrections,
        correction_count=len(corrections),
        confidence=confidence,
    )
