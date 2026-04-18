# Hermes Ops Control Plane Phase 1 Implementation Plan

> **Status:** parked (design complete, 零代码落地)  
> **Last updated:** 2026-04-11  
> **Depends-on:** `docs/plans/hermes/2026-04-11-hermes-platform-design.md`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-ready Hermes Ops Control Plane loop for AIVideoTrans by integrating Hermes-agent with controlled backend APIs, persistence, admin surfaces, and low-noise Telegram delivery.

**Architecture:** Treat Hermes-agent as an existing runtime foundation, not something to rebuild inside this repo. Phase 1 adds an AIVideoTrans-side integration layer in `gateway` and `frontend-next`: controlled Hermes-facing read APIs, minimal control-plane tables, admin discovery surfaces, anomaly taxonomy, delivery hygiene, and deployment guidance. Hermes itself must consume only bounded backend APIs and remain analysis-only.

**Tech Stack:** Hermes-agent, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js 16, React 19, TypeScript, Docker Compose, pytest

---

## Revision Note

This implementation plan assumes Hermes-agent is used as the existing agent/runtime foundation rather than being reimplemented from scratch.

Accordingly, this Phase 1 plan focuses on the AIVideoTrans-side integration layer:

- controlled Hermes-facing read APIs inside `gateway`
- authenticated Hermes writeback via internal ingest
- internal control-plane persistence for runs, reports, insights, deliveries, and a minimal source registry
- admin discovery surfaces in `frontend-next`
- anomaly taxonomy, low-noise delivery, masking, and dedupe policies
- strict backend security boundaries between Hermes and production business data

Hermes-agent base capabilities such as runtime execution, scheduling, messaging transport, profile isolation, and future extensibility are treated as platform foundations, not Phase 1 deliverables to be rebuilt inside this repository.

## Hermes Base Capabilities vs AIVideoTrans Integration Responsibilities

### Provided by Hermes-agent Base

Treat these as existing or externally managed platform capabilities:

- independent runtime process or container
- profile-oriented execution model
- cron and recurring task execution
- Telegram or messaging gateway support
- model selection and runtime orchestration
- future support for `research` and `copilot` profiles

### Built in This Repo During Phase 1

Phase 1 work in AIVideoTrans is limited to the integration/control-plane layer:

- `gateway` Hermes ORM models and migrations
- Hermes-facing controlled read APIs
- wrappers around existing job, S2, and credits monitor logic
- internal ingest endpoint for Hermes writeback
- report, insight, run, delivery, and source persistence
- admin overview, insight, report, and run surfaces
- anomaly taxonomy and evidence normalization
- Telegram masking, dedupe, quiet-hours, and retry policy hooks
- security-boundary enforcement between Hermes and production data

## Scope Lock

### In Scope

Phase 1 must deliver a complete first `ops` loop:

- `hermes_runs`
- `hermes_reports`
- `hermes_insights`
- `hermes_report_deliveries`
- `hermes_sources`
- Hermes-facing ops read APIs
- authenticated internal ingest path
- admin discovery surfaces for overview, insights, reports, and runs
- minimal Telegram delivery path
- adaptive bundle assembly
- anomaly aggregation and circuit breaking
- feedback capture through `ignored_reason`
- masking, dedupe, and retry metadata

### Supported by Hermes Base

These are assumed to exist outside the repo implementation:

- runtime lifecycle
- scheduling
- Telegram transport primitive
- profile runtime isolation
- future memory and multi-profile expansion

### Out of Scope

Do not expand Phase 1 into:

- `research` implementation
- `copilot` implementation
- autonomous remediation
- direct production writes
- unrestricted DB or filesystem access
- true multi-node rollout
- broad external crawling

## Current Project Context and Integration Premises

Current repo and deployment facts that shape the plan:

- `gateway` already aggregates multiple admin routers, see `gateway/main.py`
- structured admin job monitoring already exists, see `gateway/admin_job_monitor_api.py`
- S2 monitor already exists, see `gateway/s2_monitor_api.py`
- credits observability already exists, see `gateway/credits_observability.py`
- admin navigation shell already exists, see `frontend-next/src/components/app-shell.tsx`
- production deployment is currently single-host Docker Compose with host networking, see `docker-compose.yml`

