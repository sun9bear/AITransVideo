# Hermes Phase 3 Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, evidence-backed internal Hermes Copilot to the AIVideoTrans admin panel, starting with `ops_qa` and `job_diagnosis` only.

**Architecture:** Treat Hermes-agent as the existing runtime and API-serving foundation. This phase adds an AIVideoTrans-side Copilot integration layer: `gateway` remains the security boundary and context-assembly layer, the admin frontend talks only to `gateway`, and every Copilot answer is assembled from bounded evidence, stored in persistent session history, and returned with structured citations instead of unrestricted agent behavior.

**Tech Stack:** Hermes-agent, FastAPI, SQLAlchemy, Alembic, PostgreSQL, httpx, Next.js 16, React 19, TypeScript, pytest

---

## Revision Note

This implementation plan assumes Hermes-agent already exists as the runtime and chat-serving foundation.

Accordingly, Phase 3 focuses on the AIVideoTrans-side Copilot integration layer:

- bounded Copilot persistence in `gateway`
- restricted Hermes API client behavior
- backend context assembly and evidence ranking
- admin-only Copilot surfaces in `frontend-next`
- strict answer boundaries, conservative confidence, and citation requirements

This plan does not attempt to build a general-purpose agent chat system inside the repo.

Primary execution path:

- backend calls Hermes API server

Fallback execution path:

- backend embeds Hermes through the Python library

## Hermes Base Capabilities vs AIVideoTrans Integration Responsibilities

### Provided by Hermes-agent Base

Treat these as existing or externally managed platform capabilities:

- runtime execution and model orchestration
- built-in OpenAI-compatible API server behavior
- Python library embedding through `AIAgent`
- profile isolation and prompt/runtime configuration
- future broader profile capabilities outside this repo

### Built in This Repo During Phase 3

This repository adds only the Copilot integration/control-plane layer:

- Copilot sessions and message persistence
- bounded mode validation
- evidence retrieval and context assembly
- restricted Hermes client wrapper
- admin Copilot APIs and page
- conservative answer contract with citations and follow-up questions

## Scope Lock

### In Scope

Phase 3 includes:

- `hermes_copilot_sessions`
- `hermes_copilot_messages`
- backend context assembly
- Hermes API client boundary
- admin Copilot page
- evidence-backed answers
- session history

First supported modes:

- `ops_qa`
- `job_diagnosis`

### Supported by Hermes Base

These are assumed to exist outside repo implementation:

- runtime lifecycle
- model execution
- chat completion primitive
- profile runtime isolation

### Out of Scope

Do not expand Phase 3 into:

- `business_qa`
- `platform_qa`
- autonomous actions
- browser-driven research from chat
- broad system-shell reasoning
- public or user-facing exposure
- unrestricted retrieval over the entire system

## Current Project Context and Phase Dependencies

This plan starts only after:

- Phase 1 Hermes Ops Control Plane exists
- Phase 1.5 and Phase 2 data surfaces exist or are at least partially available

That means the repo should already have:

- persisted Hermes reports and insights
- Hermes-facing ops bundle APIs
- admin Hermes area and navigation
- evidence structures suitable for backend retrieval

Key implementation judgment:

> Phase 3 is not about making Hermes more autonomous. It is about making stored operational knowledge safely queryable through a bounded admin Copilot surface.

## Phase 3 Architecture Goal

Phase 3 builds a **bounded Copilot integration layer** with four concrete goals:

1. keep `gateway` as the only security and context boundary
2. answer only from bounded evidence sets
3. persist session history for auditability
4. return structured citations and conservative confidence with every answer

## Copilot Execution Modes

### Primary Mode

- `gateway -> Hermes API server`

This is the preferred deployment path because it preserves a clean service boundary and lets the backend stay responsible for auth, context assembly, and persistence.

### Fallback Mode

- `gateway -> embedded Hermes Python library`

This is acceptable when API server deployment is not the preferred shape, but it must preserve the same context assembly, mode validation, and session persistence rules.

## Data and Security Boundaries

### Frontend Boundary

The frontend must never call Hermes directly.

The frontend may only:

- create Copilot sessions through backend APIs
- send bounded user messages through backend APIs
- read persisted sessions and messages

### Backend Boundary

`gateway` must:

