from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterable


POLICY_CODE = "mainland_china_content_compliance"
POLICY_VERSION = "mainland_china_content_v1_2026_04"
DEFAULT_REPORT_RELATIVE_PATH = Path("compliance") / "content_review.json"
LLM_PROMPT_KEY = "content_compliance"
DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT = """你是中国大陆网络视频内容合规审核员。请基于《互联网信息服务管理办法》第十五条和《网络信息内容生态治理规定》第六条、第七条，对视频标题、简介和英文转录稿做内容审核。

审核目标：
1. 如果内容明确属于违法或不良信息，应判定为 block。
2. 如果内容存在明显上下文风险但证据不足，应判定为 needs_manual_review。
3. 如果只是新闻报道、教育科普、历史分析、反诈骗提醒、批判性讨论，且没有宣扬、煽动、教学实施或传播违法内容，不要因为关键词本身直接判定违规。
4. 只输出 JSON，不要输出 Markdown 或解释文字。

可参考的风险类别：
- 反对宪法确定的基本原则
- 危害国家安全、泄露国家秘密、颠覆国家政权、破坏国家统一
- 损害国家荣誉和利益
- 歪曲、丑化、亵渎、否定英雄烈士事迹和精神
- 宣扬恐怖主义、极端主义或煽动实施相关活动
- 煽动民族仇恨、民族歧视，破坏民族团结
- 破坏国家宗教政策，宣扬邪教和封建迷信
- 散布谣言，扰乱经济秩序和社会秩序
- 散布淫秽、色情、赌博、暴力、凶杀、恐怖或者教唆犯罪
- 侮辱诽谤他人，侵害名誉、隐私和其他合法权益
- 血腥残忍、低俗媚俗、性暗示、歧视等不良信息

返回 JSON 结构：
{
  "decision": "pass | block | needs_manual_review",
  "confidence": 0.0,
  "reason": "一句中文理由",
  "categories": [
    {
      "rule_id": "risk_category",
      "label": "风险类别中文名",
      "legal_basis": "对应法规依据",
      "evidence": [
        {
          "source": "video_title | video_description | transcript",
          "line_index": 1,
          "quote": "不超过 120 字的证据摘录"
        }
      ]
    }
  ]
}

视频信息：
- 标题：__VIDEO_TITLE__
- 简介：__VIDEO_DESCRIPTION__
- 来源类型：__SOURCE_TYPE__
- 来源标识：__SOURCE_REF__

第一层本地规则结果：
__LOCAL_FINDINGS_JSON__

转录稿：
__TRANSCRIPT_BODY__
"""


@dataclass(frozen=True, slots=True)
class ContentComplianceRule:
    rule_id: str
    label: str
    legal_basis: str
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentComplianceEvidence:
    source: str
    line_index: int | None
    start_ms: int | None
    end_ms: int | None
    snippet: str
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line_index": self.line_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "snippet": self.snippet,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True, slots=True)
class ContentComplianceFinding:
    rule_id: str
    label: str
    legal_basis: str
    evidence: tuple[ContentComplianceEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "label": self.label,
            "legal_basis": self.legal_basis,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ContentComplianceResult:
    status: str
    policy_code: str
    policy_version: str
    checked_at: str
    source_type: str
    source_ref: str
    video_title: str
    message: str
    findings: tuple[ContentComplianceFinding, ...]
    layers: dict[str, object] | None = None

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "policy_code": self.policy_code,
            "policy_version": self.policy_version,
            "checked_at": self.checked_at,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "video_title": self.video_title,
            "message": self.message,
            "findings": [finding.to_dict() for finding in self.findings],
        }
        if self.layers is not None:
            payload["layers"] = self.layers
        return payload


@dataclass(frozen=True, slots=True)
class LLMContentComplianceResult:
    status: str
    confidence: float
    message: str
    findings: tuple[ContentComplianceFinding, ...]
    model_name: str = ""
    raw_response: str = ""
    error: str | None = None

    @property
    def blocked(self) -> bool:
        return self.status in {"blocked", "needs_manual_review"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "confidence": self.confidence,
            "message": self.message,
            "model_name": self.model_name,
            "findings": [finding.to_dict() for finding in self.findings],
        }
        if self.raw_response:
            payload["raw_response"] = self.raw_response
        if self.error:
            payload["error"] = self.error
        return payload


