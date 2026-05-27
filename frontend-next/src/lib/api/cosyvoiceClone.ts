// frontend-next/src/lib/api/cosyvoiceClone.ts
//
// Phase 4.2 D.2 — CosyVoice clone API client.
//
// Three responsibilities:
//   1. `getCosyvoiceCloneGate()`   — calls GET /api/voice/cosyvoice/clone-gate
//      (D.1 endpoint, public read for display-layer authorization visibility).
//   2. `submitCosyvoiceClone()`    — multipart POST /api/voice/cosyvoice/clone
//      with sample-upload OR source-segments mode (mutex).
//   3. Exports the **frozen** `CONSENT_MODAL_VERSION` constant that MUST be
//      identical to gateway `CONSENT_MODAL_VERSION` in
//      `gateway/cosyvoice_clone/api.py` —— version drift between frontend and
//      backend will cause `consent_outdated` 400 on every clone attempt.
//
// **Paid API constraint (CLAUDE.md)**: CosyVoice clone is a per-call paid
// DashScope API. This client only sends the POST when the caller has already
// shown consent and explicitly clicked submit. There is no auto-retry on
// failure — that decision belongs to the user, not to the client.

/**
 * Locked frontend ↔ backend consent contract version.
 *
 * Update **only** when paired with a backend
 * `CONSENT_MODAL_VERSION` change in `gateway/cosyvoice_clone/api.py` and
 * a new legal text doc under `docs/legal/`. A static guard test verifies
 * both sides are in sync.
 */
export const CONSENT_MODAL_VERSION = "2026-05-25-v1" as const

/**
 * The two DashScope CosyVoice models we support for clone target. Backend
 * `ALLOWED_TARGET_MODELS` mirrors this set; mismatch → 400 invalid_target_model.
 */
export const COSYVOICE_TARGET_MODELS = [
  "cosyvoice-v3.5-flash",
  "cosyvoice-v3.5-plus",
] as const
export type CosyvoiceTargetModel = (typeof COSYVOICE_TARGET_MODELS)[number]

/**
 * Default target model per plan §Open Question #4 resolution
 * (Codex 2026-05-25): flash is default, plus opt-in via UI toggle.
 */
export const DEFAULT_COSYVOICE_TARGET_MODEL: CosyvoiceTargetModel =
  "cosyvoice-v3.5-flash"

// ---------------------------------------------------------------------------
// GET /api/voice/cosyvoice/clone-gate (D.1)
// ---------------------------------------------------------------------------

/**
 * Backend response shape from `GET /api/voice/cosyvoice/clone-gate`.
 * Mirrors gateway `cosyvoice_clone_gate` endpoint JSON.
 *
 * Two separate dimensions:
 *
 *   1. `can_access_clone` — **policy authorization visibility** (admin /
 *      allowlist / GA toggle). D.1 endpoint shape.
 *   2. `runtime_ready` — **runtime availability** (worker_enabled +
 *      uploader is production-ready backend + config complete). Added
 *      in E.1 PR #15 P2 fix (Codex 2026-05-27).
 *
 * Frontend MUST AND both before showing the clone button:
 *
 *   showCloneButton = provider.supportsClone &&
 *                     gate.can_access_clone &&
 *                     gate.runtime_ready
 *
 * Or use the convenience field `can_show_clone_button` which the backend
 * already computes as `can_access_clone && runtime_ready`.
 */
export interface CosyvoiceCloneGateResponse {
  /** Whether the current authenticated user is authorized to see the
   *  clone entry. **Policy layer only** — does NOT reflect runtime
   *  availability. AND with `runtime_ready` for the real "show button"
   *  decision. */
  can_access_clone: boolean
  /** Which rule granted (or denied) access. Useful for ops / debugging. */
  authorization_reason:
    | "admin"
    | "allowlist"
    | "general_availability"
    | "none"
  /** Mirrors admin setting; lets the UI show "全用户开放" vs "灰度 only" hints. */
  general_availability_enabled: boolean
  user_is_admin: boolean
  user_in_allowlist: boolean

  // ---- E.1 PR #15 P2 fix: runtime availability ----

