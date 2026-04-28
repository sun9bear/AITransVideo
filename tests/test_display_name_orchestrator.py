"""Gateway display_name orchestrator: lifts the pure 4-branch algorithm from
``src/services/jobs/display_name.py`` into a DB-aware flow.

Responsibilities under test:

1. Fetches the user's current ``display_name`` values exactly once, passes
   them as the ``existing_names`` collision set.
2. Only calls the branch-4 sequence counter when the algorithm actually needs
   it (YouTube placeholder / empty or non-Chinese local filename). Local uploads
   with a Chinese filename must NOT issue a counter query.
3. The counter's returned value is "how many branch-4 names the user already
   owns today"; orchestrator hands ``+1`` as ``upload_sequence_today`` (so
   the first branch-4 job of the day is 001).
4. Collision resolution from the existing_names set still applies.

These tests use in-memory fake async callables (no SQLAlchemy, no fixtures).
Production wires SQL queries behind the same Callable signatures — see the
gateway module itself for the SQL side.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from gateway.display_name_orchestrator import (
    DisplayNameContext,
    compute_display_name,
)


# ---------------------------------------------------------------------------
# Test scaffolding: async-to-sync wrapper matching this project's convention
# (see tests/test_gateway_create_job.py which uses the same pattern rather
# than pytest-asyncio).
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeFetchers:
    """Records calls + returns scripted values."""

    def __init__(
        self,
        *,
        existing_names: set[str] | None = None,
        branch4_count: int = 0,
    ) -> None:
        self._existing_names = existing_names or set()
        self._branch4_count = branch4_count
        self.existing_calls: list[str] = []
        self.branch4_calls: list[tuple[str, date]] = []

    async def fetch_existing_names(self, user_id: str) -> set[str]:
        self.existing_calls.append(user_id)
        return set(self._existing_names)

    async def fetch_branch4_sequence_today(
        self, user_id: str, local_date: date
    ) -> int:
        self.branch4_calls.append((user_id, local_date))
        return self._branch4_count


# ---------------------------------------------------------------------------
# Branch 1: YouTube placeholder — counter query
# ---------------------------------------------------------------------------


def test_youtube_with_title_returns_placeholder_and_uses_counter() -> None:
    fakes = _FakeFetchers(branch4_count=2)
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title="My Short Title",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "油管视频 2026-04-20 003"
    assert fakes.existing_calls == ["u1"]
    assert fakes.branch4_calls == [("u1", date(2026, 4, 20))]


def test_youtube_placeholder_collision_gets_suffix() -> None:
    fakes = _FakeFetchers(
        existing_names={"油管视频 2026-04-20 001"},
        branch4_count=0,
    )
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title="My Title",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result != "油管视频 2026-04-20 001"
    assert result.startswith("油管视频 2026-04-20 001_")


# ---------------------------------------------------------------------------
# Branch 2: YouTube + empty title — same placeholder path
# ---------------------------------------------------------------------------


def test_youtube_empty_title_triggers_branch4_counter() -> None:
    fakes = _FakeFetchers(branch4_count=7)
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=deleted",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title="",  # M2: empty title → fall through to branch 4
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "油管视频 2026-04-20 008"  # 7 + 1
    assert fakes.branch4_calls == [("u1", date(2026, 4, 20))]


def test_youtube_none_title_triggers_branch4_counter() -> None:
    fakes = _FakeFetchers(branch4_count=0)
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=x",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title=None,
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "油管视频 2026-04-20 001"  # counter was 0 → next is 001


# ---------------------------------------------------------------------------
# Branch 3: local upload with Chinese filename — no counter query
# ---------------------------------------------------------------------------


def test_local_with_chinese_filename_skips_counter() -> None:
    fakes = _FakeFetchers()
    ctx = DisplayNameContext(
        source_type="local_video",
        source_ref="/tmp/uploads/u1/abc_vacation.mp4",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        local_filename="暑期采访.mp4",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "暑期采访"
    assert fakes.branch4_calls == []


def test_local_with_non_chinese_filename_uses_placeholder_counter() -> None:
    fakes = _FakeFetchers(branch4_count=1)
    ctx = DisplayNameContext(
        source_type="local_video",
        source_ref="/tmp/uploads/u1/abc_vacation.mp4",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        local_filename="vacation.mp4",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "上传视频 2026-04-20 002"
    assert fakes.branch4_calls == [("u1", date(2026, 4, 20))]


# ---------------------------------------------------------------------------
# Branch 4: local upload with no / blank filename — counter query
# ---------------------------------------------------------------------------


def test_local_empty_filename_falls_through_to_branch4() -> None:
    fakes = _FakeFetchers(branch4_count=4)
    ctx = DisplayNameContext(
        source_type="local_video",
        source_ref="/tmp/uploads/u1/blob",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        local_filename="",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "上传视频 2026-04-20 005"
    assert fakes.branch4_calls == [("u1", date(2026, 4, 20))]


def test_local_none_filename_falls_through_to_branch4() -> None:
    fakes = _FakeFetchers(branch4_count=0)
    ctx = DisplayNameContext(
        source_type="local_video",
        source_ref="/tmp/uploads/u1/blob",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        local_filename=None,
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result == "上传视频 2026-04-20 001"


# ---------------------------------------------------------------------------
# Collision resolution with branch 4
# ---------------------------------------------------------------------------


def test_branch4_name_also_goes_through_collision_resolution() -> None:
    """If "上传视频 2026-04-20 003" is somehow already present (e.g. the user
    manually renamed something else to match), the suffix still applies. This
    is a contract from display_name.resolve_collision — orchestrator must
    preserve it."""
    fakes = _FakeFetchers(
        existing_names={"上传视频 2026-04-20 003"},
        branch4_count=2,  # +1 = 003 → collides
    )
    ctx = DisplayNameContext(
        source_type="local_video",
        source_ref="/tmp/uploads/u1/blob",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        local_filename="",
    )
    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert result.startswith("上传视频 2026-04-20 003_")


# ---------------------------------------------------------------------------
# Efficiency contract: existing_names fetched exactly once per call
# ---------------------------------------------------------------------------


def test_existing_names_fetched_exactly_once() -> None:
    fakes = _FakeFetchers(existing_names={"a", "b"})
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=abc",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title="c",
    )
    _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )
    assert len(fakes.existing_calls) == 1, (
        "orchestrator must not re-query existing_names mid-flow — "
        "it's a user-wide snapshot, one query is enough"
    )


def test_compute_display_name_does_not_import_heavy_services_jobs_package(monkeypatch) -> None:
    """Gateway must be able to name jobs without Job API-only dependencies."""
    monkeypatch.delitem(sys.modules, "services.jobs", raising=False)
    fakes = _FakeFetchers(branch4_count=0)
    ctx = DisplayNameContext(
        source_type="youtube_url",
        source_ref="https://youtube.com/watch?v=missing-title",
        user_id="u1",
        user_local_date=date(2026, 4, 20),
        youtube_title=None,
    )

    result = _run(
        compute_display_name(
            ctx,
            fetch_existing_names=fakes.fetch_existing_names,
            fetch_branch4_sequence_today=fakes.fetch_branch4_sequence_today,
        )
    )

    assert result == "油管视频 2026-04-20 001"
    assert "services.jobs" not in sys.modules