class ContentPolicyViolationError(RuntimeError):
    """Raised when source content must not continue through localization."""

    def __init__(self, result: ContentComplianceResult) -> None:
        super().__init__(result.message)
        self.result = result


def is_content_compliance_enabled() -> bool:
    return _env_flag("AVT_CONTENT_COMPLIANCE_ENABLED", default=True)


def is_content_compliance_llm_enabled() -> bool:
    return _env_flag("AVT_CONTENT_COMPLIANCE_LLM_ENABLED", default=True)


def is_content_compliance_llm_fail_closed() -> bool:
    return _env_flag("AVT_CONTENT_COMPLIANCE_LLM_FAIL_CLOSED", default=False)


def load_content_compliance_prompt_template() -> str:
    settings_path = (
        Path(os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config"))
        / "admin_settings.json"
    )
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT
    prompts = data.get("review_prompts", {})
    if isinstance(prompts, dict):
        override = prompts.get(LLM_PROMPT_KEY)
        if isinstance(override, str) and override.strip():
            return override.strip()
    return DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT


def validate_content_compliance_llm_response(raw_response: str) -> None:
    data = _loads_llm_json(raw_response)
    decision = str(data.get("decision", "") or data.get("status", "")).strip().lower()
    allowed = {
        "pass",
        "approved",
        "approve",
        "ok",
        "block",
        "blocked",
        "reject",
        "rejected",
        "needs_manual_review",
        "manual_review",
        "review",
        "uncertain",
    }
    if decision not in allowed:
        raise ValueError("LLM content compliance response is missing a valid decision")


class MainlandChinaContentComplianceReviewer:
    """Deterministic baseline for Mainland China content-compliance gating.

    This is intentionally small and replaceable. It provides a local default
    gate for tests and development without introducing a mandatory external
    moderation API dependency into the main path.
    """

    def __init__(
        self,
        *,
        rules: Iterable[ContentComplianceRule] | None = None,
        max_evidence_per_rule: int = 5,
    ) -> None:
        self.rules = tuple(rules or DEFAULT_MAINLAND_CHINA_RULES)
        self.max_evidence_per_rule = max(1, int(max_evidence_per_rule))

    def review(
        self,
        *,
        transcript_lines: Iterable[object],
        video_title: str = "",
        video_description: str = "",
        source_type: str = "",
        source_ref: str = "",
    ) -> ContentComplianceResult:
        findings: list[ContentComplianceFinding] = []
        evidence_items = list(
            _iter_evidence_inputs(
                transcript_lines=transcript_lines,
                video_title=video_title,
                video_description=video_description,
            )
        )

        for rule in self.rules:
            rule_evidence: list[ContentComplianceEvidence] = []
            for item in evidence_items:
                matched_terms = tuple(
                    term for term in rule.terms if _term_in_text(term, item["text"])
                )
                if not matched_terms:
                    continue
                rule_evidence.append(
                    ContentComplianceEvidence(
                        source=str(item["source"]),
                        line_index=item["line_index"],
                        start_ms=item["start_ms"],
                        end_ms=item["end_ms"],
                        snippet=_compact_snippet(str(item["text"])),
                        matched_terms=matched_terms,
                    )
                )
                if len(rule_evidence) >= self.max_evidence_per_rule:
                    break
            if rule_evidence:
                findings.append(
                    ContentComplianceFinding(
                        rule_id=rule.rule_id,
                        label=rule.label,
                        legal_basis=rule.legal_basis,
                        evidence=tuple(rule_evidence),
                    )
                )

        status = "blocked" if findings else "approved"
        if findings:
            labels = "、".join(finding.label for finding in findings[:3])
            message = (
                "视频内容审核未通过，疑似包含中国大陆法律法规禁止传播的内容"
                f"（{labels}）。该任务已停止，不能进入后续翻译、配音和草稿生成流程。"
            )
        else:
            message = "视频内容合规审核通过。"

        return ContentComplianceResult(
            status=status,
            policy_code=POLICY_CODE,
            policy_version=POLICY_VERSION,
            checked_at=datetime.now(timezone.utc).isoformat(),
            source_type=str(source_type or ""),
            source_ref=str(source_ref or ""),
            video_title=str(video_title or ""),
            message=message,
            findings=tuple(findings),
        )

    def write_report(
        self,
        result: ContentComplianceResult,
        *,
        project_dir: str | Path,
        relative_path: str | Path = DEFAULT_REPORT_RELATIVE_PATH,
    ) -> Path:
        report_path = Path(project_dir) / relative_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return report_path.resolve(strict=False)


class LLMContentComplianceReviewer:
    """Second-layer semantic reviewer for cases not clearly blocked locally."""

    def __init__(
        self,
        *,
        generate_json: Callable[[str], str],
        prompt_template: str | None = None,
        model_name: str = "",
        max_transcript_chars: int = 24000,
    ) -> None:
        self.generate_json = generate_json
        self.prompt_template = prompt_template or DEFAULT_LLM_CONTENT_COMPLIANCE_PROMPT
        self.model_name = str(model_name or "")
        self.max_transcript_chars = max(4000, int(max_transcript_chars))

    def review(
        self,
        *,
        transcript_lines: Iterable[object],
        local_result: ContentComplianceResult,
        video_title: str = "",
        video_description: str = "",
        source_type: str = "",
        source_ref: str = "",
    ) -> LLMContentComplianceResult:
        prompt = self.build_prompt(
            transcript_lines=transcript_lines,
            local_result=local_result,
            video_title=video_title,
            video_description=video_description,
            source_type=source_type,
            source_ref=source_ref,
        )
        raw_response = self.generate_json(prompt)
        return self.parse_response(raw_response)

    def build_prompt(
        self,
        *,
        transcript_lines: Iterable[object],
        local_result: ContentComplianceResult,
        video_title: str,
        video_description: str,
        source_type: str,
        source_ref: str,
    ) -> str:
        transcript_body = _build_transcript_body(
            transcript_lines,
            max_chars=self.max_transcript_chars,
        )
        local_findings_json = json.dumps(
            [finding.to_dict() for finding in local_result.findings],
            ensure_ascii=False,
            indent=2,
        )
        replacements = {
            "__VIDEO_TITLE__": str(video_title or ""),
            "__VIDEO_DESCRIPTION__": str(video_description or ""),
            "__SOURCE_TYPE__": str(source_type or ""),
            "__SOURCE_REF__": str(source_ref or ""),
            "__LOCAL_FINDINGS_JSON__": local_findings_json,
            "__TRANSCRIPT_BODY__": transcript_body,
            "{video_title}": str(video_title or ""),
            "{video_description}": str(video_description or ""),
            "{source_type}": str(source_type or ""),
            "{source_ref}": str(source_ref or ""),
            "{local_findings_json}": local_findings_json,
            "{transcript_body}": transcript_body,
        }
        prompt = self.prompt_template
        for key, value in replacements.items():
            prompt = prompt.replace(key, value)
        return prompt

    def parse_response(self, raw_response: str) -> LLMContentComplianceResult:
        data = _loads_llm_json(raw_response)
        decision = str(data.get("decision", "") or data.get("status", "")).strip().lower()
        if decision in {"pass", "approved", "approve", "ok"}:
            status = "approved"
        elif decision in {"block", "blocked", "reject", "rejected"}:
            status = "blocked"
        elif decision in {"needs_manual_review", "manual_review", "review", "uncertain"}:
            status = "needs_manual_review"
        else:
            status = "needs_manual_review"

        reason = str(data.get("reason", "") or data.get("message", "")).strip()
        if not reason:
            reason = "大模型审核结果需要人工复核。" if status == "needs_manual_review" else "大模型审核完成。"

        findings = _parse_llm_findings(data)
        if status in {"blocked", "needs_manual_review"} and not findings:
            findings = (
                ContentComplianceFinding(
                    rule_id="llm_policy_risk",
                    label="大模型识别的内容合规风险",
                    legal_basis=_LEGAL_BASIS_ILLEGAL,
                    evidence=(
                        ContentComplianceEvidence(
                            source="llm_reason",
                            line_index=None,
                            start_ms=None,
                            end_ms=None,
                            snippet=_compact_snippet(reason),
                            matched_terms=(),
                        ),
                    ),
                ),
            )

        return LLMContentComplianceResult(
            status=status,
            confidence=_coerce_confidence(data.get("confidence")),
            message=reason,
            findings=findings,
            model_name=self.model_name,
            raw_response=raw_response,
        )


def make_content_compliance_llm_error(
    exc: Exception,
    *,
    model_name: str = "",
) -> LLMContentComplianceResult:
    return LLMContentComplianceResult(
        status="error",
        confidence=0.0,
        message="大模型内容合规审核未完成。",
        findings=(),
        model_name=str(model_name or ""),
        error=str(exc),
    )


def combine_content_compliance_results(
    *,
    local_result: ContentComplianceResult,
    llm_result: LLMContentComplianceResult | None = None,
    llm_fail_closed: bool = False,
) -> ContentComplianceResult:
    local_layer = _result_layer(local_result)
    llm_layer: dict[str, object]
    if local_result.blocked:
        llm_layer = {
            "status": "skipped",
            "message": "第一层本地规则明确命中禁忌内容，未调用大模型审核。",
        }
        return _replace_result(
            local_result,
            layers={"local_rules": local_layer, "llm": llm_layer},
        )

    if llm_result is None:
        llm_layer = {"status": "skipped", "message": "大模型内容合规审核未启用或未配置。"}
        return _replace_result(
            local_result,
            message="视频内容合规审核通过（本地规则未命中，大模型审核未启用）。",
            layers={"local_rules": local_layer, "llm": llm_layer},
        )

    llm_layer = llm_result.to_dict()
    if llm_result.status == "blocked":
        return _replace_result(
            local_result,
            status="blocked",
            message=(
                "视频内容审核未通过，大模型判断疑似包含中国大陆法律法规禁止传播的内容。"
                "该任务已停止，不能进入后续翻译、配音和草稿生成流程。"
            ),
            findings=local_result.findings + llm_result.findings,
            layers={"local_rules": local_layer, "llm": llm_layer},
        )
    if llm_result.status == "needs_manual_review":
        return _replace_result(
            local_result,
            status="blocked",
            message=(
                "视频内容合规审核需要人工复核，自动流程已停止。"
                "请调整内容或联系管理员处理。"
            ),
            findings=local_result.findings + llm_result.findings,
            layers={"local_rules": local_layer, "llm": llm_layer},
        )
    if llm_result.status == "error":
        if llm_fail_closed:
            return _replace_result(
                local_result,
                status="blocked",
                message=(
                    "大模型内容合规审核不可用，系统按当前风控配置停止任务。"
                    "请稍后重试或联系管理员。"
                ),
                layers={"local_rules": local_layer, "llm": llm_layer},
            )
        return _replace_result(
            local_result,
            message="视频内容本地规则审核通过；大模型审核未完成，按当前配置继续流程。",
            layers={"local_rules": local_layer, "llm": llm_layer},
        )

    return _replace_result(
        local_result,
        message="视频内容合规审核通过（本地规则与大模型审核均未阻断）。",
        layers={"local_rules": local_layer, "llm": llm_layer},
    )


_LEGAL_BASIS_ILLEGAL = (
    "《互联网信息服务管理办法》第十五条；"
    "《网络信息内容生态治理规定》第六条"
)
_LEGAL_BASIS_UNHEALTHY = "《网络信息内容生态治理规定》第七条"


DEFAULT_MAINLAND_CHINA_RULES: tuple[ContentComplianceRule, ...] = (
    ContentComplianceRule(
        rule_id="constitutional_principles",
        label="反对宪法确定的基本原则",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "反对宪法所确定的基本原则",
            "overthrow the constitution",
        ),
    ),
    ContentComplianceRule(
        rule_id="national_security_unity",
        label="危害国家安全、破坏国家统一",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "危害国家安全",
            "泄露国家秘密",
            "颠覆国家政权",
            "破坏国家统一",
            "分裂国家",
            "subvert state power",
            "leak state secrets",
            "split the country",
            "secession propaganda",
        ),
    ),
    ContentComplianceRule(
        rule_id="national_honor_interests",
        label="损害国家荣誉和利益",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "损害国家荣誉",
            "损害国家利益",
            "harm national honor",
            "harm national interests",
        ),
    ),
    ContentComplianceRule(
        rule_id="heroes_martyrs",
        label="侵害英雄烈士名誉荣誉",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "侮辱英雄烈士",
            "诋毁英雄烈士",
            "否定英雄烈士",
            "insult heroes and martyrs",
        ),
    ),
    ContentComplianceRule(
        rule_id="terrorism_extremism",
        label="宣扬恐怖主义、极端主义",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "宣扬恐怖主义",
            "宣扬极端主义",
            "煽动恐怖活动",
            "恐怖主义宣传",
            "terrorist propaganda",
            "promote terrorism",
            "promote extremism",
            "how to make a bomb",
            "bomb making tutorial",
        ),
    ),
    ContentComplianceRule(
        rule_id="ethnic_religious_hatred",
        label="煽动民族仇恨、破坏宗教政策",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "煽动民族仇恨",
            "民族歧视",
            "破坏民族团结",
            "宣扬邪教",
            "封建迷信",
            "incite ethnic hatred",
            "ethnic discrimination",
            "cult propaganda",
        ),
    ),
    ContentComplianceRule(
        rule_id="rumors_social_order",
        label="散布谣言、扰乱社会秩序",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "散布谣言",
            "扰乱社会秩序",
            "破坏社会稳定",
            "fabricate rumors",
            "spread rumors to disrupt public order",
        ),
    ),
    ContentComplianceRule(
        rule_id="obscenity_gambling_violence_crime",
        label="淫秽色情、赌博、暴力恐怖或教唆犯罪",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "淫秽",
            "色情",
            "赌博",
            "暴力凶杀",
            "教唆犯罪",
            "online casino",
            "pornography",
            "sexual explicit content",
            "gambling platform",
            "how to commit murder",
            "how to commit fraud",
            "teach people to commit crimes",
        ),
    ),
    ContentComplianceRule(
        rule_id="defamation_privacy",
        label="侮辱诽谤或侵害他人合法权益",
        legal_basis=_LEGAL_BASIS_ILLEGAL,
        terms=(
            "侮辱诽谤",
            "侵犯隐私",
            "人肉搜索",
            "泄露个人隐私",
            "doxxing",
            "leak private information",
        ),
    ),
    ContentComplianceRule(
        rule_id="unhealthy_bloody_vulgar_discrimination",
        label="不良信息：血腥残忍、低俗或歧视内容",
        legal_basis=_LEGAL_BASIS_UNHEALTHY,
        terms=(
            "血腥残忍",
            "低俗媚俗",
            "地域歧视",
            "人群歧视",
            "graphic gore",
            "promote discrimination",
        ),
    ),
)