Key implementation judgment:

> Hermes Phase 1 is not mainly about inventing new sensors. It is about turning existing monitor surfaces into a Hermes-consumable, persistent, low-noise operational control plane.

## Phase 1 Architecture Goal

Phase 1 builds a **Hermes Ops Control Plane Integration Layer** on top of Hermes-agent.

This layer has four concrete goals:

1. unify a controlled read surface for Hermes
2. unify run/report/insight/delivery persistence through internal ingest
3. unify admin discovery and orchestration surfaces
4. unify low-noise Telegram delivery policy

## Data and Security Boundaries

### What Hermes May Access

Hermes is allowed to:

- call `/api/admin/hermes/...`
- consume bounded Hermes-facing internal read APIs
- write structured results through authenticated internal ingest
- read delivery-safe report outputs
- write control-plane records through controlled backend flows

Hermes is not allowed to:

- call broad `/api/admin/*`
- connect directly to the business database
- scan `jobs` or `projects` directories directly
- read raw production config by itself

### What Gateway May Reuse Internally

Inside `gateway`, Phase 1 may reuse or wrap:

- existing monitor helpers
- existing file-backed monitor logic
- existing structured job, S2, and credits summaries

But this reuse must stay behind adapters or wrapper APIs so Hermes still sees only the controlled boundary.

### Telegram Security Boundary

Telegram payloads may include only allowlisted content such as:

- aggregate metrics
- internal job ids
- generalized failure summaries
- delivery-safe report snippets

Telegram payloads must never include:

- user source URLs
- raw prompts
- raw payloads
- full unredacted logs
- arbitrary copied artifact content

Default rule:

> Raw content is denied by default. Only explicitly allowlisted fields may enter Telegram payloads.

### Internal Ingest Boundary

Hermes runtime must not write directly to the business database.

Preferred writeback flow:

- Hermes cron or runtime performs analysis
- Hermes may deliver directly to Telegram
- Hermes writes runs, reports, insights, and delivery audit through `/api/internal/hermes/ingest`
- `gateway` validates and persists the payload

## Hermes Source Adapter Layer

Phase 1 should treat monitor integration as a first-class design object.

### Source Types

- `internal_job_monitor`
- `internal_s2_monitor`
- `internal_shadow_credits`

### Adapter Responsibilities

Each source adapter should define:

- input origin
- Hermes-facing output shape
- evidence item format
- degraded behavior when source data is incomplete

### Expected Hermes-Facing Output Shape

Adapters should normalize outputs into:

- `source_type`
- `source_ref`
- `summary`
- optional bounded `bundle`
- `evidence_items`
- `degraded` or equivalent partial-data metadata when needed

### Degradation and Failure Cases

Adapters should degrade safely for:

- no data returned
- missing files
- malformed JSON or artifacts
- upstream timeouts
- partial field loss in summaries

The adapter layer should preserve the security boundary even when internal monitor implementations are still partly file-backed.

## Phase 1 Data Model Priorities

### P0 Core

Must exist for the first loop:

- `hermes_runs`
- `hermes_insights`

### P1 First Batch

Should ship as part of Phase 1 because the loop is not operationally complete without them:

- `hermes_reports`
- `hermes_report_deliveries`

### P2 Minimal Registry

Can stay small in Phase 1:

- `hermes_sources`

Field design rules:

- every recurring run must support `idempotency_key`
- every insight lifecycle update must be auditable
- every delivery must support dedupe and retry metadata
- every evidence reference must remain traceable to a source

If implementation pace becomes constrained, prioritize `runs + insights + minimal reports` before elaborating the source registry.

## Phase 1 API Delivery Tiers

### First-Batch Required APIs

Hermes-facing internal read APIs:

- `GET /api/admin/hermes/ops/jobs/recent-failures`
- `GET /api/admin/hermes/ops/jobs/{job_id}/bundle`
- `GET /api/admin/hermes/ops/s2/summary`
- `GET /api/admin/hermes/ops/credits/summary`

Control-plane admin APIs:

- `GET /api/admin/hermes/overview`
- `GET /api/admin/hermes/insights`
- `POST /api/admin/hermes/insights/{id}/status`
- `GET /api/admin/hermes/runs/recent`

Hermes-facing internal write API:

- `POST /api/internal/hermes/ingest`

### Same-Phase Enhancements

These are still part of Phase 1, but can land after the first loop is alive:

- report listing and detail
- delivery audit surfaces
- resend Telegram
- run detail and retry surfaces

## Phase 1 UI Delivery Tiers

### First-Batch Pages

- `Overview`
- `Insights`

### Same-Phase Enhancements

- `Reports`
- `Runs`

UI rule:

> Hermes pages are the discovery and orchestration layer. They do not replace detailed monitor pages such as `/admin/jobs` or `/admin/s2-monitor`.

## Noise Control and Delivery Hygiene

This is a first-class Phase 1 concern, not a polish item.

### Must-Have Controls

- anomaly taxonomy
- aggregation
- circuit breaking
- dedupe window
- severity threshold
- masking

### Later-In-Phase Enhancements

- shared-dependency grouping improvements
- grouped summaries across related anomalies
- quiet-hours policy
- retry and backoff tuning

Minimum anomaly types that must be wired early:

- `job_failed_spike`
- `job_stuck`
- `delivery_failure`

## Recommended Execution Waves

### Wave 1: Control-Plane Skeleton

- schema
- migration
- router registration
- minimal admin nav

### Wave 2: Controlled Read Surface

- recent failures
- adaptive job bundle
- S2 summary wrapper
- credits summary wrapper

### Wave 3: Minimum Ops Loop

- insights
- overview
- status transitions
- `ignored_reason`

### Wave 4: Delivery Hygiene

- Telegram masking
- dedupe
- retry metadata

### Wave 5: Same-Phase Enhancements

- reports detail
- runs audit
- stronger aggregation
- deployment and recovery docs

## File Structure

### Backend

- Create: `gateway/hermes_models.py`
  Responsibility: phase 1 control-plane ORM models only
- Create: `gateway/hermes_schemas.py`
  Responsibility: FastAPI response/request schemas for Hermes control-plane and ops APIs
- Create: `gateway/hermes_service.py`
  Responsibility: run/report/insight/delivery primitives, anomaly taxonomy, delivery dedupe, masking helpers
- Create: `gateway/hermes_ops_api.py`
  Responsibility: controlled Hermes-facing read APIs wrapping existing job, S2, and credits monitor logic
- Create: `gateway/hermes_ingest_api.py`
  Responsibility: authenticated Hermes writeback endpoint for runs, reports, insights, and delivery audit
- Modify: `gateway/config.py`
  Responsibility: shared-key configuration for Hermes-to-gateway read and write authentication
- Create: `gateway/hermes_control_api.py`
  Responsibility: overview, reports, insights, runs, and delivery admin APIs
- Modify: `gateway/main.py`
  Responsibility: register Hermes routers
- Modify: `gateway/models.py`
  Responsibility: keep Hermes ORM classes attached to the shared `Base` metadata used by gateway startup and Alembic
- Create: `gateway/alembic/versions/012_add_hermes_ops_control_plane.py`
  Responsibility: additive phase 1 tables and indexes

### Frontend

- Create: `frontend-next/src/types/hermes.ts`
  Responsibility: Hermes page types
- Create: `frontend-next/src/lib/admin/hermes.ts`
  Responsibility: typed fetch wrappers for Hermes admin APIs
- Create: `frontend-next/src/app/(app)/admin/hermes/page.tsx`
  Responsibility: overview page
- Create: `frontend-next/src/app/(app)/admin/hermes/reports/page.tsx`
  Responsibility: report list/detail surface
- Create: `frontend-next/src/app/(app)/admin/hermes/insights/page.tsx`
  Responsibility: insight list and status-change surface
- Create: `frontend-next/src/app/(app)/admin/hermes/runs/page.tsx`
  Responsibility: run and delivery audit page
- Modify: `frontend-next/src/components/app-shell.tsx`
  Responsibility: add Hermes navigation entry

### Tests

- Create: `tests/test_hermes_models.py`
- Create: `tests/test_hermes_service.py`
- Create: `tests/test_hermes_ops_api.py`
- Create: `tests/test_hermes_ingest_api.py`
- Create: `tests/test_hermes_control_api.py`
- Create: `tests/test_hermes_delivery.py`
- Modify: `tests/test_gateway_route_coverage.py`