  /** Whether the backend is configured to actually serve a clone request
   *  (worker enabled + uploader is production-ready + config complete).
   *  Mirrors POST `/clone` Layers 2-3 fail-closed ladder. */
  runtime_ready: boolean
  /** Specific reason for `runtime_ready=false`. Frontend can use this to
   *  show ops messages ("worker disabled", "uploader not configured",
   *  etc.). `null` when runtime_ready=true.
   *
   *  Codes map 1:1 to POST `/clone` 503 failure layers — adding a new
   *  code here requires extending backend `CloneRuntimeReadiness` Literal
   *  + `_resolve_runtime_ready` ladder + matching layer in POST `/clone`. */
  runtime_unavailable_code:
    | "clone_feature_disabled"
    | "sample_uploader_not_configured"
    | "sample_uploader_not_implemented"
    | "sample_uploader_config_missing"
    | "worker_disabled"
    | null
  /** Convenience: `can_access_clone && runtime_ready`. Frontend can use
   *  this directly; explicit AND is also encouraged for transparency.
   *  Backend-computed so the two layers stay in lockstep. */
  can_show_clone_button: boolean
}

/**
 * Calls `GET /api/voice/cosyvoice/clone-gate`. Throws on network / 401 / 5xx.
 *
 * 401 (unauthenticated) is treated as an error — callers must ensure the user
 * is logged in before calling this. Returning a "fake denied" response for
 * 401 would let unauthenticated callers silently see a 'no clone entry' UI
 * which is confusing; instead surface the error so the page can route to
 * login.
 */
export async function getCosyvoiceCloneGate(): Promise<CosyvoiceCloneGateResponse> {
  const resp = await fetch("/api/voice/cosyvoice/clone-gate", {
    method: "GET",
    credentials: "include",
  })
  if (!resp.ok) {
    let detail: unknown = undefined
    try {
      detail = await resp.json()
    } catch {
      /* ignore — keep detail undefined */
    }
    throw new CosyvoiceCloneApiError(
      `clone-gate failed: HTTP ${resp.status}`,
      resp.status,
      detail,
    )
  }
  return (await resp.json()) as CosyvoiceCloneGateResponse
}

// ---------------------------------------------------------------------------
// POST /api/voice/cosyvoice/clone
// ---------------------------------------------------------------------------

/**
 * Sample dispatch mode. Backend `POST /clone` requires **exactly one** of
 * these two sources (mutex enforced server-side, Layer 6.5).
 *
 *   - `"file"`: user uploads a fresh audio file via the modal upload widget.
 *   - `"segments"`: sample is concatenated from transcript segments of an
 *     existing job. Requires `sourceJobId` + non-empty `sourceSegmentIds`.
 */
export type CosyvoiceSampleMode = "file" | "segments"

/**
 * Consent payload (matches the three Form fields the backend requires).
 *
 * The CosyVoiceConsentModal builds this exact shape on submit and the
 * CloneModal forwards it untouched. Backend rejects any deviation with
 * `consent_required` (false / wrong field) or `consent_outdated` (stale
 * modal_version).
 */
export interface CosyvoiceConsentPayload {
  /** Backend accepts ONLY the literal string `"true"` (StrictBool semantics). */
  voice_clone_confirmed: "true"
  /** Must equal `CONSENT_MODAL_VERSION`. */
  modal_version: typeof CONSENT_MODAL_VERSION
  /** ISO 8601 UTC timestamp captured when user clicked confirm. */
  confirmed_at: string
}

/**
 * Caller payload for `submitCosyvoiceClone`. The client converts this into
 * the multipart/form-data body the backend expects.
 */