def _iter_evidence_inputs(
    *,
    transcript_lines: Iterable[object],
    video_title: str,
    video_description: str,
):
    if video_title.strip():
        yield {
            "source": "video_title",
            "line_index": None,
            "start_ms": None,
            "end_ms": None,
            "text": video_title,
        }
    if video_description.strip():
        yield {
            "source": "video_description",
            "line_index": None,
            "start_ms": None,
            "end_ms": None,
            "text": video_description,
        }
    for line in transcript_lines:
        text = str(getattr(line, "source_text", "") or "").strip()
        if not text:
            continue
        yield {
            "source": "transcript",
            "line_index": _coerce_optional_int(getattr(line, "index", None)),
            "start_ms": _coerce_optional_int(getattr(line, "start_ms", None)),
            "end_ms": _coerce_optional_int(getattr(line, "end_ms", None)),
            "text": text,
        }


def _term_in_text(term: str, text: str) -> bool:
    normalized_term = str(term or "").strip()
    if not normalized_term:
        return False

    normalized_text = str(text or "").casefold()
    folded_term = normalized_term.casefold()
    if _is_ascii_word_or_phrase(folded_term):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(folded_term)}(?![a-z0-9])",
            normalized_text,
        ) is not None
    return folded_term in normalized_text


def _is_ascii_word_or_phrase(value: str) -> bool:
    return value.isascii() and any(ch.isalnum() for ch in value)