### Deployment and Docs

- Modify: `docker-compose.yml`
  Responsibility: add Hermes service placeholder or resource-limited service block guidance if deployment is compose-driven in repo
- Create: `docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md`
  Responsibility: delivery policies, masking rules, anomaly thresholds, manual recovery

## Hardening Goals Mapped To Tasks

- Adaptive bundle rule: Tasks 3 and 4
- Authenticated writeback path: Task 4.5
- Aggregation and circuit breaking: Task 5
- Insight ignored reason: Tasks 1, 6, and 7
- Telegram sanitization and delivery audit: Task 6
- Deployment resource budget and isolation guidance: Task 8

### Task 1: Add Phase 1 Control-Plane Schema

**Files:**
- Create: `gateway/hermes_models.py`
- Create: `gateway/alembic/versions/012_add_hermes_ops_control_plane.py`
- Test: `tests/test_hermes_models.py`

- [ ] **Step 1: Write the failing model and migration tests**

Add tests that assert the phase 1 schema exposes:

- `hermes_runs.status` with run states
- `hermes_runs.idempotency_key`
- `hermes_insights.ignored_reason`
- `hermes_report_deliveries.delivery_origin`
- `hermes_report_deliveries.dedupe_key`
- `hermes_sources.source_type`

Suggested test targets:

```python
def test_hermes_run_model_has_idempotency_key():
    from hermes_models import HermesRun
    assert "idempotency_key" in HermesRun.__table__.columns

def test_hermes_insight_model_has_ignored_reason():
    from hermes_models import HermesInsight
    assert "ignored_reason" in HermesInsight.__table__.columns
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_models.py -q`

Expected: FAIL because the models and migration do not exist yet.

- [ ] **Step 3: Add ORM models and migration**

Implement:

- `HermesRun`
- `HermesReport`
- `HermesInsight`
- `HermesReportDelivery`
- `HermesSource`

Required columns and indexes:

```python
class HermesRun(Base):
    __tablename__ = "hermes_runs"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile = mapped_column(String(32), nullable=False)
    task_type = mapped_column(String(64), nullable=False)
    trigger_mode = mapped_column(String(32), nullable=False)
    status = mapped_column(String(16), nullable=False, server_default="queued")
    idempotency_key = mapped_column(String(160), nullable=False, unique=True)
```

```python
class HermesInsight(Base):
    __tablename__ = "hermes_insights"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    anomaly_type = mapped_column(String(64), nullable=False)
    status = mapped_column(String(16), nullable=False, server_default="open")
    ignored_reason = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_models.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_models.py gateway/alembic/versions/012_add_hermes_ops_control_plane.py tests/test_hermes_models.py
git commit -m "feat: add hermes ops control-plane schema"
```

### Task 2: Add Hermes Schemas and Service Primitives

**Files:**
- Create: `gateway/hermes_schemas.py`
- Create: `gateway/hermes_service.py`
- Test: `tests/test_hermes_service.py`

- [ ] **Step 1: Write failing service tests for anomaly taxonomy, evidence shape, and run state handling**

Cover:

- supported anomaly types
- run status validation
- evidence item normalization
- ignored-reason persistence helper

Suggested tests:

```python
def test_supported_anomaly_types_include_job_failed_spike():
    from hermes_service import ANOMALY_TYPES
    assert "job_failed_spike" in ANOMALY_TYPES

def test_build_evidence_item_normalizes_required_keys():
    from hermes_service import build_evidence_item
    item = build_evidence_item(source_type="internal_job_monitor", source_ref="job_1", label="job 1")
    assert item["source_type"] == "internal_job_monitor"
    assert item["source_ref"] == "job_1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_service.py -q`

Expected: FAIL because schemas and service helpers do not exist yet.

- [ ] **Step 3: Implement the minimal schema and service layer**

Include:

- anomaly taxonomy constants
- evidence item builder
- report priority enum or constant map
- run status helper
- `mask_for_telegram()`
- `compute_delivery_dedupe_key()`
- `should_group_as_spike()`

Core helper signatures:

```python
ANOMALY_TYPES = {
    "job_failed",
    "job_stuck",
    "job_failed_spike",
    "s2_quality_drift",
    "s2_pass3_missing_rate_high",
    "s2_model_downgrade_spike",
    "shadow_credits_capture_gap",
    "shadow_credits_delta_high",
    "delivery_failure",
}

def build_evidence_item(*, source_type: str, source_ref: str, label: str, url: str | None = None, excerpt: str | None = None, captured_at: str | None = None) -> dict: ...
def compute_delivery_dedupe_key(*, report_type: str, channel_target: str, scope_ref: str, window_key: str) -> str: ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_schemas.py gateway/hermes_service.py tests/test_hermes_service.py
git commit -m "feat: add hermes service primitives"
```

### Task 3: Implement Adaptive Job Bundle Assembly

**Files:**
- Modify: `gateway/admin_job_monitor_api.py`
- Create: `gateway/hermes_ops_api.py`
- Test: `tests/test_hermes_ops_api.py`

- [ ] **Step 1: Write failing tests for adaptive bundle behavior**

Cover:

- small log sets pass through mostly intact
- noisy progress logs get filtered
- error/warn/status-transition lines are always preserved
- result-summary is preferred when available

Suggested tests:

```python
def test_adaptive_bundle_keeps_error_and_status_events():
    from hermes_ops_api import build_job_bundle
    bundle = build_job_bundle(job_info={}, events=[...], result_summary={"error_summary": {"code": "X"}})
    assert bundle["analysis_context"]["events_kept"] >= 2
    assert bundle["analysis_context"]["has_result_summary"] is True

def test_progress_noise_is_filtered_from_large_logs():
    from hermes_ops_api import build_job_bundle
    bundle = build_job_bundle(job_info={}, events=[... many progress lines ...], result_summary=None)
    assert bundle["analysis_context"]["events_trimmed"] > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_ops_api.py -q`

Expected: FAIL

- [ ] **Step 3: Extract or wrap the existing log-trim logic into Hermes-facing bundle helpers**

Implement in `gateway/hermes_ops_api.py`:

- `GET /api/admin/hermes/ops/jobs/recent-failures`
- `GET /api/admin/hermes/ops/jobs/{job_id}/bundle`

Bundle shape:

```python
{
  "job": {...},
  "result_summary": {...} | None,
  "artifact_summary": {...} | None,
  "analysis_context": {
    "events_total": 312,
    "events_kept": 41,
    "events_trimmed": 271,
    "has_result_summary": True,
  },
  "events": [... trimmed structured events ...],
}
```

Reuse the existing smart trim in `admin_job_monitor_api.py` rather than duplicating unrelated logic.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_ops_api.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/admin_job_monitor_api.py gateway/hermes_ops_api.py tests/test_hermes_ops_api.py
git commit -m "feat: add hermes adaptive job bundles"
```

### Task 4: Wrap S2 and Credits Sources Behind Hermes Read APIs

**Files:**
- Modify: `gateway/s2_monitor_api.py`
- Modify: `gateway/credits_observability.py`
- Modify: `gateway/hermes_ops_api.py`
- Test: `tests/test_hermes_ops_api.py`
- Test: `tests/test_credits_observability.py`

- [ ] **Step 1: Write failing tests for Hermes S2 and credits source wrappers**

Cover:

- `/api/admin/hermes/ops/s2/summary`
- optional `/api/admin/hermes/ops/s2/{job_id}/bundle`
- `/api/admin/hermes/ops/credits/summary`

Suggested assertions:

```python
def test_hermes_s2_summary_exposes_monitor_shape():
    ...
    assert payload["source_type"] == "internal_s2_monitor"

def test_hermes_credits_summary_exposes_source_type():
    ...
    assert payload["source_type"] == "internal_shadow_credits"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_ops_api.py tests/test_credits_observability.py -q`

Expected: FAIL for the new Hermes wrappers

- [ ] **Step 3: Implement Hermes-facing wrappers without giving Hermes direct file or DB access**

Rules:

- Hermes calls only `hermes_ops_api`
- `hermes_ops_api` may call or reuse existing S2 and credits monitor internals
- direct file scanning stays hidden behind gateway

Required endpoints:

- `GET /api/admin/hermes/ops/s2/summary`
- `GET /api/admin/hermes/ops/credits/summary`

Optional:

- `GET /api/admin/hermes/ops/s2/{job_id}/bundle`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_ops_api.py tests/test_credits_observability.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/s2_monitor_api.py gateway/credits_observability.py gateway/hermes_ops_api.py tests/test_hermes_ops_api.py tests/test_credits_observability.py
git commit -m "feat: expose hermes s2 and credits read APIs"
```