export interface CosyvoiceCloneRequest {
  targetModel: CosyvoiceTargetModel
  speakerId: string
  speakerName: string
  consent: CosyvoiceConsentPayload
  /** Sample dispatch mode — file OR segments, never both. */
  sampleMode: CosyvoiceSampleMode
  /** Required when `sampleMode === "file"`. Browser File object. */
  sampleFile?: File
  /** Required when `sampleMode === "segments"`. Job id to pull segments from. */
  sourceJobId?: string
  /**
   * Required when `sampleMode === "segments"`. Array of integer segment ids
   * from the job's transcript.
   *
   * **Type MUST be `number[]`** — Phase 4.2 A.2b backend
   * `_parse_source_segments` enforces strict `type(x) is int` (rejects
   * `bool` / `float` / `"1"` string / `null`). Source segments is the
   * primary input that drives transcript lookup + speaker ownership checks;
   * type drift here lets `"1"` strings silently bypass ownership boundaries.
   * Backend also rejects empty array. Static guard:
   * `tests/test_phase42_d2_cosyvoice_frontend_components.py::
   * test_d2_source_segment_ids_typed_as_number_array`.
   */
  sourceSegmentIds?: number[]
}

/**
 * Successful clone response (subset; backend may return more fields). Type
 * narrowing on the actually-used voice metadata fields keeps the API
 * surface tight without freezing the entire backend shape.
 */
export interface CosyvoiceCloneSuccess {
  voice_id: string
  target_model: CosyvoiceTargetModel
  /** Backend `requires_worker=true` always for CosyVoice clone. */
  requires_worker?: boolean
  /** Provider request id (DashScope) for billing reconciliation. */
  provider_request_id?: string
  /** Worker request id (Wuhan mainland worker) for cross-border audit. */
  worker_request_id?: string
  /** Free-form additional metadata; opaque to the UI client. */
  [extra: string]: unknown
}

/**
 * Typed error from the clone endpoint. Carries HTTP status + parsed body
 * detail (when present) so the UI can show backend-provided error codes
 * (`consent_required` / `consent_outdated` / `forbidden_not_in_allowlist`
 * / `clone_feature_disabled` / `invalid_target_model` / `quota_exceeded`
 * / `sample_invalid` / `clone_failed`, etc.).
 */
export class CosyvoiceCloneApiError extends Error {
  readonly status: number
  readonly detail: unknown
  /**
   * Backend `detail.code` if present (e.g. `"consent_outdated"`).
   * Convenience accessor — UI dispatches by code.
   */
  readonly code: string | null

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = "CosyvoiceCloneApiError"
    this.status = status
    this.detail = detail
    // Extract `detail.code` from FastAPI HTTPException body shape:
    //   { detail: { code: "consent_outdated", message: "..." } }
    let code: string | null = null
    if (detail && typeof detail === "object" && "detail" in detail) {
      const inner = (detail as { detail?: unknown }).detail
      if (inner && typeof inner === "object" && "code" in inner) {
        const c = (inner as { code?: unknown }).code
        if (typeof c === "string") code = c
      }
    }
    this.code = code
  }
}

/**
 * Submits the clone request to the backend.
 *
 * **Paid API trigger point.** This function MUST only be called from a code
 * path that has:
 *   1. Verified `can_access_clone === true` via `getCosyvoiceCloneGate()`
 *   2. Verified the runtime gate (`provider.supportsClone`) in E-phase wiring
 *   3. Received explicit consent (all three checkboxes ticked) from the
 *      `CosyVoiceConsentModal` —— never auto-fill consent in code paths.
 *
 * The function does NOT retry on failure — by design. Network / quota /
 * sample-rejected errors must surface to the user so they can decide to
 * resubmit. Auto-retry on a paid API is a CLAUDE.md hard violation.
 */
