from __future__ import annotations

from pathlib import Path


PAGE_PATH = Path(__file__).resolve().parents[1] / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin" / "pricing" / "page.tsx"


def _read_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def test_free_plan_uses_task_quota_copy_instead_of_minutes() -> None:
    page = _read_page()

    assert 'label="免费任务额度"' in page
    assert 'label="免费任务额度(次)"' in page
    assert "free_quota_total} 次" in page

    assert 'label="免费额度"' not in page
    assert "free_quota_total} 分钟" not in page


def test_free_plan_surfaces_credit_gift_and_migration_note() -> None:
    page = _read_page()

    assert 'label="赠送点数"' in page
    assert "freeGrantCredits" in page
    assert "当前 Free 仍同时受旧任务额度与新点数赠送约束" in page