### Task 4.5: Add Hermes Internal Ingest API

**Files:**
- Create: `gateway/hermes_ingest_api.py`
- Modify: `gateway/main.py`
- Create: `tests/test_hermes_ingest_api.py`
- Modify: `tests/test_gateway_route_coverage.py`

- [ ] **Step 1: Write failing tests for authenticated ingest**

Cover:

- shared-key authentication
- ingesting a run + report + insight payload
- ingesting delivery audit metadata for a Telegram send
- rejecting malformed or unauthorized payloads

Suggested tests:

```python
def test_ingest_requires_shared_key():
    ...
    assert response.status_code == 401

def test_ingest_persists_run_report_and_insight():
    ...
    assert payload["created"]["reports"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_ingest_api.py tests/test_gateway_route_coverage.py -q`

Expected: FAIL

- [ ] **Step 3: Implement the authenticated ingest endpoint**

Required endpoint:

- `POST /api/internal/hermes/ingest`

Behavior:

- authenticate with a shared Hermes-to-gateway service API key
- accept bounded payloads for runs, reports, insights, and delivery audit records
- persist through normal gateway models and service helpers
- reject direct DB shortcuts or ambiguous payload shapes

Add config in `gateway/config.py` for a shared read/write secret used by both Hermes-facing read APIs and internal ingest, for example:

```python
hermes_service_api_key: str | None = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_ingest_api.py tests/test_gateway_route_coverage.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_ingest_api.py gateway/config.py gateway/main.py tests/test_hermes_ingest_api.py tests/test_gateway_route_coverage.py
git commit -m "feat: add hermes internal ingest api"
```

### Task 5: Add Detector Aggregation and Circuit Breaking

**Files:**
- Modify: `gateway/hermes_service.py`
- Test: `tests/test_hermes_service.py`

- [ ] **Step 1: Write failing detector tests for grouped anomalies**

Cover:

- `job_failed_spike` grouping when failures cross threshold
- representative evidence selection
- suppression of low-priority per-job analysis during spike windows

Suggested tests:

```python
def test_failed_jobs_group_into_spike_above_threshold():
    from hermes_service import detect_failure_window
    result = detect_failure_window([...], spike_threshold=10)
    assert result["anomaly_type"] == "job_failed_spike"

def test_spike_mode_suppresses_per_job_analysis():
    from hermes_service import should_analyze_job_in_window
    assert should_analyze_job_in_window(in_spike=True, severity="low") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_service.py -q`

Expected: FAIL for the new detector behavior

- [ ] **Step 3: Implement aggregation and circuit-breaking helpers**

Implement:

- scan-window counters
- shared dependency grouping
- spike threshold config
- per-window suppression helper

Representative interface:

```python
def detect_failure_window(job_failures: list[dict], *, spike_threshold: int) -> dict | None: ...
def should_analyze_job_in_window(*, in_spike: bool, severity: str) -> bool: ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_service.py tests/test_hermes_service.py
git commit -m "feat: add hermes detector aggregation"
```

### Task 6: Add Delivery Dedupe, Sanitization, and Retry Policy

**Files:**
- Modify: `gateway/hermes_service.py`
- Create: `tests/test_hermes_delivery.py`

- [ ] **Step 1: Write failing tests for Telegram delivery hardening**

Cover:

- dedupe key generation
- masking of raw input and URLs
- grouped summary rendering
- retry count handling

Suggested tests:

```python
def test_mask_for_telegram_removes_source_urls():
    from hermes_service import mask_for_telegram
    text = mask_for_telegram("user source https://example.com/private.mp4 failed")
    assert "https://example.com/private.mp4" not in text

def test_delivery_dedupe_key_is_stable_for_same_window():
    from hermes_service import compute_delivery_dedupe_key
    a = compute_delivery_dedupe_key(report_type="ops_daily", channel_target="me", scope_ref="global", window_key="2026-04-11")
    b = compute_delivery_dedupe_key(report_type="ops_daily", channel_target="me", scope_ref="global", window_key="2026-04-11")
    assert a == b
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_delivery.py -q`