export async function submitCosyvoiceClone(
  request: CosyvoiceCloneRequest,
): Promise<CosyvoiceCloneSuccess> {
  // Client-side mutex enforcement mirroring backend Layer 6.5. We do NOT
  // rely on the backend alone here — preventing a wasted multipart upload
  // (potentially many MB) when the request shape is obviously wrong
  // protects the user's bandwidth and gives a clearer error message.
  if (request.sampleMode === "file") {
    if (!request.sampleFile) {
      throw new CosyvoiceCloneApiError(
        "sampleMode='file' 但未提供 sampleFile",
        400,
        { detail: { code: "client_missing_sample_file" } },
      )
    }
    if (request.sourceSegmentIds && request.sourceSegmentIds.length > 0) {
      throw new CosyvoiceCloneApiError(
        "sampleMode='file' 不允许同时传 sourceSegmentIds",
        400,
        { detail: { code: "client_sample_source_mutex" } },
      )
    }
  } else if (request.sampleMode === "segments") {
    if (!request.sourceJobId) {
      throw new CosyvoiceCloneApiError(
        "sampleMode='segments' 需要 sourceJobId",
        400,
        { detail: { code: "client_missing_source_job_id" } },
      )
    }
    if (!request.sourceSegmentIds || request.sourceSegmentIds.length === 0) {
      throw new CosyvoiceCloneApiError(
        "sampleMode='segments' 需要非空 sourceSegmentIds",
        400,
        { detail: { code: "client_missing_source_segments" } },
      )
    }
    if (request.sampleFile) {
      throw new CosyvoiceCloneApiError(
        "sampleMode='segments' 不允许同时传 sampleFile",
        400,
        { detail: { code: "client_sample_source_mutex" } },
      )
    }
  } else {
    // Exhaustiveness check — narrows `request.sampleMode` to `never` if all
    // union variants are handled above. A new variant will fail TypeScript.
    const _exhaustive: never = request.sampleMode
    throw new CosyvoiceCloneApiError(
      `unknown sampleMode: ${String(_exhaustive)}`,
      400,
      { detail: { code: "client_unknown_sample_mode" } },
    )
  }

  // Strict consent payload check — backend rejects anything other than the
  // literal "true" string + matching modal_version. Catch it here before
  // network round-trip so the user gets an immediate, clear message.
  if (request.consent.voice_clone_confirmed !== "true") {
    throw new CosyvoiceCloneApiError(
      "consent.voice_clone_confirmed 必须是字符串 'true'",
      400,
      { detail: { code: "client_consent_required" } },
    )
  }
  if (request.consent.modal_version !== CONSENT_MODAL_VERSION) {
    throw new CosyvoiceCloneApiError(
      `consent.modal_version 必须是 '${CONSENT_MODAL_VERSION}'，` +
        `收到 '${request.consent.modal_version}'`,
      400,
      { detail: { code: "client_consent_outdated" } },
    )
  }

  const form = new FormData()
  form.append("target_model", request.targetModel)
  form.append("speaker_id", request.speakerId)
  form.append("speaker_name", request.speakerName)
  // Backend expects literal "true" string, not the JS boolean.
  form.append(
    "consent_voice_clone_confirmed",
    request.consent.voice_clone_confirmed,
  )
  form.append("consent_modal_version", request.consent.modal_version)
  form.append("consent_confirmed_at", request.consent.confirmed_at)

  if (request.sampleMode === "file" && request.sampleFile) {
    form.append("sample", request.sampleFile)
  } else if (request.sampleMode === "segments") {
    form.append("source_job_id", request.sourceJobId as string)
    // Stringified JSON array of int — backend `_parse_source_segments` uses
    // `type(x) is int` strict check (rejects bool / float / string). The
    // type system catches misuse at compile time, but the runtime conversion
    // here keeps the contract explicit for reviewers.
    form.append(
      "source_segments",
      JSON.stringify(request.sourceSegmentIds as number[]),
    )
  }

  const resp = await fetch("/api/voice/cosyvoice/clone", {
    method: "POST",
    credentials: "include",
    body: form,
    // NB: do NOT set Content-Type header — browser sets multipart boundary.
  })

  let body: unknown = undefined
  try {
    body = await resp.json()
  } catch {
    /* ignore */
  }

  if (!resp.ok) {
    throw new CosyvoiceCloneApiError(
      `clone failed: HTTP ${resp.status}`,
      resp.status,
      body,
    )
  }

  // Defensive sanity check on the success response — if the backend skipped
  // wrapping the `target_model` echo (D.1 mismatch invariant) we don't
  // silently accept it.
  if (
    !body ||
    typeof body !== "object" ||
    !("voice_id" in body) ||
    !("target_model" in body)
  ) {
    throw new CosyvoiceCloneApiError(
      "clone response missing voice_id / target_model",
      resp.status,
      body,
    )
  }

  return body as CosyvoiceCloneSuccess
}