def _compact_snippet(text: str, *, limit: int = 160) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _coerce_optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _build_transcript_body(
    transcript_lines: Iterable[object],
    *,
    max_chars: int,
) -> str:
    lines = list(transcript_lines)
    rows: list[str] = []
    used_chars = 0
    omitted_count = 0
    for line in lines:
        text = str(getattr(line, "source_text", "") or "").strip()
        if not text:
            continue
        line_index = _coerce_optional_int(getattr(line, "index", None))
        start_ms = _coerce_optional_int(getattr(line, "start_ms", None))
        end_ms = _coerce_optional_int(getattr(line, "end_ms", None))
        speaker_id = str(getattr(line, "speaker_id", "") or "").strip() or "speaker"
        time_range = ""
        if start_ms is not None and end_ms is not None:
            time_range = f" [{start_ms / 1000:.2f}-{end_ms / 1000:.2f}s]"
        prefix = f"#{line_index}" if line_index is not None else "#?"
        row = f"{prefix}{time_range} {speaker_id}: {text}"
        projected = used_chars + len(row) + 1
        if projected > max_chars:
            omitted_count += 1
            continue
        rows.append(row)
        used_chars = projected
    if omitted_count:
        rows.append(f"[已截断：另有 {omitted_count} 条转录未放入本次审核提示词]")
    return "\n".join(rows)


