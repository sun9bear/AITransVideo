# Hermes Platform Design (Converged Hermes-First Version)

> **Status:** parked (design complete, 零代码落地, 无明确时间表)  
> **Last updated:** 2026-04-11  
> **Scope:** AIVideoTrans single-host production deployment, with future-ready expansion to multi-node and split deployments.  
> **Startup-condition:** 若未来需要独立 ops 控制面，先更新 `docs/plans/hermes/README.md` 添加启动决策说明。

## Goal

Build a Hermes-first internal platform for AIVideoTrans.

The long-term platform includes three bounded capabilities:

- `ops`: production anomaly detection, bounded diagnosis, reporting, and optimization suggestions
- `research`: competitor, pricing, and feature monitoring
- `copilot`: controlled internal admin question answering

Hermes must not behave like a passive reporting layer. It must be able to run on its own, detect anomalies, perform bounded root-cause analysis, produce structured findings, and push important conclusions without waiting for a human to open an admin page.

## Scope Statement

The first implementation is intentionally narrower:

- **Phase 1 is a Hermes Ops Control Plane**

Phase 1 must prove one thing well:

- Hermes can autonomously inspect internal operational sources, detect important anomalies, generate structured reports and insights, and deliver low-noise summaries through the admin and Telegram.

`research` and `copilot` remain part of the long-term platform design, but they must not block the first operational rollout.

## Current Project Context

The current AIVideoTrans deployment is a single Linux host with Docker Compose and host networking.

The production topology is:

- `app`
- `postgres`
- `next`
- `gateway`
- `caddy`

Relevant capabilities already present in the repo:

- structured job APIs and admin job monitoring
- runtime logs and access logs
- admin pages for jobs, settings, prompts, pricing, and S2 monitoring
- gateway-backed admin interfaces suitable for controlled internal expansion
- baseline credits observability for shadow metering and settlement health

These existing monitoring surfaces are not the center of the new platform. They are the current sensor layer Hermes should consume.

Relevant Hermes-agent runtime capabilities to assume as real platform foundations:

- cron jobs with optional `script` pre-collection and direct `deliver: "telegram:..."`
- built-in OpenAI-compatible API server, typically exposed locally at `http://127.0.0.1:8642/v1` in gateway mode
- Python library embedding through `AIAgent` for programmatic integration when needed

Key repo references:

- `docker-compose.yml`
- `gateway/admin_job_monitor_api.py`
- `gateway/s2_monitor_api.py`
- `gateway/credits_observability.py`
- `gateway/admin_settings.py`
- `frontend-next/src/app/(app)/admin/jobs/page.tsx`
- `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx`

## Architecture Decision

Use a backend-enhancement approach with Hermes as the active control brain, instead of either:

- a passive dashboard enhancement
- a pure Hermes sidecar with no durable platform layer
- a brand-new standalone control plane that duplicates existing backend capabilities

Recommended direction:

- keep Hermes as an independent runtime layer
- let Hermes own recurring inspection, anomaly detection, summarization, and suggestion generation
- add a new internal control plane inside the existing AIVideoTrans backend
- store Hermes outputs in new internal tables and expose them through admin APIs and pages
- treat the current job monitor, S2 monitor, and shadow-credits observability surfaces as internal Hermes data sources
- keep the design single-node now, but reserve multi-node fields and boundaries from day one

Implementation constraint:

- do not try to ship all three capability families at once
- ship `ops` first as the smallest autonomous closed loop

## System Architecture

The platform is split into four layers.

### 1. Core Business Layer

This remains the source of truth and stays unchanged as the production system:

- business services
- job lifecycle
- review workflow
- pricing and billing
- prompt and settings management
- runtime logs

Hermes does not become the pipeline executor. It consumes controlled facts produced by this layer.

### 2. Hermes Runtime Layer

Hermes runs as an independent agent layer with three logical profiles:

- `ops`
- `research`
- `copilot`

