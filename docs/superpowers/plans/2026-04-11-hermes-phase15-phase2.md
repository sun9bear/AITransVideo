# Hermes Phase 1.5 and Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Hermes Ops Control Plane with stronger `ops` anomaly coverage in Phase 1.5 and a bounded first `research` integration in Phase 2.

**Architecture:** Treat Hermes-agent as the existing runtime, scheduling, and messaging foundation. This plan adds more AIVideoTrans-side integration: stronger internal detectors and admin surfaces for `ops`, then a bounded `research` layer for managed sources, snapshot persistence, change detection, and delivery-safe summaries without turning `gateway` into a crawler, scheduler, or general intelligence platform.

**Tech Stack:** Hermes-agent, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Next.js 16, React 19, TypeScript, Docker Compose, pytest

---

## Revision Note

This implementation plan assumes Hermes-agent remains the existing runtime foundation.

Accordingly, Phase 1.5 and Phase 2 focus on AIVideoTrans-side integration work:

- extending the `ops` control plane beyond the minimum loop
- introducing bounded `research` source management and persistence
- extending internal ingest so Hermes runtime can write back snapshots and changes
- exposing new admin discovery surfaces and filters
- preserving backend security boundaries and low-noise delivery rules

Hermes runtime execution, scheduling, profile isolation, and messaging transport are treated as existing platform capabilities rather than repository deliverables.

## Hermes Base Capabilities vs AIVideoTrans Integration Responsibilities

### Provided by Hermes-agent Base

Treat these as existing or externally managed platform capabilities:

- profile-oriented runtime execution
- recurring scheduling and cron
- messaging transport primitives
- model/runtime orchestration
- future long-lived profile evolution

### Built in This Repo During Phase 1.5 and Phase 2

This repository adds only the integration/control-plane layer needed for the next phases:

- stronger `ops` detectors and admin/reporting support
- bounded Hermes-facing read wrappers for deeper S2 and credits summaries
- bounded Hermes-facing writeback through internal ingest
- source registry extensions, research snapshots, and change persistence
- bounded research admin APIs and pages
- normalization, diffing, dedupe, and delivery-safe summary behavior

## Scope Lock

### In Scope

Phase 1.5 includes:

- `s2-quality-drift-scan`
- `cost-signals-scan`
- `ops-weekly-review`
- stronger anomaly taxonomy coverage
- improved suppression, grouping, and delivery hygiene
- optional deeper ops bundles for S2 and cost drill-down

Phase 2 includes:

- managed `research` sources
- snapshot and change persistence
- pricing and feature monitoring first
- research admin page and APIs
- Telegram delivery for important research changes and periodic briefs

### Supported by Hermes Base

These are assumed to exist outside repo implementation:

- runtime lifecycle
- scheduling
- Telegram transport primitive
- profile runtime isolation
- actual external fetch execution by Hermes runtime

### Out of Scope

Do not expand this plan into:

- broad docs/blog crawling
- social media intelligence
- arbitrary browser-driven research from admin UI
- automatic opportunity generation beyond bounded summaries
- `copilot`
- true multi-node runtime behavior
- gateway-native schedulers

## Current Project Context and Phase Dependencies

This plan starts only after the Phase 1 minimum ops loop already exists.

That means the repo should already have:

- Hermes control-plane persistence for runs, reports, insights, deliveries, and sources
- Hermes-facing ops read APIs in `gateway`
- Hermes overview and insight surfaces in admin
- delivery-safe Telegram policies and dedupe foundations

Key implementation judgment:

> Phase 1.5 extends the existing ops control loop. Phase 2 adds bounded external research persistence and visibility. Neither phase should re-open the settled Phase 1 boundary that Hermes consumes only controlled backend surfaces.

## Phase 1.5 / Phase 2 Architecture Goal

Phase 1.5 and Phase 2 continue the Hermes integration layer with two bounded goals:

1. make `ops` smarter without making it noisier
2. add a first `research` surface without turning the backend into a crawler platform

## Data and Security Boundaries

### Hermes Access Boundary

Hermes may:

- call controlled Hermes-facing APIs
- write reports, insights, snapshots, and changes through backend-managed flows
- consume bounded source definitions for `research`

Authentication rule:

- Hermes runtime uses one shared `hermes_service_api_key` for both controlled read APIs and `POST /api/internal/hermes/ingest`

Hermes may not:

- read broad `/api/admin/*`
- connect directly to business DB tables
- drive arbitrary browser automation from the admin UI
- bypass source-type restrictions for research

### Gateway Reuse Boundary

Inside `gateway`, this phase may reuse:

- existing S2 monitor internals
- existing credits observability logic
- Phase 1 control-plane persistence and listing helpers

But all reuse must remain behind wrappers so Hermes still sees only the bounded API layer.

### Research Safety Boundary

Phase 2 source kinds must stay bounded to:

- `pricing_page`
- `feature_page`

Optional source kinds such as `homepage` or `changelog` may appear in schema or docs, but they must not expand implementation scope unless explicitly scheduled later.

### Research Writeback Boundary

Research fetch and analysis should run in Hermes runtime, not inside `gateway`.

Preferred flow:

- Hermes runtime fetches and analyzes the external source
- Hermes writes normalized snapshot and change payloads through `/api/internal/hermes/ingest`
- `gateway` validates, persists, and exposes admin views

## Delivery Hygiene and Bounded Expansion Rules

These phases must preserve the low-noise standards introduced in Phase 1.

### Must-Have Controls

- stronger anomaly taxonomy without alert spam
- grouped anomaly rendering for S2 and cost signals
- bounded research change summaries
- delivery dedupe for recurring research updates
- severity thresholding for Telegram research pushes

### Explicit Non-Goals

- no broad opportunity mining
- no silent expansion to arbitrary source types
- no scheduler embedded in `gateway`
- no admin-triggered freeform crawl surface

## Recommended Execution Waves

### Wave 1: Persistence and Detector Foundations

- research tables and source metadata extensions
- S2 drift detectors
- cost-signal detectors

### Wave 2: Ops Expansion Surfaces

- overview cards for S2 and cost signals
- weekly ops report support
- richer report and insight filters

### Wave 3: Research API Surface

- source CRUD
- snapshot listing
- change listing
- route registration and coverage

### Wave 4: Research Normalization and Admin UI

- pricing and feature normalization helpers
- diff helpers
- bounded research page

### Wave 5: Docs and Delivery Guidance

- research profile docs
- runbook updates
- example config templates

## File Structure

### Backend

- Modify: `gateway/hermes_models.py`
  Responsibility: add research snapshot/change tables and any source metadata needed beyond Phase 1
- Modify: `gateway/hermes_schemas.py`
  Responsibility: add ops-expansion and research request/response schemas
- Modify: `gateway/hermes_service.py`
  Responsibility: add S2 drift detectors, cost-signal detectors, spike grouping refinements, and research change-normalization helpers
- Modify: `gateway/hermes_ops_api.py`
  Responsibility: add deeper S2 bundle and cost-signal read surfaces for Hermes runtime
- Modify: `gateway/config.py`
  Responsibility: expose shared Hermes-to-gateway service auth used by both read and write paths
- Modify: `gateway/hermes_ingest_api.py`
  Responsibility: accept research snapshot and change writeback payloads from Hermes runtime
- Create: `gateway/hermes_sources_api.py`
  Responsibility: source CRUD plus snapshot/change listing for `research`
- Modify: `gateway/hermes_control_api.py`
  Responsibility: expose research summaries and extended report/insight filters
- Modify: `gateway/main.py`
  Responsibility: register `hermes_sources_api`
- Create: `gateway/alembic/versions/013_add_hermes_research_tables.py`
  Responsibility: add `hermes_source_snapshots` and `hermes_source_changes`, and extend `hermes_sources` if needed

### Frontend

- Modify: `frontend-next/src/types/hermes.ts`
  Responsibility: add phase 1.5 and research types
- Modify: `frontend-next/src/lib/admin/hermes.ts`
  Responsibility: add fetchers for research and deeper ops endpoints
