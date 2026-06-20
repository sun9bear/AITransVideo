# AutoDub-Jianying Pipeline

## Project goal
Build a Python workflow that outputs Jianying draft projects rather than rendered MP4.

## Core architecture invariants
- TTS unit is SemanticBlock, not subtitle line.
- Alignment uses DSP first, rewrite loop second.
- Subtitle retiming is mathematical, not LLM-driven.
- Pipeline target is Jianying draft output, not direct rendered MP4 as the main deliverable.
- Prefer minimal, testable, replaceable abstractions.

## Project graphs
- New sessions should read `docs/graphs/GITNEXUS_PROJECT_GRAPH.md` first, then enter the relevant subgraph by task.
- Graph index: `docs/graphs/README.md`
- Workflow core: `docs/graphs/GITNEXUS_WORKFLOW_CORE_GRAPH.md`
- CosyVoice / Mainland Worker: `docs/graphs/GITNEXUS_COSYVOICE_MAINLAND_WORKER_GRAPH.md`
- Express CosyVoice Auto-Clone: `docs/graphs/GITNEXUS_EXPRESS_COSYVOICE_AUTO_CLONE_GRAPH.md`
- Smart Auto Review: `docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`
- Anonymous Preview / Chunked Upload: `docs/graphs/GITNEXUS_ANONYMOUS_PREVIEW_FUNNEL_GRAPH.md`
- Jianying draft delivery: `docs/graphs/GITNEXUS_JIANYING_DRAFT_DELIVERY_GRAPH.md`
- Review flow: `docs/graphs/GITNEXUS_REVIEW_GRAPH.md`
- Editing / Post-Edit / Regeneration: `docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md`
- Storage / Delivery / R2: `docs/graphs/GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md`
- Commercialization: `docs/graphs/GITNEXUS_COMMERCIALIZATION_GRAPH.md`
- Free Tier / MiMo VoiceClone: `docs/graphs/GITNEXUS_FREE_TIER_GRAPH.md`
- Support / Notifications / Announcements: `docs/graphs/GITNEXUS_SUPPORT_NOTIFICATIONS_GRAPH.md`
- Admin / Ops / Calibration: `docs/graphs/GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md`
- Benchmark / Quality / Cost: `docs/graphs/GITNEXUS_BENCHMARK_QUALITY_COST_GRAPH.md`
- Pan Backup / Archive / Restore: `docs/graphs/GITNEXUS_PAN_BACKUP_GRAPH.md`
- Use these graph docs as the fast orientation layer before deeper code reads when the task is architecture-sensitive or the codebase is unfamiliar.

## Current execution-phase rules
- Treat the current commercialization and frontend work as a staged v2 migration, not a big-bang rewrite.
- Gateway is the source of truth for plan catalog, trial rules, prices, and entitlements. Frontend consumes these facts and must not become the final pricing or entitlement source of truth.
- Prefer incremental migration over replacement for auth, billing, and payment flows. Reuse existing session and payment infrastructure where possible, with explicit migration paths.
- Keep the frontend split conceptually aligned to `marketing`, `auth`, and `app`. Do not let marketing or auth pages inherit workspace-only UI chrome by accident.
- Keep code lightweight, testable, and reversible. Avoid large speculative frameworks or abstractions before the current phase proves they are needed.
- In tests, local development, and default paths, prefer mocks/stubs/fakes over live external services.
- Do not introduce real external API dependencies into the main path or test path unless the task explicitly requires it and the project developer has approved that stage.
- `main.py` and `pytest` must remain runnable in a clean local environment.
- Team seats, reviewer seats, full minute-level usage ledgering, and full auto-renew/second-channel payment flows are later-stage capabilities unless the current task explicitly brings them into scope.
- For marketing, auth, billing, and payment-facing UX, optimize primarily for Chinese-language users: Chinese copy should read naturally, CTA wording should fit Chinese product expectations, and trust cues should match Chinese SaaS/payment habits.

## Production deployment
- US host deployment instructions live in `docs/deployment/US_HOST_PRODUCTION_DEPLOYMENT.md`.
- The only production Compose entrypoint is `/opt/aivideotrans/docker-compose.yml`.
- Deploy from `/opt/aivideotrans`, not from `/opt/aivideotrans/app`:
  `docker compose --env-file /opt/aivideotrans/config/.env up -d --build`
- Keep the server root Compose file aligned with the repository `docker-compose.yml`.
- Production build contexts are supplied by `/opt/aivideotrans/config/.env`:
  `AIVIDEOTRANS_APP_BUILD_CONTEXT=/opt/aivideotrans/app`,
  `AIVIDEOTRANS_NEXT_BUILD_CONTEXT=/opt/aivideotrans/app/frontend-next`,
  `AIVIDEOTRANS_GATEWAY_BUILD_CONTEXT=/opt/aivideotrans/app/gateway`.
- Do not keep divergent Compose files under `/opt/aivideotrans` and `/opt/aivideotrans/app`; that caused the previous captcha build-arg drift.
- Never run `docker compose down -v` in production unless explicitly asked and the PostgreSQL/data volume impact has been accepted.

## Review guidelines
When reviewing pull requests for this repository:

- Prioritize correctness, regressions, architectural drift, and missing tests over style or naming suggestions.
- Treat the following as non-negotiable architecture invariants:
  - TTS unit must remain `SemanticBlock`, not subtitle line.
  - Alignment should stay DSP-first; rewrite loops are fallback logic, not the primary mechanism.
  - Subtitle retiming should stay mathematical/deterministic, not LLM-driven.
  - The pipeline target is Jianying draft output, not direct rendered MP4 as the main deliverable.
- Treat the current execution-phase rules as default review boundaries:
  - Gateway should remain the source of truth for plan, trial, pricing, and entitlement facts.
  - Frontend should consume commercial facts, not redefine them.
  - Auth and payment changes should be incremental and migration-aware, not big-bang rewrites.
  - Prefer mocks/stubs/fakes over live integrations in tests and local default paths.
  - Keep abstractions small, replaceable, and easy to test.
- Flag changes that make `main.py` harder to run, break CLI behavior, or risk `pytest` failing in a clean local environment.
- Flag changes that introduce unapproved real external APIs, production service calls, or hard runtime network dependencies into tests or the default local path.
- Flag changes that duplicate or drift plan/pricing/trial facts away from the gateway source of truth.
- Flag changes that smuggle later-stage commercialization scope into earlier tasks, especially team seats, reviewer seats, full usage ledgering, or full auto-renew flows.
- Flag changes that bypass an incremental migration path for auth, session, billing, or payment models.
- Flag changes that introduce heavyweight frameworks, unnecessary indirection, or tightly coupled abstractions without clear benefit.
- Flag missing or weak tests when behavior changes, especially around segmentation, alignment, retiming, draft generation, and pipeline orchestration.
- Flag missing or weak tests when behavior changes in marketing/auth/billing/payment flows, especially around catalog truth, trial gating, auth/session transitions, checkout, and webhook-driven state changes.
- Be skeptical of suggestions that move logic from deterministic code into prompts/LLM calls unless there is a strong project-specific reason.
- For user-facing marketing/auth/payment surfaces, prefer Chinese-first clarity and trustworthiness over literal translation or generic Silicon Valley-style copy.
- Prefer actionable findings with file/behavior impact. Skip low-value style feedback unless it hides a real maintenance or correctness risk.