- validate the requested mode
- assemble the context pack before every Hermes call
- limit evidence count and source breadth
- persist user and assistant messages
- reject unsupported or under-evidenced requests conservatively

### Hermes Boundary

Hermes must not receive:

- raw unrestricted DB access
- raw filesystem access
- broad `/api/admin/*` access
- permission to initiate new scans or crawling during chat

## Evidence and Answer Contract

### Allowed Evidence Sources

Copilot may retrieve only from controlled sources such as:

- stored Hermes reports
- stored Hermes insights
- bounded job bundle from `hermes_ops_api`
- bounded S2 bundle from `hermes_ops_api`

### Answer Shape

Every Copilot answer must return:

- `answer_markdown`
- `confidence`
- `citations`
- `follow_up_questions`

Each citation must support:

- `source_type`
- `source_ref`
- `label`
- `url`
- `excerpt`
- `captured_at`

### Conservative-Answer Rule

If the evidence set is weak, stale, or too narrow, the system should answer conservatively rather than speculate.

## Recommended Execution Waves

### Wave 1: Persistence and Client Boundary

- Copilot session/message tables
- restricted Hermes client
- config defaults and mode validation

### Wave 2: Context Assembly

- evidence ranking helpers
- bounded `ops_qa` context builder
- bounded `job_diagnosis` context builder

### Wave 3: Copilot API Surface

- session create/list/get
- message submission
- assistant response persistence
- route registration and coverage

### Wave 4: Admin Copilot UI

- session list
- chat thread
- evidence panel
- bounded mode selector

### Wave 5: Docs and Operational Guidance

- profile doc
- runbook
- example config

## File Structure

### Backend

- Modify: `gateway/hermes_models.py`
  Responsibility: add Copilot session and message ORM models
- Modify: `gateway/hermes_schemas.py`
  Responsibility: add Copilot request/response schemas
- Modify: `gateway/hermes_service.py`
  Responsibility: add context-assembly helpers and evidence ranking primitives
- Create: `gateway/hermes_copilot_client.py`
  Responsibility: restricted client for Hermes API server calls
- Create: `gateway/hermes_copilot_api.py`
  Responsibility: admin session/message endpoints and answer flow
- Modify: `gateway/config.py`
  Responsibility: add Hermes Copilot connection settings and safe defaults
- Modify: `gateway/main.py`
  Responsibility: register Copilot router
- Create: `gateway/alembic/versions/014_add_hermes_copilot_tables.py`
  Responsibility: additive session/message tables

### Frontend

- Modify: `frontend-next/src/types/hermes.ts`
  Responsibility: add Copilot types
- Modify: `frontend-next/src/lib/admin/hermes.ts`
  Responsibility: add Copilot fetch helpers
- Create: `frontend-next/src/app/(app)/admin/hermes/copilot/page.tsx`
  Responsibility: Copilot session list, chat panel, evidence panel
- Modify: `frontend-next/src/components/app-shell.tsx`
  Responsibility: expose Copilot nav entry once Phase 3 is enabled

### Tests

- Create: `tests/test_hermes_copilot_models.py`
- Create: `tests/test_hermes_copilot_client.py`
- Create: `tests/test_hermes_copilot_api.py`
- Modify: `tests/test_hermes_service.py`
- Modify: `tests/test_gateway_route_coverage.py`

### Deployment and Docs

- Create: `docs/hermes/HERMES_COPILOT_PROFILE.md`
- Create: `docs/hermes/HERMES_COPILOT_RUNBOOK.md`
- Create: `docs/hermes/examples/copilot_profile.example.yaml`

## Core Design Constraints

### Allowed Question Modes

Phase 3 only supports:

- `ops_qa`
- `job_diagnosis`

Recommended examples:

- "What were the most common failure causes in the last 7 days?"
- "Why did this job fail?"
- "Has this failure pattern increased recently?"

### Evidence Sources

Copilot may retrieve only from controlled sources such as:

- stored Hermes reports
- stored Hermes insights
- bounded job bundle from `hermes_ops_api`
- bounded S2 bundle from `hermes_ops_api`

It must not trigger new external crawling or broad system-wide scans during chat.

### Answer Shape

Every Copilot answer should return:

- `answer_markdown`
- `confidence`
- `citations`
- `follow_up_questions`