- Modify: `frontend-next/src/app/(app)/admin/hermes/page.tsx`
  Responsibility: show S2 drift and cost-signal cards once Phase 1.5 lands
- Modify: `frontend-next/src/app/(app)/admin/hermes/reports/page.tsx`
  Responsibility: support weekly ops and research report types
- Modify: `frontend-next/src/app/(app)/admin/hermes/insights/page.tsx`
  Responsibility: support richer anomaly and source insight filters
- Create: `frontend-next/src/app/(app)/admin/hermes/research/page.tsx`
  Responsibility: research sources, recent snapshots, detected changes
- Modify: `frontend-next/src/components/app-shell.tsx`
  Responsibility: keep Hermes navigation current if research becomes visible in admin

### Tests

- Modify: `tests/test_hermes_service.py`
- Modify: `tests/test_hermes_ops_api.py`
- Create: `tests/test_hermes_sources_api.py`
- Create: `tests/test_hermes_research_models.py`
- Modify: `tests/test_hermes_ingest_api.py`
- Modify: `tests/test_credits_observability.py`
- Modify: `tests/test_gateway_route_coverage.py`

### Deployment and Docs

- Create: `docs/hermes/HERMES_RESEARCH_PROFILE.md`
- Create: `docs/hermes/HERMES_RESEARCH_RUNBOOK.md`
- Modify: `docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md`
- Create: `docs/hermes/examples/research_sources.example.yaml`
- Create: `docs/hermes/examples/research_profile.example.yaml`

## Milestone Split

### Phase 1.5

Focus:

- richer internal anomaly detection for `ops`
- weekly operational reporting
- stronger suppression and grouping under incident load

### Phase 2

Focus:

- bounded `research` source management
- snapshot persistence and change detection
- research UI and delivery flow

## Task Group A: Phase 1.5 Ops Expansion

### Task 1: Extend Control-Plane Schema for Research Readiness

**Files:**
- Modify: `gateway/hermes_models.py`
- Create: `gateway/alembic/versions/013_add_hermes_research_tables.py`
- Test: `tests/test_hermes_research_models.py`

- [ ] **Step 1: Write the failing model tests**

Cover:

- `hermes_source_snapshots`
- `hermes_source_changes`
- minimal useful indexes
- any required `hermes_sources` field extensions such as `kind`, `fetch_mode`, or `importance`

Suggested tests:

```python
def test_hermes_source_snapshot_model_exists():
    from hermes_models import HermesSourceSnapshot
    assert HermesSourceSnapshot.__tablename__ == "hermes_source_snapshots"

def test_hermes_source_change_model_exists():
    from hermes_models import HermesSourceChange
    assert HermesSourceChange.__tablename__ == "hermes_source_changes"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_research_models.py -q`

Expected: FAIL

- [ ] **Step 3: Add the minimal research tables and source metadata extensions**

Required table intent:

```python
class HermesSourceSnapshot(Base):
    __tablename__ = "hermes_source_snapshots"
    source_id = mapped_column(UUID(as_uuid=True), ForeignKey("hermes_sources.id"), nullable=False)
    snapshot_type = mapped_column(String(64), nullable=False)
    version_hash = mapped_column(String(128), nullable=False)
    title = mapped_column(String(255), nullable=True)
    raw_text = mapped_column(Text, nullable=True)
    normalized_json = mapped_column(JSONB, nullable=False)
```

```python
class HermesSourceChange(Base):
    __tablename__ = "hermes_source_changes"
    source_id = mapped_column(UUID(as_uuid=True), ForeignKey("hermes_sources.id"), nullable=False)
    change_type = mapped_column(String(64), nullable=False)
    title = mapped_column(String(255), nullable=False)
    summary = mapped_column(Text, nullable=False)
    change_json = mapped_column(JSONB, nullable=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_research_models.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_models.py gateway/alembic/versions/013_add_hermes_research_tables.py tests/test_hermes_research_models.py
git commit -m "feat: add hermes research persistence tables"
```

### Task 2: Add S2 Drift Detectors and Weekly Ops Report Support

**Files:**
- Modify: `gateway/hermes_service.py`
- Modify: `tests/test_hermes_service.py`

