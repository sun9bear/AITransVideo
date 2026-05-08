"""Tests for the FAQ resolver, PII redactor, and JobContextForAI sanitizer."""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace


def test_search_faq_finds_jianying_question():
    from gateway.support_knowledge import search_faq

    hits = search_faq("怎么导出剪映草稿？")
    assert hits, "expected at least one FAQ hit for 剪映草稿"
    ids = [h["id"] for h in hits]
    assert "faq_jianying_draft" in ids


def test_search_faq_returns_top_k_at_most():
    from gateway.support_knowledge import search_faq

    hits = search_faq("视频", top_k=2)
    assert len(hits) <= 2


def test_search_faq_empty_for_unrelated_query():
    from gateway.support_knowledge import search_faq

    hits = search_faq("今天天气怎么样")
    # Stop-word free — unlikely overlap with curated tags.
    assert hits == []


def test_redact_pii_strips_phone_email_order():
    from gateway.support_knowledge import redact_pii

    raw = "我手机号 13800138000，邮箱 user@example.com，订单 ord_abcXYZ123"
    out = redact_pii(raw)
    assert "13800138000" not in out
    assert "user@example.com" not in out
    assert "ord_abcXYZ123" not in out
    assert "[手机号]" in out
    assert "[邮箱]" in out
    assert "[订单号]" in out


def test_redact_pii_strips_url_secrets():
    from gateway.support_knowledge import redact_pii

    raw = "https://example.com/x?token=abcdef&other=1"
    out = redact_pii(raw)
    assert "abcdef" not in out


def test_sanitize_job_context_returns_only_allowlisted_fields():
    from gateway.support_knowledge import (
        JobContextForAI,
        sanitize_job_context_for_ai,
    )

    fake_job = SimpleNamespace(
        job_id="job_abc",
        display_name="测试任务",
        status="failed",
        service_mode="studio",
        source_duration_seconds=125.5,
        # Internal fields we MUST NOT see in the output:
        project_dir="/opt/aivideotrans/data/projects/job_abc",
        workspace_dir="/opt/aivideotrans/work/job_abc",
        manifest_path="/opt/aivideotrans/data/projects/job_abc/manifest.json",
        # Error summary with raw payload:
        error_summary={
            "category": "tts_failed",
            "user_visible_message": "配音生成失败，可重试",
            "message": "Traceback (most recent call last)... internal stuff /opt/aivideotrans/secret",
            "stacktrace": "raw stacktrace text",
        },
        review_gate={"jianying_draft": True, "materials_pack": False},
        updated_at=None,
    )
    ctx = sanitize_job_context_for_ai(fake_job)
    assert isinstance(ctx, JobContextForAI)
    fields = asdict(ctx)
    assert set(fields.keys()) == {
        "job_id",
        "display_name",
        "status",
        "service_mode",
        "source_duration_seconds",
        "error_category",
        "user_visible_error",
        "available_artifacts",
        "updated_at",
    }
    assert ctx.error_category == "tts_failed"
    assert ctx.user_visible_error == "配音生成失败，可重试"
    assert "jianying_draft" in ctx.available_artifacts
    assert "materials_pack" not in ctx.available_artifacts


def test_sanitize_job_context_drops_internal_paths_in_user_message():
    from gateway.support_knowledge import sanitize_job_context_for_ai

    fake_job = SimpleNamespace(
        job_id="job_abc",
        display_name="x",
        status="failed",
        service_mode="express",
        source_duration_seconds=1.0,
        error_summary={
            "category": "alignment_failed",
            "user_visible_message": (
                "对齐失败，请重试：参考 /opt/aivideotrans/work/internal "
                "或 D:\\Claude\\internal_path 下的日志"
            ),
        },
        review_gate=None,
        updated_at=None,
    )
    ctx = sanitize_job_context_for_ai(fake_job)
    msg = ctx.user_visible_error or ""
    assert "/opt/aivideotrans" not in msg
    assert "D:\\Claude" not in msg
    assert "[内部路径]" in msg


def test_sanitize_job_context_handles_none():
    from gateway.support_knowledge import sanitize_job_context_for_ai

    assert sanitize_job_context_for_ai(None) is None


def test_get_plan_facts_returns_codes():
    """When the gateway plan_catalog import succeeds, we expect a list of
    plans with at least the standard codes. If the import fails (e.g. in
    a stripped test env), the function still returns a stable shape."""
    from gateway.support_knowledge import get_plan_facts

    facts = get_plan_facts()
    assert "plans" in facts
    assert isinstance(facts["plans"], list)