Expected: FAIL

- [ ] **Step 3: Implement delivery helper behavior**

Required rules:

- only aggregate metrics, internal job ids, and generalized failure summaries go to Telegram
- redact user source URLs
- redact prompt bodies and raw payload content
- support retry/backoff metadata on delivery audit records
- support dedupe windows and grouped summaries
- model Hermes direct Telegram sends as the default path and backend resend as a secondary path

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_delivery.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_service.py tests/test_hermes_delivery.py
git commit -m "feat: harden hermes telegram delivery"
```

### Task 7: Add Control-Plane Admin APIs and UI

**Files:**
- Create: `gateway/hermes_control_api.py`
- Modify: `gateway/main.py`
- Create: `frontend-next/src/types/hermes.ts`
- Create: `frontend-next/src/lib/admin/hermes.ts`
- Create: `frontend-next/src/app/(app)/admin/hermes/page.tsx`
- Create: `frontend-next/src/app/(app)/admin/hermes/reports/page.tsx`
- Create: `frontend-next/src/app/(app)/admin/hermes/insights/page.tsx`
- Create: `frontend-next/src/app/(app)/admin/hermes/runs/page.tsx`
- Modify: `frontend-next/src/components/app-shell.tsx`
- Test: `tests/test_hermes_control_api.py`

- [ ] **Step 1: Write failing backend tests for overview, reports, insights, and runs**

Cover:

- `GET /api/admin/hermes/overview`
- `GET /api/admin/hermes/reports`
- `GET /api/admin/hermes/insights`
- `POST /api/admin/hermes/insights/{id}/status` with `ignored_reason`
- `GET /api/admin/hermes/runs`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_control_api.py -q`

Expected: FAIL

- [ ] **Step 3: Implement the control-plane API surface**

Backend endpoints should support:

- overview cards
- report listing
- insight listing and status transitions
- run and delivery audit listing, including Hermes-direct Telegram sends ingested from runtime

Insight status update request should allow:

```python
{
  "status": "ignored",
  "ignored_reason": "transient upstream incident already covered by spike insight"
}
```

- [ ] **Step 4: Implement frontend pages and typed fetch wrappers**

UI requirements:

- Hermes overview emphasizes active anomalies and links into `/admin/jobs` and `/admin/s2-monitor`
- first render priority is `Overview` and `Insights`; `Reports` and `Runs` can land later in the same phase
- Reports page shows report summaries and delivery state
- Insights page allows `open / accepted / ignored / archived`
- Ignoring an insight supports a short optional reason
- Runs page shows recent runs and delivery failures

- [ ] **Step 5: Run backend tests**

Run: `pytest tests/test_hermes_control_api.py tests/test_gateway_route_coverage.py -q`

Expected: PASS

- [ ] **Step 6: Run frontend lint**

Run: `npm run lint`
Workdir: `D:\\Claude\\AIVideoTrans_Codex_web_mvp\\frontend-next`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gateway/hermes_control_api.py gateway/main.py frontend-next/src/types/hermes.ts frontend-next/src/lib/admin/hermes.ts frontend-next/src/app/(app)/admin/hermes frontend-next/src/components/app-shell.tsx tests/test_hermes_control_api.py tests/test_gateway_route_coverage.py
git commit -m "feat: add hermes ops control-plane admin surfaces"
```

### Task 8: Add Deployment Resource Budget and Runbook

**Files:**
- Modify: `docker-compose.yml`
- Create: `docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md`

- [ ] **Step 1: Add deployment notes or compose guidance with bounded resource budgets**

If Hermes is defined in repo-managed compose, add explicit bounds such as:

```yaml
deploy:
  resources:
    limits:
      cpus: "1.00"
      memory: 1G