Hermes is the active observer in this design. Existing admin monitor pages remain useful, but they are evidence and drill-down surfaces, not the decision-maker.

This runtime layer has three integration surfaces that matter for AIVideoTrans:

- `cron + script + deliver` for autonomous `ops` and `research`
- API server for backend-mediated `copilot`
- Python library as a fallback or embedded integration option where API server is not the right deployment shape

### 3. Hermes Control Plane Layer

This is the new internal platform layer added to AIVideoTrans. It is implemented inside the existing backend and admin surfaces and is responsible for:

- storing Hermes runs
- storing reports
- storing structured insights
- exposing internal ingest endpoints for Hermes writeback
- storing delivery audit status for Telegram
- exposing admin pages and internal APIs

Future expansions can add research snapshots, change tracking, and copilot session history.

### 4. Delivery Layer

All important Hermes outputs follow dual delivery:

- persisted in admin backend views through controlled ingest
- optionally delivered to Telegram DM, usually by Hermes runtime directly

## Design Principles

The platform follows these non-negotiable rules.

### 1. Hermes is analysis-only in Phase 1

Hermes can:

- read controlled internal facts
- read public external information
- write reports, insights, sessions, and delivery records through controlled ingest flows

Hermes cannot:

- change production settings
- change pricing
- change prompt overrides
- change model toggles
- cancel, delete, or rerun jobs
- trigger any paid production action

### 2. Backend remains the security boundary

Hermes should consume controlled APIs exposed by the backend instead of talking directly to unrestricted databases, production config files, or broad admin APIs.

### 3. Hermes must be proactive, not page-driven

The primary workflow is:

- `collector`
- `detector`
- `analyst`
- `publisher`

Hermes should discover issues from recurring scans, not depend on humans manually opening the admin.

### 4. Rules first, LLM second

Recurring scans should first use structured facts, thresholds, and eligibility rules. LLM analysis should only run on suspicious slices or bounded bundles.

### 5. Existing monitor pages remain drill-down tools

Hermes overview and reports should surface what matters first. Existing admin pages remain the detailed follow-up surfaces for job-level and S2-level inspection.

### 6. Single-node now, multi-node later

Schema may reserve future multi-node fields, but first implementation logic must stay single-node and simple.

## Hermes Profiles and Delivery Order

## `ops` Profile

### Purpose

Production analysis assistant for:

- failed-job inspection
- stuck-job review
- S2 quality drift detection
- shadow metering and cost-signal detection
- daily operational summaries
- weekly operational review
- stability and cost optimization suggestions

### Inputs

Prefer controlled API bundles over raw file scanning.

Phase 1 should formalize three internal source types:

- `internal_job_monitor`
- `internal_s2_monitor`
- `internal_shadow_credits`

Primary inputs:

- job summaries
- job details
- job logs
- result summaries
- artifact summaries
- runtime-health aggregates
- S2 aggregate stats
- S2 per-job details
- credits summary and cost-signal aggregates

### Operating Pattern

`ops` should run as a four-stage chain:

- `collector`: fetch structured internal source data
- `detector`: apply thresholds, eligibility filters, and simple pattern rules
- `analyst`: invoke Hermes reasoning only on suspicious slices
- `publisher`: write reports and insights through internal ingest and optionally deliver directly to Telegram

This keeps Hermes proactive without turning every cron cycle into a full LLM log-analysis pass.

### Deep-Dive Rule

The existing per-job `analyze-logs` style diagnosis is useful, but it should be a second-stage drill-down tool. It should not be the default primary ingestion path for recurring scans.

### Adaptive Bundle Rule

Phase 1 should not assume raw logs are always the right payload for LLM analysis, even if larger-context models are available.

Bundle assembly should be adaptive:

- small bounded logs may be passed through nearly in full
- large noisy logs should be reduced to error, warning, status-transition, and tail-context slices
- repetitive progress output and low-signal lines should be filtered before LLM analysis
- result summaries and structured artifacts should be preferred over raw log volume when they already capture the failure mode

The goal is not only token fit. It is also latency control, cost control, and better signal density.

### Phase 1 Delivery Rule

Only `ops` is in scope for the first implementation milestone.

The first operational loop should be built around:

- `failed-job-watch`
- `stuck-job-review`
- `ops-daily-report`

After that loop is stable, expand within `ops` to:

- `s2-quality-drift-scan`
- `cost-signals-scan`
- `ops-weekly-review`

### Outputs

- reports
- insights
- overview metrics
- Telegram DM summaries for important events and scheduled reports
- delivery audit records persisted through backend ingest

### Subdomains

Phase 1 `ops` should explicitly separate these subdomains:

- `job_health`
- `s2_quality`
- `cost_signals`

## `research` Profile

### Purpose

Operating research assistant for:

- competitor monitoring
- pricing changes
- feature changes
- recurring summaries

### Delivery Status

`research` remains part of the platform target, but it is deferred until after `ops` has a stable first production loop.

When implemented, `research` should follow the same runtime split as `ops`:

- Hermes runtime performs external fetch and analysis
- gateway stores source definitions, snapshots, changes, and delivery audit

## `copilot` Profile

### Purpose

Internal admin copilot for controlled question answering over stored reports, insights, and bounded operational bundles.

### Delivery Status

`copilot` remains part of the platform target, but it is deferred until after `ops` has produced stable structured reports and insights that can be used as controlled evidence.

Preferred integration path:

- backend calls Hermes API server

Fallback integration path:

- backend embeds Hermes through the Python library when API server is not the preferred deployment mode

## Control Plane Data Model

Use multiple focused internal tables instead of a single catch-all table, but do not ship the whole schema in the first implementation.

### Phase 1 Minimum Tables

Phase 1 should ship only the smallest set needed for an autonomous `ops` loop.

#### `hermes_runs`

Purpose:

- record every Hermes execution instance

Suggested fields:

- `id`
- `profile`
- `task_type`
- `trigger_mode`
- `status`
- `idempotency_key`
- `input_ref_json`
- `summary`
- `error_message`
- `started_at`
- `finished_at`

#### `hermes_reports`

Purpose:

- store human-readable output objects for backend and Telegram delivery

Suggested fields:

- `id`
- `run_id`
- `profile`
- `report_type`
- `scope_type`
- `scope_ref`
- `title`
- `summary`
- `content_markdown`
- `content_json`
- `priority`
- `time_range_start`
- `time_range_end`
- `is_pinned`
- `created_at`

#### `hermes_insights`

Purpose:

- store structured findings and suggestions that can be tracked beyond one report

Suggested fields:

- `id`
- `run_id`
- `profile`
- `insight_type`
- `severity`
- `anomaly_type`
- `title`
- `summary`
- `details_markdown`
- `details_json`
- `entity_type`
- `entity_ref`
- `status`
- `ignored_reason`
- `created_at`
- `updated_at`

#### `hermes_report_deliveries`

Purpose:

- record Telegram DM and future delivery channels for audit, dedupe, retry metadata, and delivery provenance

Suggested fields:

- `id`
- `report_id`
- `channel_type`
- `channel_target`
- `delivery_origin`
- `delivery_status`
- `dedupe_key`
- `provider_message_id`
- `error_message`
- `retry_count`
- `delivered_at`
- `created_at`

#### `hermes_sources`

Purpose:

- maintain a minimal registry of Hermes-readable sources in Phase 1

Phase 1 source types should include at least:

- `internal_job_monitor`
- `internal_s2_monitor`
- `internal_shadow_credits`

Suggested fields:

- `id`
- `source_type`
- `name`
- `base_url_or_path`
- `config_json`
- `is_enabled`
- `created_at`
- `updated_at`

### Deferred Tables

