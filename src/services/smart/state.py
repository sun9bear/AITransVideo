"""Smart MVP state-machine plumbing — F1 marker channel + F3 effective mode.

This module covers two pieces of plan 2026-05-13 §4.2 / §4.3 / §6.0.6:

1. **F1 — `[SMART_STATE]` marker channel.** Pipeline (subprocess of
   process_runner) cannot directly mutate JobRecord. It emits a stdout
   line with prefix ``[SMART_STATE]`` followed by JSON. process_runner's
   ``_record_line`` parses it via ``parse_smart_state_marker`` and writes
   the parsed dict into ``JobRecord.smart_state`` through the JobStore
   atomic update path. The Gateway mirror picks it up via the metering
   callback whitelist (see ``gateway/job_intercept.py::update_job_metering``).

2. **F3 — effective pipeline mode.** A smart job that has been downgraded
   (``smart_state.status in {downgraded_to_studio, fail_and_refunded}``)
   must, on rerun (user clicks /continue after handoff), traverse the
   Studio human-review control flow rather than re-trigger auto-review.
   ``record.service_mode`` stays ``"smart"`` (audit fact, plus Gateway
   routing/billing still smart-priced); only the **pipeline-internal
   smart-aware branches** read ``derive_effective_pipeline_mode`` to
   decide whether to invoke the auto layer.

Both functions are pure and stdlib-only — safe to import from
``src/pipeline/process.py`` (subprocess context) and from runner.

Tests: ``tests/test_smart_skeleton_acceptance.py``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping


SMART_STATE_MARKER_PREFIX = "[SMART_STATE]"

# Statuses that mean "Smart auto path is no longer in charge of this job".
# A pipeline rerun MUST switch to Studio control flow when smart_state.status
# is one of these — otherwise /continue after handoff would loop the same
# auto-review failure (plan §6.0.6).
_SMART_HANDOFF_OR_TERMINAL_STATUSES = frozenset(
    {"downgraded_to_studio", "fail_and_refunded"}
)

# Smart statuses that allow a user-facing handoff into Studio post-edit /
# Jianying draft (plan §4.3 末段 + §6.6 contract). A smart job is editable
# when the smart pipeline has either succeeded ("completed") or formally
# handed off to Studio ("downgraded_to_studio"). Still-running / paused-
# for-clone / refunded jobs must NOT enter editing.
_SMART_STATE_EDITABLE_STATUSES = frozenset(
    {"completed", "downgraded_to_studio"}
)

# All service_modes that user-facing Studio gates (enter-edit, Jianying
# draft) MUST accept. Plan §4.3 末段 + Codex 第二轮 F3 + 第六轮 F3.
# Smart additionally needs the smart_state.status secondary check via
# ``is_editable_smart_state`` — accepting "smart" in service_mode alone
# would let in-flight smart jobs into editing.
EDITABLE_SERVICE_MODES = frozenset({"studio", "smart"})


def emit_smart_state_marker(state: Mapping[str, Any]) -> None:
    """Print a ``[SMART_STATE] {json}`` line to stdout.

    Mirrors ``_build_web_review_marker`` pattern (process_runner.py:502 /
    process.py:3645). The runner's per-line parser will pick this up and
    apply it to JobRecord.smart_state via JobStore.update_job.

    Use ``flush=True`` so the marker reaches the runner's stdout pump
    immediately — pipeline frames may run for many seconds before the
    next print() flushes the buffer otherwise.

    Caller MUST pass JSON-serialisable values only. Non-trivial objects
    raise TypeError here (visible in pipeline logs) rather than producing
    a malformed marker that the runner silently drops.
    """
    payload = json.dumps(dict(state), ensure_ascii=False, sort_keys=True)
    print(f"{SMART_STATE_MARKER_PREFIX} {payload}", flush=True)


def parse_smart_state_marker(line: str) -> dict[str, Any] | None:
    """Inverse of ``emit_smart_state_marker``.

    Returns the parsed dict on a well-formed marker line, ``None``
    otherwise (the runner's ``_record_line`` keeps walking other parsers
    on None — a malformed marker should not abort log processing).

    Mirrors ``_parse_web_review_marker`` shape (process_runner.py:784).
    """
    normalized = line.strip()
    if not normalized.startswith(SMART_STATE_MARKER_PREFIX):
        return None
    raw_payload = normalized.removeprefix(SMART_STATE_MARKER_PREFIX).strip()
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _read_field(record: Any, key: str) -> Any:
    """Read a field from either an attribute-style object (JobRecord
    dataclass, SimpleNamespace) or a Mapping (the dict returned by
    ``JobRecord.to_dict()`` that the real pipeline path uses — see
    process.py:1426 ``_jr = _job_record.to_dict()``).

    The previous implementation used ``getattr()`` only, which silently
    returned ``None`` when ``record`` was a dict — making
    ``derive_effective_pipeline_mode()`` always answer ``"express"`` on
    the production path. Codex 第八轮 review F3 fix.
    """
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def derive_effective_pipeline_mode(record: Any) -> str:
    """Return the effective service_mode that pipeline-internal smart-aware
    branches should use.

    Contract (plan §6.0.6):
      - Non-smart jobs: returns ``record.service_mode`` unchanged
      - Smart jobs whose ``smart_state.status`` is downgraded_to_studio or
        fail_and_refunded: returns ``"studio"`` (so on /continue the
        pipeline traverses Studio human-review control flow, not auto)
      - All other smart states (None / running / completed /
        clone_blocked_waiting_retry): returns ``"smart"``

    Note: this MUST NOT be used for Gateway routing/billing/queue —
    those continue to read ``record.service_mode`` (which stays "smart"
    as audit fact). Only pipeline-internal branches use the effective
    derivation. See plan §4.3 末段 for the routing-vs-pipeline split.

    Accepts any object with ``service_mode`` and ``smart_state`` attrs
    (dataclass JobRecord, dict via JobRecord.to_dict(), SimpleNamespace,
    etc.). Mapping support is critical because the real pipeline call
    site at src/pipeline/process.py:1426 reads ``_jr =
    _job_record.to_dict()`` — the dict form is the production shape.
    """
    service_mode = _read_field(record, "service_mode")
    if service_mode != "smart":
        # Express / studio / unknown — return as-is; effective derivation
        # is a no-op for non-smart jobs.
        return service_mode if service_mode else "express"

    smart_state = _read_field(record, "smart_state") or {}
    status = smart_state.get("status") if isinstance(smart_state, Mapping) else None
    if status in _SMART_HANDOFF_OR_TERMINAL_STATUSES:
        # Smart audit fact preserved on record.service_mode; pipeline
        # control flow now studio (plan §6.0.6 / §6.5).
        return "studio"
    return "smart"


def is_editable_smart_state(smart_state: Any) -> bool:
    """Return whether a smart job's ``smart_state`` allows entering
    Studio post-edit / Jianying draft (the user-facing editable paths).

    Per plan §4.3 末段 + §6.6:
      - ``completed`` — smart pipeline succeeded; user CAN enter Studio
        post-edit for fine-tuning
      - ``downgraded_to_studio`` — smart auto failed and formally handed
        off; user CAN take over via Studio human-review
      - ``running`` / ``clone_blocked_waiting_retry`` / ``fail_and_refunded``
        / None / missing — user MUST NOT enter editing (job not in a
        post-edit-ready state)

    Accepts dict (the canonical ``smart_state`` JSON shape) or None;
    anything else is treated as "no editable state" (fail-closed).
    """
    if not isinstance(smart_state, Mapping):
        return False
    # P3e-4a：smart 预览任务（smart_preview_mode=True）只产 3min 水印 teaser、stream-only
    # （P3e-3b/3d）——绝不可进入任何"可编辑 / 可导出"路径（Studio post-edit / 剪映 draft），
    # 否则经 segments / copy_as_new / 剪映 zip 暴露完整段落文本与音频。在共享判定单点
    # fail-closed，统一覆盖 enter_editing / 剪映 draft gate / JianyingDraftRunner 三处消费者。
    if smart_state.get("smart_preview_mode") is True:
        return False
    return smart_state.get("status") in _SMART_STATE_EDITABLE_STATUSES
