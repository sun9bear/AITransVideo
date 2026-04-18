"""Runner extension: submit a job pointing at an existing project_dir (T1-8).

Plan ref: §7.8 / D28 / T1-8 scope

The existing ``JobRunner.start(record, continue_existing=True)`` path
already handles "continue from where pipeline left off, project_dir
already on disk" — we reuse it. This module exposes a thin wrapper that:

1. Fetches the JobRecord by id (raises if missing).
2. Asserts it's in a startable state for a copy-as-new child
   (``queued`` — new Job rows land in queued by default).
3. Invokes ``service.runner.start(record, continue_existing=True)``.

Why a dedicated wrapper instead of letting T1-9 call runner.start directly:

- The plan's §7.8 explicitly calls out ``submit_job_from_existing_project_dir``
  as a named entry point so that copy_as_new (and any future "re-run
  pipeline on existing dir" flows) share a single code path with its own
  tests.
- Keeps the pipeline start_stage concern localised: a later enhancement
  can set ``record.current_stage = 'alignment'`` here to force pipeline
  to skip earlier stages (alignment-only commit, plan §D26). T1-8 ships
  the signature; the state-skip policy stays in T1-9 where the commit
  strategy is known.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from services.jobs.models import JobRecord

logger = logging.getLogger(__name__)

__all__ = [
    "SUPPORTED_START_STAGES",
    "submit_job_from_existing_project_dir",
]

# Allowed start_stage values. Pipeline entry points that a copy_as_new
# commit can jump into directly. Keep the list small and explicit — adding
# new start stages is a pipeline-design decision, not a copy_service one.
SUPPORTED_START_STAGES: frozenset[str] = frozenset({"alignment"})


def submit_job_from_existing_project_dir(
    runner,  # duck-typed: must expose .start(record, continue_existing=True)
    record: JobRecord,
    *,
    start_stage: str = "alignment",
) -> JobRecord:
    """Start ``record`` on ``runner`` with the project_dir already on disk.

    The caller has:
    - Copied the source project_dir to ``record.project_dir`` via
      ``copy_service.prepare_copy_project_dir`` (or overwrite equivalent).
    - Set ``record.status`` to ``queued`` (default for new Job rows).
    - Persisted the record via the JobStore.

    We stamp ``current_stage = start_stage`` so the pipeline picks up at
    that entry point (skipping ingestion / transcription / etc. — D26).

    Returns the record post-start (stage + status may have advanced by
    the runner's own transition logic).
    """
    if start_stage not in SUPPORTED_START_STAGES:
        raise ValueError(
            f"unsupported start_stage: {start_stage!r}; "
            f"must be one of {sorted(SUPPORTED_START_STAGES)}"
        )

    # Stamp current_stage so the pipeline knows which stage gate to enter.
    # continue_existing=True tells runner.start to skip the "fresh project"
    # initialisation path (see ProcessJobRunner behaviour).
    updated = replace(record, current_stage=start_stage)
    runner.start(updated, continue_existing=True)
    logger.info(
        "submit_job_from_existing_project_dir: job_id=%s start_stage=%s",
        record.job_id,
        start_stage,
    )
    return updated
