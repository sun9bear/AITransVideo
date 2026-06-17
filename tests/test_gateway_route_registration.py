"""Gateway route registration guards for feature routers."""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _main_src() -> str:
    return (_REPO_ROOT / "gateway" / "main.py").read_text(encoding="utf-8")


def test_anonymous_chunked_router_is_registered() -> None:
    src = _main_src()

    assert "from anonymous_preview_chunked_api import router as anonymous_preview_chunked_router" in src
    assert "app.include_router(anonymous_preview_chunked_router)" in src


def test_language_facts_route_is_registered() -> None:
    src = _main_src()

    assert "intercept_language_facts" in src
    assert 'app.get("/api/language-facts")(intercept_language_facts)' in src