```

If current deployment mode does not honor compose `deploy` limits, document equivalent runtime guidance in the runbook and keep any compose snippet clearly labeled as guidance rather than guaranteed enforcement.

- [ ] **Step 2: Write the ops runbook**

The runbook must include:

- anomaly thresholds
- spike grouping threshold
- Telegram masking rules
- dedupe window policy
- quiet-hours or rate-limit policy
- manual retry flow for deliveries
- safe defaults for single-node deployment
- rollback guidance if Hermes integration is noisy or unsafe
- complete Hermes cron job JSON examples for at least:
  - `failed-job-watch`
  - `ops-daily-report`

The examples should show realistic `script`, `prompt`, `deliver`, and auth usage against the controlled read and ingest APIs, matching the expected `~/.hermes/cron/jobs.json` shape.

- [ ] **Step 3: Smoke-check compose and docs formatting**

Run:

- `docker compose config`
  Workdir: `D:\\Claude\\AIVideoTrans_Codex_web_mvp`
- optional markdown lint or manual read-through for the runbook

Expected: compose config resolves without syntax errors when applicable

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md
git commit -m "docs: add hermes ops deployment guidance and runbook"
```

## Success Criteria

### Engineering Completion

- migration applies cleanly
- backend APIs resolve
- admin pages render
- targeted tests pass
- compose or deployment guidance is syntactically valid

### Integration Boundary

- Hermes reads only controlled Hermes-facing APIs
- Hermes writes only through authenticated internal ingest
- Hermes does not directly reach DB, `jobs`, or `projects`
- gateway remains the only integration boundary

### Operational Quality

- one anomaly window does not create an alert storm
- Telegram delivery shows only sanitized content
- overview surfaces issues faster than opening separate monitors manually
- ignored insights capture operator feedback

### Platform Readiness

- Phase 1.5, Phase 2, and Phase 3 still have clean extension points
- Phase 1 does not break the core translation pipeline or billing path

## Risk and Rollback Strategy

### Risk 1: Source Adapter Instability

- trigger: S2 or credits wrappers return partial or broken data
- impact: degraded anomaly accuracy
- mitigation: preserve `summary`-first mode and allow bundle suppression
- rollback: disable the affected source while keeping the rest of the ops loop running

### Risk 2: False Positives or Missed Grouping

- trigger: detector thresholds are too aggressive or too weak
- impact: noisy or blind overview
- mitigation: start with conservative thresholds and use `ignored_reason`
- rollback: fall back to report-only mode for the noisy anomaly type

### Risk 3: Telegram Sanitization Gap

- trigger: payload includes unsafe fields
- impact: data leakage to external channel
- mitigation: allowlist fields only, test masking helpers explicitly
- rollback: switch delivery to preview-only or disable Telegram while keeping admin persistence

### Risk 4: Boundary Erosion

- trigger: Hermes starts depending on broad admin APIs or raw files
- impact: security and maintainability regression
- mitigation: enforce all Hermes reads through `hermes_ops_api`
- rollback: reject the shortcut and keep adapters inside `gateway`

### Risk 5: Ingest Contract Drift

- trigger: Hermes runtime sends payloads the backend no longer accepts
- impact: reports or delivery audit stop appearing in admin
- mitigation: explicit ingest schema, shared tests, versioned payload examples in runbooks
- rollback: temporarily keep Telegram direct delivery active while disabling persistence for the broken payload type

## Final Verification

- [ ] Run backend test slice:

```bash
pytest tests/test_hermes_models.py tests/test_hermes_service.py tests/test_hermes_ops_api.py tests/test_hermes_ingest_api.py tests/test_hermes_control_api.py tests/test_hermes_delivery.py tests/test_gateway_route_coverage.py tests/test_credits_observability.py -q
```

- [ ] Run frontend lint:

```bash
cd frontend-next && npm run lint
```

- [ ] Run compose validation:

```bash
docker compose config
```

- [ ] Manual admin verification checklist:

- Hermes nav item is visible to admins only
- Overview page shows active anomalies and links to existing monitors
- Marking an insight as `ignored` allows an optional reason
- Telegram payload preview shows masked content only
- repeated scan windows dedupe correctly

## Notes for Implementers

- Do not let Hermes read `jobs` or `projects` directories directly. Gateway may temporarily keep file-backed monitor internals, but Hermes itself must stay behind the API boundary.
- Prefer extracting reusable trim helpers from `admin_job_monitor_api.py` over duplicating log selection rules.
- Keep `research` and `copilot` out of scope even if the schema leaves future expansion room.
- Keep commits small and reversible. The goal is a stable first operational loop, not a full platform rollout.
