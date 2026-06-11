"""APF P0 T5 — 匿名预览本地规则预筛（AD-2：gateway 侧免费 sanity 预筛）。

本模块是注入 backend adapter 的 ``ComplianceFn`` 真实现：
对上传阶段可得的文本（文件名）跑本地大陆合规规则，**零付费调用、
零网络、同步**。重合规（ASR teaser + LLM）由 pipeline 既有 stage
跑一次（AD-2），本层只拦明显违规的文件名。

约束：
* 不 import services.jobs / 任何 clone provider / tts provider；
* 输出为 APF2 契约 ``ComplianceResult``（src.services.anonymous_preview_intake），
  与契约 ``evaluate_compliance_result`` 的 fail-closed 语义对接；
* reason 只给固定文案，不回显用户输入（防日志注入/泄漏）。
"""

from __future__ import annotations

from src.services.anonymous_preview_intake import ComplianceResult, ComplianceStatus
from src.services.content_compliance import MainlandChinaContentComplianceReviewer


def prescreen_filename(filename: str) -> ComplianceResult:
    """对匿名上传的文件名做本地规则预筛。

    命中本地禁忌规则 → BLOCK（契约 evaluate_compliance_result 映射为
    REJECTED）；未命中 → PASS。任何内部异常 fail-closed 为 BLOCK。
    """
    try:
        reviewer = MainlandChinaContentComplianceReviewer()
        local_result = reviewer.review(
            transcript_lines=[],
            video_title=str(filename or ""),
        )
        if local_result.blocked:
            return ComplianceResult(
                status=ComplianceStatus.BLOCK,
                reason="local prescreen blocked (details redacted)",
                audit_metadata={
                    "prescreen": "filename",
                    "policy_code": str(local_result.policy_code),
                },
            )
        return ComplianceResult(
            status=ComplianceStatus.PASS,
            reason="local prescreen passed",
            audit_metadata={"prescreen": "filename"},
        )
    except Exception:
        # fail-closed：预筛自身故障一律拒绝，不放行。
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            reason="local prescreen failure (details redacted)",
            audit_metadata={"prescreen": "filename", "failure": "internal"},
        )