def _loads_llm_json(raw_response: str) -> dict[str, object]:
    text = str(raw_response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM content compliance response must be a JSON object")
    return payload


def _parse_llm_findings(data: dict[str, object]) -> tuple[ContentComplianceFinding, ...]:
    raw_categories = data.get("categories")
    if raw_categories is None:
        raw_categories = data.get("findings")
    if not isinstance(raw_categories, list):
        return ()

    findings: list[ContentComplianceFinding] = []
    for category in raw_categories:
        if not isinstance(category, dict):
            continue
        evidence_items: list[ContentComplianceEvidence] = []
        raw_evidence = category.get("evidence")
        if isinstance(raw_evidence, list):
            for item in raw_evidence[:5]:
                if not isinstance(item, dict):
                    continue
                quote = str(
                    item.get("quote")
                    or item.get("snippet")
                    or item.get("text")
                    or ""
                ).strip()
                if not quote:
                    continue
                evidence_items.append(
                    ContentComplianceEvidence(
                        source=str(item.get("source") or "llm"),
                        line_index=_coerce_optional_int(item.get("line_index")),
                        start_ms=_coerce_optional_int(item.get("start_ms")),
                        end_ms=_coerce_optional_int(item.get("end_ms")),
                        snippet=_compact_snippet(quote),
                        matched_terms=(),
                    )
                )
        label = str(category.get("label") or category.get("rule_id") or "").strip()
        if not label and not evidence_items:
            continue
        findings.append(
            ContentComplianceFinding(
                rule_id=str(category.get("rule_id") or "llm_policy_risk"),
                label=label or "大模型识别的内容合规风险",
                legal_basis=str(category.get("legal_basis") or _LEGAL_BASIS_ILLEGAL),
                evidence=tuple(evidence_items),
            )
        )
    return tuple(findings)


def _coerce_confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if confidence > 1.0 and confidence <= 100.0:
        confidence = confidence / 100.0
    return min(1.0, max(0.0, confidence))


def _result_layer(result: ContentComplianceResult) -> dict[str, object]:
    payload = result.to_dict()
    payload.pop("layers", None)
    return payload


def _replace_result(
    result: ContentComplianceResult,
    *,
    status: str | None = None,
    message: str | None = None,
    findings: tuple[ContentComplianceFinding, ...] | None = None,
    layers: dict[str, object] | None = None,
) -> ContentComplianceResult:
    return ContentComplianceResult(
        status=status if status is not None else result.status,
        policy_code=result.policy_code,
        policy_version=result.policy_version,
        checked_at=result.checked_at,
        source_type=result.source_type,
        source_ref=result.source_ref,
        video_title=result.video_title,
        message=message if message is not None else result.message,
        findings=findings if findings is not None else result.findings,
        layers=layers if layers is not None else result.layers,
    )