These remain part of the long-term platform design, but they should not block the first operational rollout:

- `hermes_nodes`
- `hermes_source_snapshots`
- `hermes_source_changes`
- `hermes_copilot_sessions`
- `hermes_copilot_messages`

If needed, `hermes_nodes` can start later as an extremely small table with a single default record.

### Anomaly Taxonomy

Phase 1 should standardize anomaly types so reports, insights, dedupe, and Telegram routing all speak the same language.

Initial anomaly types should include at least:

- `job_failed`
- `job_stuck`
- `job_failed_spike`
- `s2_quality_drift`
- `s2_pass3_missing_rate_high`
- `s2_model_downgrade_spike`
- `shadow_credits_capture_gap`
- `shadow_credits_delta_high`
- `delivery_failure`

### Aggregation and Circuit Breaking

Detector logic must support fault aggregation before invoking Hermes analysis at scale.

Examples:

- if failed jobs exceed a threshold inside one scan window, prefer one `job_failed_spike` insight over per-job analysis spam
- if one shared dependency appears to be failing repeatedly, prefer one grouped anomaly with representative evidence
- if a scan window is already in a spike state, suppress or defer lower-priority per-job analysis until the spike is cleared

This protects LLM cost, prevents alert storms, and keeps Telegram usable.

### Evidence Model

Reports and insights should carry structured evidence references inside `content_json` or `details_json`.

Each evidence item should support:

- `source_type`
- `source_ref`
- `label`
- `url`
- `excerpt`
- `captured_at`

### Idempotency and Retry

Phase 1 must treat repeated cron execution as normal and safe.

Required controls:

- `hermes_runs` must support `queued/running/succeeded/failed/partial`
- each recurring task should compute an `idempotency_key` per time window or target entity
- report generation should be idempotent inside its intended time window
- Telegram deliveries should support retry with backoff and dedupe

### Internal Ingest API

Hermes runtime needs a controlled writeback path into the control plane.

Minimum requirement:

- `POST /api/internal/hermes/ingest`

This endpoint should accept authenticated writeback payloads for:

- run records
- reports
- insights
- delivery audit records
- later research snapshots and changes

The backend, not Hermes, remains responsible for validating payload shape and writing control-plane tables.

## Control Plane API Design

Expose Hermes functionality under a dedicated admin namespace:

- `/api/admin/hermes/...`

Do not mix Hermes control-plane responsibilities into unrelated job, pricing, or settings endpoints.

### Phase 1 Required Admin API Groups

#### Overview

- `GET /api/admin/hermes/overview`
- `GET /api/admin/hermes/runs/recent`
- `GET /api/admin/hermes/insights/high-priority`

#### Reports

- `GET /api/admin/hermes/reports`
- `GET /api/admin/hermes/reports/{id}`
- `GET /api/admin/hermes/reports/{id}/deliveries`
- `POST /api/admin/hermes/reports/{id}/resend-telegram`

#### Insights

- `GET /api/admin/hermes/insights`
- `GET /api/admin/hermes/insights/{id}`
- `POST /api/admin/hermes/insights/{id}/status`
- `POST /api/admin/hermes/insights/{id}/pin`

#### Runs and Delivery Audit

- `GET /api/admin/hermes/runs`
- `GET /api/admin/hermes/runs/{id}`
- `GET /api/admin/hermes/deliveries`
- `POST /api/admin/hermes/runs/{id}/retry`

### Hermes-Facing Internal Read APIs

Add a controlled read layer for Hermes instead of pointing Hermes at broad unrestricted admin APIs.

Required Phase 1 examples:

- `GET /api/admin/hermes/ops/jobs/recent-failures`
- `GET /api/admin/hermes/ops/jobs/{job_id}/bundle`
- `GET /api/admin/hermes/ops/s2/summary`
- `GET /api/admin/hermes/ops/credits/summary`

Optional early extension:

- `GET /api/admin/hermes/ops/s2/{job_id}/bundle`

The bundle endpoint should aggregate:

- job info
- logs
- result summary
- artifact summary

For Phase 1, these Hermes-facing internal APIs should wrap existing monitor surfaces instead of re-implementing the underlying logic.

Hermes must only consume these controlled Hermes-facing APIs. It must not directly call broad admin APIs, directly read business tables, or directly scan `jobs` and `projects` directories.

### Hermes Writeback API

For `ops` and `research`, Hermes should publish structured results through:

- `POST /api/internal/hermes/ingest`

This keeps writeback explicit and auditable instead of requiring Hermes to connect directly to the database.

### Deferred API Groups

These remain part of the long-term platform, but should not block the first `ops` rollout.

#### Research Sources

- `GET /api/admin/hermes/sources`
- `POST /api/admin/hermes/sources`
- `POST /api/admin/hermes/sources/{id}`
- `GET /api/admin/hermes/sources/{id}/snapshots`
- `GET /api/admin/hermes/sources/{id}/changes`

#### Copilot

- `POST /api/admin/hermes/copilot/sessions`
- `GET /api/admin/hermes/copilot/sessions`
- `GET /api/admin/hermes/copilot/sessions/{id}`
- `POST /api/admin/hermes/copilot/sessions/{id}/messages`

## Admin UI Design

Create a new admin area for Hermes as the discovery and orchestration layer, rather than scattering features into existing pages.

### Phase 1 Required Pages

Recommended pages:

- `Overview`
- `Reports`
- `Insights`
- optional `Runs`

### Overview Page

Should show:

- latest run status by profile
- highest-priority active anomalies
- today failed-job summary
- current S2 quality warnings
- current cost-signal warnings
- top high-priority insights
- delivery status summary

The overview page should optimize for "what Hermes found" rather than "what raw metrics exist".

### Report Center

Should support filters for:

- `profile`
- `report_type`
- time range
- delivery status
- priority

Report details should deep-link to the existing detailed admin surfaces when a reader needs evidence-level inspection.

### Insights Page

Should allow tracking insight lifecycle:

- `open`
- `accepted`
- `ignored`
- `archived`

When an insight is marked `ignored`, the UI should support a short optional reason. Phase 1 does not need automatic model learning from this feedback, but the reason should be stored for operator context, future suppression tuning, and later prompt refinement.

### Relationship With Existing Admin Pages

Hermes pages should not replace current detailed monitors.

Recommended pattern:

- Hermes overview, reports, and insights discover and rank issues
- existing `/admin/jobs` remains the detailed per-job drill-down surface
- existing `/admin/s2-monitor` remains the detailed S2 quality drill-down surface
- a future `/admin/credits-monitor` can remain the detailed shadow-credits drill-down surface if built

### Deferred Pages

These remain part of the long-term Hermes area, but should not block Phase 1:

- `Research`
- `Internal Copilot`

## Security Boundary

Phase 1 permission model: analysis + light write.

### Explicitly Allowed

- read controlled internal APIs
- read public external web pages
- write control-plane records
- write control-plane records through authenticated internal ingest
- deliver to Telegram directly through Hermes runtime or through backend-controlled retry paths

### Explicitly Forbidden

- modify production config
- modify pricing
- modify prompts
- modify model toggles
- cancel, delete, or rerun jobs
- trigger paid production flows

### API Boundary

Hermes must not receive broad access to all `/api/admin/*`.

Profile-specific access should be scoped:

- `ops`: controlled read + controlled report write
- `research`: source read + snapshot and report write
- `copilot`: backend-assembled context only

Even inside `ops`, recurring scans should prefer structured aggregates and bounded bundles first, then escalate to deeper analysis only for suspicious cases.

### Filesystem and Database Boundary

Hermes must not directly connect to the business database with broad credentials.

Hermes must not directly scan production `jobs` or `projects` directories.