Each citation should support:

- `source_type`
- `source_ref`
- `label`
- `url`
- `excerpt`
- `captured_at`

## Task 1: Add Copilot Persistence Schema

**Files:**
- Modify: `gateway/hermes_models.py`
- Create: `gateway/alembic/versions/014_add_hermes_copilot_tables.py`
- Test: `tests/test_hermes_copilot_models.py`

- [ ] **Step 1: Write failing model tests**

Cover:

- `hermes_copilot_sessions`
- `hermes_copilot_messages`
- useful indexes on session and created time

Suggested tests:

```python
def test_hermes_copilot_session_model_exists():
    from hermes_models import HermesCopilotSession
    assert HermesCopilotSession.__tablename__ == "hermes_copilot_sessions"

def test_hermes_copilot_message_model_exists():
    from hermes_models import HermesCopilotMessage
    assert HermesCopilotMessage.__tablename__ == "hermes_copilot_messages"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hermes_copilot_models.py -q`

Expected: FAIL

- [ ] **Step 3: Add minimal session and message tables**

Suggested intent:

```python
class HermesCopilotSession(Base):
    __tablename__ = "hermes_copilot_sessions"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    mode = mapped_column(String(32), nullable=False)
    conversation_title = mapped_column(String(255), nullable=True)
    context_scope_json = mapped_column(JSONB, nullable=True)
```

```python
class HermesCopilotMessage(Base):
    __tablename__ = "hermes_copilot_messages"
    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = mapped_column(UUID(as_uuid=True), ForeignKey("hermes_copilot_sessions.id"), nullable=False)
    role = mapped_column(String(16), nullable=False)
    content_markdown = mapped_column(Text, nullable=False)
    content_json = mapped_column(JSONB, nullable=True)
    source_refs_json = mapped_column(JSONB, nullable=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hermes_copilot_models.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_models.py gateway/alembic/versions/014_add_hermes_copilot_tables.py tests/test_hermes_copilot_models.py
git commit -m "feat: add hermes copilot persistence schema"
```

## Task 2: Add Gateway Config and Restricted Hermes Client

**Files:**
- Modify: `gateway/config.py`
- Create: `gateway/hermes_copilot_client.py`
- Test: `tests/test_hermes_copilot_client.py`

- [ ] **Step 1: Write failing client tests**

Cover:

- config defaults
- request payload shape
- rejection when unsupported mode is requested
- timeout and network error normalization

Suggested tests:

```python
def test_copilot_client_rejects_unsupported_mode():
    from hermes_copilot_client import validate_mode
    with pytest.raises(ValueError):
        validate_mode("business_qa")

def test_build_chat_payload_contains_bounded_messages():
    from hermes_copilot_client import build_chat_payload
    payload = build_chat_payload(system_prompt="x", messages=[{"role": "user", "content": "why failed"}])
    assert "messages" in payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hermes_copilot_client.py -q`

Expected: FAIL

- [ ] **Step 3: Add config settings and client wrapper**

Add settings in `gateway/config.py` such as:

```python
hermes_copilot_base_url: str = "http://127.0.0.1:8642/v1"
hermes_copilot_timeout_seconds: int = 45
hermes_copilot_enabled: bool = False
```

Implement `hermes_copilot_client.py` with:

- mode validation
- OpenAI-compatible payload builder
- `ask_hermes_copilot()` wrapper
- normalized error shape

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hermes_copilot_client.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/config.py gateway/hermes_copilot_client.py tests/test_hermes_copilot_client.py
git commit -m "feat: add hermes copilot client boundary"
```

## Task 3: Add Context Assembly and Evidence Ranking

**Files:**
- Modify: `gateway/hermes_service.py`
- Modify: `tests/test_hermes_service.py`

- [ ] **Step 1: Write failing tests for context assembly**

Cover:

- assembling a bounded `ops_qa` context from reports and insights
- assembling a bounded `job_diagnosis` context from a specific job bundle
- ranking evidence
- conservative behavior when evidence is thin

Suggested tests:

```python
def test_build_ops_qa_context_prefers_recent_high_priority_reports():
    from hermes_service import build_ops_qa_context
    context = build_ops_qa_context(reports=[...], insights=[...], question="What failure causes were most common recently?")
    assert len(context["evidence"]) > 0

