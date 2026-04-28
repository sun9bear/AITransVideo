"""Orchestrate ``display_name`` generation for new jobs.

Sits between the pure algorithm (``src/services/jobs/display_name.py``) and
the persistence layer. The algorithm only knows about already-in-memory
values (``existing_names`` set, ``upload_sequence_today`` int); this
orchestrator lazily fetches both from injectable async callables so
production code can plug in SQLAlchemy queries and unit tests can use
in-memory fakes.

Design decision — **lazy branch-4 counter**: the counter is fetched only
when the algorithm actually needs it (YouTube placeholder / empty or
non-Chinese local filename). Detection is done by running the pure algorithm *first* without
a date/sequence, catching the ``ValueError`` that ``_branch_4_default``
raises, and then re-running with counter values populated. This skips a
wasted ``COUNT(*)`` query on local uploads whose Chinese filename can be used
directly, without duplicating the branch-4 predicate logic.

Corresponds to ``docs/plans/2026-04-18-studio-post-edit-plan.md`` §6.2 + T0-4.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable

__all__ = ["DisplayNameContext", "compute_display_name"]


_DISPLAY_NAME_MODULE: ModuleType | None = None


def _load_display_name_module() -> ModuleType:
    """Load the pure display_name module without importing services.jobs.

    ``services/jobs/__init__.py`` imports the full Job API stack, including
    runtime dependencies that the Gateway image intentionally does not carry.
    The naming algorithm itself is pure logic, so load that file directly.
    """
    global _DISPLAY_NAME_MODULE
    if _DISPLAY_NAME_MODULE is not None:
        return _DISPLAY_NAME_MODULE

    src_roots = (
        Path(__file__).resolve().parent.parent / "src",
        Path("/opt/aivideotrans/app/src"),
    )
    for src_root in src_roots:
        module_path = src_root / "services" / "jobs" / "display_name.py"
        if not module_path.is_file():
            continue

        app_root = src_root.parent
        for candidate in (str(app_root), str(src_root)):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)

        spec = importlib.util.spec_from_file_location(
            "_avt_display_name_logic", module_path
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _DISPLAY_NAME_MODULE = module
        return module

    raise ImportError("Cannot locate src/services/jobs/display_name.py")


ExistingNamesFetcher = Callable[[str], Awaitable[set[str]]]
BranchFourCounterFetcher = Callable[[str, date], Awaitable[int]]


@dataclass(slots=True, frozen=True)
class DisplayNameContext:
    """All the pre-fetched signals a naming decision needs.

    - ``source_type``: ``"youtube_url"`` or ``"local_video"``.
    - ``source_ref``: the URL / uploaded file path (passed through for
      parity with the pure algorithm — it does not surface in the
      user-visible result).
    - ``user_id``: scopes both the existing_names query and the branch-4
      counter. Collision and numbering are strictly per-user.
    - ``user_local_date``: the user's calendar date in their timezone;
      shapes the ``"油管视频/上传视频 YYYY-MM-DD NNN"`` placeholder.
    - ``youtube_title``: yt-dlp probe result; kept for compatibility. New
      YouTube jobs use a Chinese placeholder until S2 derives a Chinese title.
    - ``local_filename``: original upload filename as sent from the
      browser; may be ``None`` / empty.
    """

    source_type: str
    source_ref: str
    user_id: str
    user_local_date: date
    youtube_title: str | None = None
    local_filename: str | None = None


async def compute_display_name(
    ctx: DisplayNameContext,
    *,
    fetch_existing_names: ExistingNamesFetcher,
    fetch_branch4_sequence_today: BranchFourCounterFetcher,
) -> str:
    """Produce a persistable ``display_name`` for a new job.

    - ``fetch_existing_names`` is invoked exactly once; the result is used
      for both the base-name collision check and any ``_xxxx`` suffix
      retry inside the pure algorithm.
    - ``fetch_branch4_sequence_today`` is invoked at most once, and only
      when the algorithm falls through to branch 4. It should return
      "how many branch-4 names this user already owns on the given date".
      The orchestrator passes ``count + 1`` as the new sequence number.
    """
    display_name_module = _load_display_name_module()
    DisplayNameInput = display_name_module.DisplayNameInput
    generate_display_name = display_name_module.generate_display_name

    existing_names = await fetch_existing_names(ctx.user_id)

    base_input = DisplayNameInput(
        source_type=ctx.source_type,
        source_ref=ctx.source_ref,
        youtube_title=ctx.youtube_title,
        local_filename=ctx.local_filename,
    )

    try:
        # Cheap path: branches 1 / 3 never touch date/sequence.
        return generate_display_name(base_input, existing_names)
    except ValueError:
        # Algorithm fell through to branch 4 → pay the counter round trip.
        count = await fetch_branch4_sequence_today(
            ctx.user_id, ctx.user_local_date
        )
        branch4_input = DisplayNameInput(
            source_type=ctx.source_type,
            source_ref=ctx.source_ref,
            youtube_title=ctx.youtube_title,
            local_filename=ctx.local_filename,
            user_local_date=ctx.user_local_date,
            upload_sequence_today=count + 1,
        )
        return generate_display_name(branch4_input, existing_names)
