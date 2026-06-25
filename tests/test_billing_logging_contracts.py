"""TU-08 · 计费 & 付费路径结构化日志契约守卫。

使用 inspect.getsource 做源码断言（同 test_tts_fallback_observability.py 惯例），
不调用任何真实外部服务。
"""

from __future__ import annotations

import inspect


class TestTTSGeneratorBillingLogContracts:
    def test_metering_skip_uses_logger(self):
        from services.tts import tts_generator

        src = inspect.getsource(tts_generator)
        assert "tts_metering_skip" in src, "tts_generator 必须有结构化日志 tts_metering_skip（metering 异常审计）"

    def test_tts_retry_uses_logger(self):
        from services.tts import tts_generator

        src = inspect.getsource(tts_generator)
        assert "tts_segment_attempt_failed" in src, (
            "tts_generator 必须有结构化日志 tts_segment_attempt_failed（重试路径）"
        )

    def test_minimax_retry_uses_logger(self):
        from services.tts import tts_generator

        src = inspect.getsource(tts_generator)
        assert "minimax_request_retry" in src, (
            "tts_generator 必须有结构化日志 minimax_request_retry（MiniMax 重试路径）"
        )

    def test_existing_fallback_contracts_intact(self):
        """TU-08 不得破坏 T7 既有契约日志字符串。"""
        from services.tts import tts_generator

        src = inspect.getsource(tts_generator)
        assert "tts_fallback_triggered" in src
        assert "free_voiceclone_fallback_to_preset" in src
        assert "tts_fallback_failed" in src


class TestTranslatorLLMLogContracts:
    def test_llm_fallback_uses_logger(self):
        from services.gemini import translator

        src = inspect.getsource(translator)
        assert "llm_fallback_triggered" in src, (
            "translator 必须有结构化日志 llm_fallback_triggered（LLM fallback chain 审计）"
        )

    def test_llm_legacy_path_uses_logger(self):
        from services.gemini import translator

        src = inspect.getsource(translator)
        assert "llm_router_legacy_path" in src, (
            "translator 必须有结构化日志 llm_router_legacy_path（legacy router 审计）"
        )

    def test_llm_metering_skip_uses_logger(self):
        from services.gemini import translator

        src = inspect.getsource(translator)
        assert "llm_metering_skip" in src, "translator 必须有结构化日志 llm_metering_skip（metering 异常审计）"


class TestProcessBillingLogContracts:
    def test_smart_billing_inconsistency_uses_logger(self):
        from pipeline import process as pipeline_process

        src = inspect.getsource(pipeline_process)
        assert "smart_billing_inconsistency" in src, (
            "process.py 必须有结构化日志 smart_billing_inconsistency（reservation 与 task_id 不一致）"
        )

    def test_smart_register_billed_failed_uses_logger(self):
        from pipeline import process as pipeline_process

        src = inspect.getsource(pipeline_process)
        assert "smart_register_billed_failed" in src, (
            "process.py 必须有结构化日志 smart_register_billed_failed（register-billed 失败路径）"
        )

    def test_job_metering_reported_uses_logger(self):
        from pipeline import process as pipeline_process

        src = inspect.getsource(pipeline_process)
        assert "job_metering_reported" in src, "process.py 必须有结构化日志 job_metering_reported（metering 上报成功）"

    def test_no_stale_no_logger_comment(self):
        """过时注释「no module-level logger configured」不能残留。"""
        from pipeline import process as pipeline_process

        src = inspect.getsource(pipeline_process)
        assert "no module-level logger configured" not in src, (
            "process.py 中「no module-level logger configured」注释应在 TU-08 后删除"
        )


class TestASRTranscriberLogContracts:
    def test_assemblyai_retry_uses_logger(self):
        from services.assemblyai import transcriber

        src = inspect.getsource(transcriber)
        assert "assemblyai_request_retry" in src, (
            "assemblyai/transcriber 必须有结构化日志 assemblyai_request_retry（付费 ASR 重试）"
        )

    def test_gemini_transcribe_lifecycle_uses_logger(self):
        from services.gemini import transcriber

        src = inspect.getsource(transcriber)
        assert "gemini_transcribe_start" in src, "gemini/transcriber 必须有结构化日志 gemini_transcribe_start"