def test_build_job_diagnosis_context_includes_job_bundle():
    from hermes_service import build_job_diagnosis_context
    context = build_job_diagnosis_context(job_bundle={"job": {"job_id": "job_1"}}, question="Why did this job fail?")
    assert context["mode"] == "job_diagnosis"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hermes_service.py -q`

Expected: FAIL

- [ ] **Step 3: Implement context builders**

Add helpers such as:

```python
def build_ops_qa_context(*, question: str, reports: list[dict], insights: list[dict]) -> dict: ...
def build_job_diagnosis_context(*, question: str, job_bundle: dict, s2_bundle: dict | None = None) -> dict: ...
def rank_evidence_items(items: list[dict]) -> list[dict]: ...
```

Rules:

- evidence must be bounded
- prioritize recent and high-priority reports
- do not exceed a fixed evidence count per answer
- if evidence is weak, mark lower confidence and avoid overclaiming

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hermes_service.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_service.py tests/test_hermes_service.py
git commit -m "feat: add hermes copilot context assembly"
```

## Task 4: Add Copilot API Endpoints

**Files:**
- Create: `gateway/hermes_copilot_api.py`
- Modify: `gateway/main.py`
- Create: `tests/test_hermes_copilot_api.py`
- Modify: `tests/test_gateway_route_coverage.py`

- [ ] **Step 1: Write failing API tests**

Cover:

- create session
- list sessions
- get session messages
- post a user message and receive assistant response
- reject unsupported mode
- reject non-admin or unauthorized access

Suggested tests:

```python
def test_create_copilot_session():
    ...
    assert payload["mode"] == "ops_qa"

def test_post_message_returns_answer_with_citations():
    ...
    assert "citations" in payload["assistant_message"]["content_json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hermes_copilot_api.py tests/test_gateway_route_coverage.py -q`

Expected: FAIL

- [ ] **Step 3: Implement the backend API flow**

Required endpoints:

- `POST /api/admin/hermes/copilot/sessions`
- `GET /api/admin/hermes/copilot/sessions`
- `GET /api/admin/hermes/copilot/sessions/{id}`
- `POST /api/admin/hermes/copilot/sessions/{id}/messages`

Message flow:

1. validate admin
2. validate mode
3. assemble context from stored reports/insights and bounded bundles
4. call `hermes_copilot_client`
5. persist user and assistant messages
6. return assistant answer and citations

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hermes_copilot_api.py tests/test_gateway_route_coverage.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/hermes_copilot_api.py gateway/main.py tests/test_hermes_copilot_api.py tests/test_gateway_route_coverage.py
git commit -m "feat: add hermes copilot api"
```

## Task 5: Add Admin Copilot Page

**Files:**
- Modify: `frontend-next/src/types/hermes.ts`
- Modify: `frontend-next/src/lib/admin/hermes.ts`
- Create: `frontend-next/src/app/(app)/admin/hermes/copilot/page.tsx`
- Modify: `frontend-next/src/components/app-shell.tsx`

- [ ] **Step 1: Define the TypeScript types**

Include:

- session summary
- message shape
- citation shape
- supported mode union

- [ ] **Step 2: Implement typed fetch helpers**

Add:

- `createHermesCopilotSession()`
- `listHermesCopilotSessions()`
- `getHermesCopilotSession()`
- `sendHermesCopilotMessage()`

- [ ] **Step 3: Implement the page**

First-page behavior:

- left column: session list
- main column: chat thread
- right column: evidence panel

UI constraints:

- mode selector only offers `ops_qa` and `job_diagnosis`
- answers show citations clearly
- if no answer is available yet, show loading state
- do not expose arbitrary tool controls

- [ ] **Step 4: Run frontend lint**

Run: `npm run lint`
Workdir: `D:\\Claude\\AIVideoTrans_Codex_web_mvp\\frontend-next`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend-next/src/types/hermes.ts frontend-next/src/lib/admin/hermes.ts frontend-next/src/app/(app)/admin/hermes/copilot/page.tsx frontend-next/src/components/app-shell.tsx
git commit -m "feat: add hermes copilot admin page"
```

## Task 6: Add Copilot Docs and Deployment Guidance