Preferred flow:

- Hermes calls controlled backend APIs
- gateway may temporarily reuse existing file-backed monitor logic internally
- backend writes control-plane tables

This keeps the security boundary clean even if some current monitor implementations are still partly file-backed.

### Telegram Boundary

Telegram sending should be treated as a controlled delivery workflow, but direct Hermes runtime delivery is the normal path for cron-driven `ops` and `research`.

Preferred flow:

- Hermes cron or runtime produces a result
- Hermes may deliver directly to Telegram
- Hermes writes delivery audit metadata back through internal ingest
- backend stores delivery state and exposes it for admin audit

Optional secondary path:

- backend can support manual resend or replay workflows later, but this is not the primary sending path in the first rollout

Telegram delivery must apply data sanitization rules. Pushed messages should contain:

- aggregate metrics
- internal job identifiers when needed
- masked or generalized failure summaries

Telegram delivery must not include:

- raw user input
- user-provided source URLs
- sensitive prompt contents
- full unredacted logs
- unnecessary payload details copied from artifacts or request bodies

Required noise controls:

- dedupe window
- grouped summaries for related anomalies
- severity threshold for push notifications
- quiet hours or basic rate limiting

### Copilot Boundary

The admin frontend must not connect directly to unrestricted Hermes runtime endpoints.

Preferred flow:

- frontend calls backend
- backend authenticates and assembles context
- backend calls Hermes API server
- backend stores session and message history

Fallback flow:

- backend embeds Hermes through the Python library
- the same context assembly and session persistence rules still apply

## Deployment Design

### Current Recommended Shape

Keep business services unchanged and deploy Hermes separately on the same Linux host.

Hermes should run as an independent service group, not inside the existing `app` container.

Because Hermes shares the host with the production pipeline, it must be treated as a bounded auxiliary workload rather than a peer with equal resource priority.

### Physical and Logical Isolation

The long-term logical model still includes:

- `ops`
- `research`
- `copilot`

But the first deployment should prefer:

- **logical three-way isolation, physical single runtime**

That means first implementation can run as one Hermes service or container, while still keeping profile isolation at four levels:

- configuration isolation
- scheduling isolation
- storage isolation
- prompt and context isolation

### Resource Limits

The first deployment should define explicit CPU and memory limits for Hermes runtime containers or services.

These limits should ensure:

- Hermes cannot starve the main `app` workload
- spike analysis remains bounded under incident load
- background summarization and delivery work stays opportunistic, not production-critical

The exact values belong in deployment and implementation plans, but resource caps are a required part of the deployment design, not an optional tuning detail.

### Suggested Runtime Layout

Suggested host-side structure:

- `/opt/aivideotrans/hermes/`
  - `profiles/ops/`
  - `profiles/research/`
  - `profiles/copilot/`
  - `logs/`
  - `cache/`

Suggested config structure:

- `/opt/aivideotrans/config/hermes/`
  - `ops.yaml`
  - `research.yaml`
  - `copilot.yaml`
  - `telegram.yaml`
  - `sources.yaml`

### Future Multi-Node Expansion

Do not design future scaling around one central Hermes instance SSHing into every host with broad permissions.

Preferred evolution path:

- represent each deployment node in a later `hermes_nodes` table
- expose controlled node-facing data bundles or summary feeds
- let the central control plane aggregate node data
- keep research centralized
- keep copilot centralized with node-aware context scoping

Schema may reserve multi-node fields later, but first implementation logic must stay single-node.

## Repo Mapping

### Backend

Recommended gateway files:

- `gateway/hermes_models.py`
- `gateway/hermes_schemas.py`
- `gateway/hermes_service.py`
- `gateway/hermes_runs_api.py`
- `gateway/hermes_reports_api.py`
- `gateway/hermes_insights_api.py`

Deferred backend files:

- `gateway/hermes_sources_api.py`
- `gateway/hermes_copilot_api.py`