- [ ] **Step 1: Write failing detector tests for S2 drift and weekly rollups**

Cover:

- pass 3 missing-rate anomaly
- pass 2 model downgrade spike
- weekly report grouping across multiple daily runs

Suggested tests:

```python
def test_detect_s2_pass3_missing_rate_high():
    from hermes_service import detect_s2_quality_drift
    result = detect_s2_quality_drift({"pass3": {"total": 20, "missing_count": 6}})
    assert result["anomaly_type"] == "s2_pass3_missing_rate_high"

def test_build_weekly_ops_report_collects_top_anomalies():
    from hermes_service import build_weekly_ops_report
    report = build_weekly_ops_report([...])
    assert report["report_type"] == "ops_weekly"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_service.py -q`

Expected: FAIL

- [ ] **Step 3: Implement Phase 1.5 ops detectors**

Add helpers such as:

```python
def detect_s2_quality_drift(aggregate: dict) -> dict | None: ...
def build_weekly_ops_report(run_inputs: list[dict]) -> dict: ...
```

Initial S2 anomaly targets:

- `s2_pass3_missing_rate_high`
- `s2_model_downgrade_spike`
- `s2_quality_drift`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_service.py tests/test_hermes_service.py
git commit -m "feat: add hermes s2 drift detectors"
```

### Task 3: Add Cost-Signal Detectors and Credits Read Surfaces

**Files:**
- Modify: `gateway/credits_observability.py`
- Modify: `gateway/hermes_ops_api.py`
- Modify: `tests/test_credits_observability.py`
- Modify: `tests/test_hermes_ops_api.py`

- [ ] **Step 1: Write failing tests for cost metrics and outlier read surfaces**

Cover:

- cost metrics window summary
- provider breakdown
- outlier jobs
- Hermes-facing normalized credits summary

Suggested tests:

```python
def test_cost_metrics_reports_delta_and_k_values():
    ...
    assert payload["estimate_actual_delta_pct"] is not None

def test_hermes_ops_credits_summary_marks_source_type():
    ...
    assert payload["source_type"] == "internal_shadow_credits"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_credits_observability.py tests/test_hermes_ops_api.py -q`

Expected: FAIL

- [ ] **Step 3: Implement bounded credits observability expansion**

Add admin-only endpoints in `credits_observability.py`:

- `GET /api/admin/credits/cost-metrics`
- `GET /api/admin/credits/provider-breakdown`
- `GET /api/admin/credits/outliers`

Expose a simplified Hermes-facing summary in `hermes_ops_api.py` that wraps these admin surfaces rather than duplicating their SQL.

- [ ] **Step 4: Add cost-signal detection helpers**

In `hermes_service.py`, add helpers such as:

```python
def detect_cost_signals(cost_metrics: dict, outliers: dict) -> list[dict]: ...
```

Initial anomaly targets:

- `shadow_credits_capture_gap`
- `shadow_credits_delta_high`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_credits_observability.py tests/test_hermes_ops_api.py tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/credits_observability.py gateway/hermes_ops_api.py gateway/hermes_service.py tests/test_credits_observability.py tests/test_hermes_ops_api.py tests/test_hermes_service.py
git commit -m "feat: add hermes cost-signal observability"
```

### Task 4: Extend Ops Admin Surfaces for Phase 1.5

**Files:**
- Modify: `gateway/hermes_control_api.py`
- Modify: `frontend-next/src/types/hermes.ts`
- Modify: `frontend-next/src/lib/admin/hermes.ts`
- Modify: `frontend-next/src/app/(app)/admin/hermes/page.tsx`
- Modify: `frontend-next/src/app/(app)/admin/hermes/reports/page.tsx`
- Modify: `frontend-next/src/app/(app)/admin/hermes/insights/page.tsx`
- Test: `tests/test_hermes_control_api.py`

- [ ] **Step 1: Write failing tests for extended filters and weekly report visibility**

Cover:

- report listing by `ops_weekly`
- insight filtering by `s2_*` and `shadow_credits_*` anomaly types
- overview cards for S2 and cost anomalies

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_control_api.py -q`

Expected: FAIL

- [ ] **Step 3: Implement backend support for richer report and insight filtering**

Required query support:

- `report_type`
- `anomaly_type`
- `severity`
- `status`

- [ ] **Step 4: Update frontend pages**

UI additions:

- overview cards for S2 drift and cost signals
- report filters that include `ops_weekly`
- insight filters by anomaly type and severity

- [ ] **Step 5: Run tests and lint**

Run:

- `pytest tests/test_hermes_control_api.py -q`
- `npm run lint`
  Workdir: `D:\\Claude\\AIVideoTrans_Codex_web_mvp\\frontend-next`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gateway/hermes_control_api.py frontend-next/src/types/hermes.ts frontend-next/src/lib/admin/hermes.ts frontend-next/src/app/(app)/admin/hermes/page.tsx frontend-next/src/app/(app)/admin/hermes/reports/page.tsx frontend-next/src/app/(app)/admin/hermes/insights/page.tsx tests/test_hermes_control_api.py
git commit -m "feat: extend hermes ops admin surfaces"
```

## Task Group B: Phase 2 Research Rollout

### Task 5: Add Source Management and Research API Surface

**Files:**
- Create: `gateway/hermes_sources_api.py`
- Modify: `gateway/hermes_ingest_api.py`
- Modify: `gateway/config.py`
- Modify: `gateway/hermes_schemas.py`
- Modify: `gateway/hermes_service.py`
- Modify: `gateway/main.py`
- Create: `tests/test_hermes_sources_api.py`
- Modify: `tests/test_hermes_ingest_api.py`
- Modify: `tests/test_gateway_route_coverage.py`

- [ ] **Step 1: Write failing tests for source CRUD and listing**

Cover:

- create source
- update source
- list sources
- list snapshots
- list changes
- ingesting research snapshot and change payloads

Suggested tests:

```python
def test_create_research_source():
    ...
    assert payload["source_type"] == "pricing_page"

def test_list_source_changes():
    ...
    assert isinstance(payload["items"], list)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_sources_api.py tests/test_gateway_route_coverage.py -q`

Expected: FAIL

- [ ] **Step 3: Implement `hermes_sources_api.py`**

Required endpoints:

- `GET /api/admin/hermes/sources`
- `POST /api/admin/hermes/sources`
- `POST /api/admin/hermes/sources/{id}`
- `GET /api/admin/hermes/sources/{id}/snapshots`
- `GET /api/admin/hermes/sources/{id}/changes`

Phase 2 source types should start with:

- `pricing_page`
- `feature_page`

Optional but deferred in implementation:

- `homepage`
- `changelog`

- [ ] **Step 4: Extend internal ingest for research writeback**

Accept bounded Hermes runtime payloads for:

- research snapshots
- research changes
- optional delivery audit metadata for research briefs

Endpoint:

- `POST /api/internal/hermes/ingest`

Authentication:

- use the same shared `hermes_service_api_key` already used by Hermes-facing read APIs so runtime scripts and writeback stay on one bounded credential

- [ ] **Step 5: Register routes and verify routing**

Update `gateway/main.py` and route coverage tests.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_sources_api.py tests/test_gateway_route_coverage.py -q`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gateway/hermes_sources_api.py gateway/hermes_ingest_api.py gateway/config.py gateway/hermes_schemas.py gateway/hermes_service.py gateway/main.py tests/test_hermes_sources_api.py tests/test_hermes_ingest_api.py tests/test_gateway_route_coverage.py
git commit -m "feat: add hermes research source management"
```

### Task 6: Add Snapshot Normalization and Change Detection Helpers

**Files:**
- Modify: `gateway/hermes_service.py`
- Modify: `tests/test_hermes_service.py`

- [ ] **Step 1: Write failing tests for snapshot normalization and diffing**

Cover:

- stable version hash for unchanged input
- normalized pricing snapshot extraction
- normalized feature snapshot extraction
- change detection output

Suggested tests:

```python
def test_normalize_pricing_snapshot_is_stable():
    from hermes_service import normalize_pricing_snapshot
    normalized = normalize_pricing_snapshot("Pro plan $29")
    assert "version_hash" in normalized

def test_diff_snapshots_detects_price_change():
    from hermes_service import diff_source_snapshots
    change = diff_source_snapshots(old={"plans": [{"name": "Pro", "price": 29}]}, new={"plans": [{"name": "Pro", "price": 39}]})
    assert change["change_type"] == "price_changed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_hermes_service.py -q`

Expected: FAIL

- [ ] **Step 3: Implement bounded research normalization helpers**

Add helpers such as:

```python
def normalize_pricing_snapshot(raw_text: str, source_meta: dict) -> dict: ...
def normalize_feature_snapshot(raw_text: str, source_meta: dict) -> dict: ...
def diff_source_snapshots(old: dict, new: dict) -> list[dict]: ...
```

Rules:

- prefer deterministic parsing and normalization first
- keep output compact and structured
- do not invent unsupported source kinds in first rollout
- keep snapshot and change payloads aligned with the internal ingest schema

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_service.py tests/test_hermes_service.py
git commit -m "feat: add hermes research normalization helpers"
```

### Task 7: Build the Research Admin Page

**Files:**
- Modify: `frontend-next/src/types/hermes.ts`
- Modify: `frontend-next/src/lib/admin/hermes.ts`
- Create: `frontend-next/src/app/(app)/admin/hermes/research/page.tsx`

- [ ] **Step 1: Write the UI data contract first**

Define TypeScript types for:

- source rows
- snapshot rows
- change rows

- [ ] **Step 2: Implement the typed fetch wrappers**

Add:

- `getHermesSources()`
- `createHermesSource()`
- `updateHermesSource()`
- `getHermesSourceSnapshots()`
- `getHermesSourceChanges()`

- [ ] **Step 3: Implement the `research` page**

Phase 2 UI should support:

- source list
- add/edit source
- recent snapshots
- recent changes

Keep it bounded:

- pricing and feature sources first
- no embedded crawler UI
- no ad hoc browser automation from admin

- [ ] **Step 4: Run frontend lint**

Run: `npm run lint`
Workdir: `D:\\Claude\\AIVideoTrans_Codex_web_mvp\\frontend-next`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-next/src/types/hermes.ts frontend-next/src/lib/admin/hermes.ts frontend-next/src/app/(app)/admin/hermes/research/page.tsx
git commit -m "feat: add hermes research admin page"
```

### Task 8: Add Research Profile Templates and Runbook

**Files:**
- Create: `docs/hermes/HERMES_RESEARCH_PROFILE.md`
- Create: `docs/hermes/HERMES_RESEARCH_RUNBOOK.md`
- Modify: `docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md`
- Create: `docs/hermes/examples/research_sources.example.yaml`
- Create: `docs/hermes/examples/research_profile.example.yaml`

- [ ] **Step 1: Write the profile and runbook docs**

Document:

- supported source types
- fetch frequency guidance
- failure handling
- snapshot retention
- Telegram severity and dedupe guidance
- what Phase 2 explicitly does not cover
- complete Hermes cron job JSON examples for at least one `pricing_page` watcher and one `feature_page` watcher, matching `~/.hermes/cron/jobs.json`
- realistic `script`, `prompt`, `deliver`, and shared-auth usage against the controlled read and ingest APIs

- [ ] **Step 2: Add example config templates**

Example source entries:

```yaml
sources:
  - name: "Competitor A Pricing"
    source_type: "pricing_page"
    url: "https://example.com/pricing"
    enabled: true
```

- [ ] **Step 3: Review docs for consistency with the spec**

Check:

- no social-intel scope creep
- no scheduler embedded into gateway
- pricing and feature monitoring first

- [ ] **Step 4: Commit**

```bash
git add docs/hermes/HERMES_RESEARCH_PROFILE.md docs/hermes/HERMES_RESEARCH_RUNBOOK.md docs/hermes/HERMES_OPS_PHASE1_RUNBOOK.md docs/hermes/examples/research_sources.example.yaml docs/hermes/examples/research_profile.example.yaml
git commit -m "docs: add hermes research profile guidance"
```