**Files:**
- Create: `docs/hermes/HERMES_COPILOT_PROFILE.md`
- Create: `docs/hermes/HERMES_COPILOT_RUNBOOK.md`
- Create: `docs/hermes/examples/copilot_profile.example.yaml`

- [ ] **Step 1: Write the Copilot profile doc**

Document:

- supported modes
- safety boundary
- evidence requirements
- confidence expectations
- non-goals for Phase 3

- [ ] **Step 2: Write the runbook**

Document:

- required env vars
- Hermes API base URL expectations, with local default `http://127.0.0.1:8642/v1`
- timeout guidance
- admin-only exposure
- fallback behavior when Hermes is unavailable
- optional Python library fallback guidance
- incident handling for bad or low-confidence answers

- [ ] **Step 3: Add example profile config**

Example:

```yaml
profile: copilot
enabled: true
allowed_modes:
  - ops_qa
  - job_diagnosis
timeout_seconds: 45
```

- [ ] **Step 4: Commit**

```bash
git add docs/hermes/HERMES_COPILOT_PROFILE.md docs/hermes/HERMES_COPILOT_RUNBOOK.md docs/hermes/examples/copilot_profile.example.yaml
git commit -m "docs: add hermes copilot profile guidance"
```

## Success Criteria

### Engineering Completion

- Copilot tables migrate cleanly
- restricted client and Copilot APIs resolve
- admin Copilot page renders
- targeted backend tests and frontend lint pass

### Integration Boundary

- frontend never calls Hermes directly
- `gateway` assembles all context and persists all session state
- unsupported modes are rejected early
- chat cannot trigger broad scans or unrestricted retrieval

### Product Quality

- `ops_qa` and `job_diagnosis` both return usable evidence-backed answers
- every answer includes citations and confidence
- weak evidence leads to conservative wording instead of speculation
- Hermes API outages produce bounded user-facing failures

### Platform Readiness

- answer contract is reusable for future `business_qa` or `platform_qa`
- Phase 3 does not widen permissions beyond the approved Copilot boundary

## Risk and Rollback Strategy

### Risk 1: Context Assembly Is Too Broad or Too Weak

- trigger: answers include too much irrelevant evidence or too little useful evidence
- impact: low answer quality or slow responses
- mitigation: bounded evidence counts, explicit ranking, conservative-answer rule
- rollback: narrow supported modes further or reduce evidence breadth until quality stabilizes

### Risk 2: Copilot Boundary Erodes

- trigger: new modes or shortcuts bypass gateway context assembly
- impact: security and reliability regression
- mitigation: mode validation in backend and restricted client wrapper
- rollback: reject unsupported mode paths and disable unfinished UI controls

### Risk 3: Hermes API Instability Hurts Admin UX

- trigger: upstream Hermes runtime times out or errors frequently
- impact: admin chat becomes slow or unreliable
- mitigation: timeouts, normalized error shape, bounded loading states
- rollback: disable Copilot feature flag while keeping stored sessions intact

### Risk 4: Citation Quality Is Too Weak

- trigger: answers return citations that are missing, stale, or hard to inspect
- impact: Copilot feels untrustworthy
- mitigation: structured citation schema, evidence panel, source traceability
- rollback: refuse answer generation when minimum evidence requirements are not met

## Final Verification

- [ ] Run backend test slice:

```bash
pytest tests/test_hermes_copilot_models.py tests/test_hermes_copilot_client.py tests/test_hermes_copilot_api.py tests/test_hermes_service.py tests/test_gateway_route_coverage.py -q
```

- [ ] Run frontend lint:

```bash
cd frontend-next && npm run lint
```

- [ ] Manual verification checklist:

- admin can create an `ops_qa` session
- admin can create a `job_diagnosis` session
- a message returns an answer with citations
- evidence panel lists the same citations returned by the backend
- unsupported modes are rejected
- if Hermes API is unavailable, UI shows a bounded error instead of hanging

## Notes for Implementers

- Keep Phase 3 narrow. A reliable evidence-backed Copilot is better than a broad but fuzzy assistant.
- Reuse stored reports and insights wherever possible; do not make chat calls trigger expensive new scans by default.
- If a user asks a question outside the supported modes, reject it explicitly instead of silently widening permissions.
- Keep the answer contract structured so future `business_qa` or `platform_qa` can reuse it without rewriting the page.