### Frontend

Recommended first admin area:

- `frontend-next/src/app/(app)/admin/hermes/page.tsx`
- `frontend-next/src/app/(app)/admin/hermes/reports/page.tsx`
- `frontend-next/src/app/(app)/admin/hermes/insights/page.tsx`
- optional `frontend-next/src/app/(app)/admin/hermes/runs/page.tsx`

Supporting files:

- `frontend-next/src/lib/admin/hermes.ts`
- `frontend-next/src/types/hermes.ts`

Deferred pages:

- `frontend-next/src/app/(app)/admin/hermes/research/page.tsx`
- `frontend-next/src/app/(app)/admin/hermes/copilot/page.tsx`

### Database Migration

Create new Alembic migrations under:

- `gateway/alembic/versions/`

Phase 1 minimum tables:

- `hermes_runs`
- `hermes_reports`
- `hermes_insights`
- `hermes_report_deliveries`
- `hermes_sources`

Deferred tables:

- `hermes_nodes`
- `hermes_source_snapshots`
- `hermes_source_changes`
- `hermes_copilot_sessions`
- `hermes_copilot_messages`

### Documentation

Recommended follow-up docs:

- `docs/hermes/HERMES_PLATFORM_OVERVIEW.md`
- `docs/hermes/HERMES_OPS_PROFILE.md`
- `docs/hermes/HERMES_SECURITY_BOUNDARY.md`
- `docs/hermes/HERMES_DEPLOYMENT_PLAN.md`

Deferred docs:

- `docs/hermes/HERMES_RESEARCH_PROFILE.md`
- `docs/hermes/HERMES_COPILOT_PROFILE.md`

## Phased Rollout

### Phase 0: Control Plane Skeleton

Deliver:

- foundational Phase 1 control-plane tables
- foundational APIs
- overview, report, and insight pages
- initial internal source records for jobs, S2, and shadow credits

### Phase 1: Minimum Ops Loop

Deliver:

- `ops` profile
- `failed-job-watch`
- `stuck-job-review`
- `ops-daily-report`
- overview visibility in admin
- internal ingest for runs, reports, insights, and delivery audit
- low-noise Telegram delivery for daily and critical summaries
- deep-link integration to existing detailed admin monitors

### Phase 1.5: Ops Expansion

Deliver:

- `s2-quality-drift-scan`
- `cost-signals-scan`
- `ops-weekly-review`
- stronger anomaly taxonomy coverage
- better dedupe and delivery controls

### Phase 2: Research

Deliver:

- `research` profile
- managed sources
- snapshots and change tracking
- pricing and feature monitoring first
- runtime-side fetch with backend-side persistence and delivery audit

### Phase 3: Copilot

Deliver:

- `copilot` integration into admin
- controlled backend-assembled question answering
- backend to Hermes API server as the primary execution path
- Python library embedding as an optional fallback path
- evidence-backed answers over stored reports, insights, and bounded bundles

## Recommended Delivery Priority

Execution priority should be:

1. Phase 0
2. Phase 1
3. Phase 1.5
4. Phase 2
5. Phase 3

Rationale:

- `ops` gives the fastest direct operational value and best demonstrates Hermes autonomy
- `research` has value but is operationally noisier and should not delay the core loop
- `copilot` should come after the structured report and insight layers exist

## Explicit Phase 1 Boundaries

Phase 1 includes:

- control-plane foundations
- `ops`
- Telegram dual delivery
- backend report and insight visibility
- autonomous anomaly detection for internal operational sources

Phase 1 excludes:

- production write operations
- real multi-node deployment
- `research` implementation
- `copilot` implementation
- unrestricted general-purpose copilot
- complex external crawling
- autonomous remediation or execution

## Open Implementation Note

This document is a design spec only. It does not define the implementation task breakdown, tests, or execution sequence in code-level detail. A separate implementation plan should be written before coding begins.