## Success Criteria

### Engineering Completion

- research tables and route additions migrate cleanly
- Phase 1.5 detector and filter APIs resolve
- research source APIs resolve
- admin pages and updated filters render
- targeted tests and frontend lint pass

### Integration Boundary

- Hermes still reads only controlled backend surfaces
- Hermes uses the same shared `hermes_service_api_key` for controlled reads and internal ingest writes
- Hermes writes snapshots and changes only through internal ingest
- `gateway` still owns all persistence and source definitions
- no scheduler is introduced into `gateway`
- research remains bounded to supported source kinds

### Operational Quality

- S2 and cost-signal expansion improves visibility without recreating alert spam
- weekly ops reporting is visible in admin and usable for review
- research changes are stored and rendered in a structured, low-noise form
- Telegram research updates remain deduped and severity-gated

### Platform Readiness

- Phase 2 leaves clean evidence structures for later Copilot use
- research rollout does not break the stable Phase 1 ops loop

## Risk and Rollback Strategy

### Risk 1: Detector Expansion Becomes Noisy

- trigger: new S2 or cost thresholds generate too many insights
- impact: overview quality regresses
- mitigation: conservative thresholds, grouped anomaly rendering, phased rollout of anomaly types
- rollback: disable the noisy detector while preserving the rest of the ops loop

### Risk 2: Research Normalization Is Too Brittle

- trigger: pricing or feature page parsing produces unstable snapshots
- impact: false changes or unusable research summaries
- mitigation: deterministic normalization, bounded source types, snapshot/change tests
- rollback: store raw snapshot summaries only and disable diff-based changes for that source type

### Risk 3: Scope Creep Into Broad Crawling

- trigger: source schema or UI starts accepting arbitrary source kinds or crawl behavior
- impact: implementation complexity and noise balloon
- mitigation: hard-limit Phase 2 to `pricing_page` and `feature_page`
- rollback: reject unsupported kinds at API validation and hide unfinished controls from admin

### Risk 4: Shared Helpers Blur Ops and Research Boundaries

- trigger: `hermes_service.py` accumulates mixed logic without clear boundaries
- impact: maintainability and testability degrade
- mitigation: keep helpers grouped by responsibility and avoid hidden cross-phase coupling
- rollback: split noisy helpers into bounded ops/research sections before continuing

### Risk 5: Research Ingest Contract Drift

- trigger: Hermes runtime emits snapshot or change payloads the backend no longer accepts
- impact: research fetch still runs but persistence silently breaks
- mitigation: shared ingest tests, documented payload examples, bounded schema evolution
- rollback: disable writeback for the broken source type while keeping source definitions intact

## Final Verification

- [ ] Run backend test slice:

```bash
pytest tests/test_hermes_research_models.py tests/test_hermes_service.py tests/test_hermes_ops_api.py tests/test_credits_observability.py tests/test_hermes_sources_api.py tests/test_hermes_ingest_api.py tests/test_hermes_control_api.py tests/test_gateway_route_coverage.py -q
```

- [ ] Run frontend lint:

```bash
cd frontend-next && npm run lint
```

- [ ] Run compose validation if deployment examples are updated:

```bash
docker compose config
```

- [ ] Manual verification checklist:

- overview shows S2 drift and cost-signal cards
- weekly ops report appears in report center
- research page can create a source and list empty snapshots/changes cleanly
- insight filters work for `s2_*` and `shadow_credits_*` anomaly types
- no `research` page allows arbitrary raw crawling configuration beyond bounded source types

## Notes for Implementers

- Keep `research` bounded. First shipping value is stable source definition, snapshot storage, and change visibility, not broad intelligence automation.
- Do not add gateway-native schedulers. Hermes runtime or deployment orchestration owns recurring execution.
- Reuse the existing S2 and credits monitor logic where possible; do not create parallel truth sources.
- If a helper is needed by both `ops` and `research`, put it in `hermes_service.py` only if the boundary stays clean. Otherwise keep `ops` and `research` helpers separate.
- `Phase 2` should not silently depend on `copilot`. Keep evidence structures reusable, but do not implement conversation storage here.
