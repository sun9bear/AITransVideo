# MiMo Free Tier — LAUNCH GATE (consent / legal + deployment acceptance)

**Status:** Phase 2a **engineering (Tasks 0–8) complete** behind `AVT_ENABLE_FREE_TIER`
(default off). This doc covers what must land **before the flag may be flipped on for
the public** — the consent/legal gate (`gateway/config.py:107` ⚠️) and the deployment
acceptance checklist. None of this authorizes a public launch on its own; it is the
checklist that does.

Companion: [`2026-05-29-mimo-free-tier-phase2-plan.md`](2026-05-29-mimo-free-tier-phase2-plan.md)
(engineering, done) + [`2026-05-29-mimo-free-tier-design.md`](2026-05-29-mimo-free-tier-design.md) §5.3.

---

## §1 Why a consent gate (legal framing)

The free tier's value is **MiMo voiceclone**, which **reproduces the original speaker's
voice** from the *source* video into the dubbed output. The source speaker is almost
always a **third party** (a YouTuber, a podcast guest, …), not the uploading user.

《民法典》**第 1023 条**: a natural person's **voice** is protected by reference to the
portrait-rights rules — reproducing/using someone's voice generally requires their
consent. Reproducing the source speaker's vocal identity without authorization exposes
**both** the user and the platform.

→ Before the free voiceclone runs for the public, the user must **explicitly attest they
hold the rights** to the source content and to reproducing its speakers' voices. This is a
**rights-attestation / liability-shift**, semantically *different* from the existing
`auto_voice_clone` consent (which means "clone a voice into *my* library").

**Scope note (kill-switch interaction, Task 6a):** when admin
`free_tier_voiceclone_enabled=false`, free degrades to a CosyVoice **preset** (no
source-voice reproduction) → no §1023 exposure. So the consent is strictly required only
on the **voiceclone path**. Since voiceclone is the default, the launch recommendation is
to **require consent for all free job creation** (simplest, safe); relaxing it on the
preset path is an optional optimization (see §4).

---

## §2 Consent gate — engineering design (reuses the established pattern)

> ✅ **Backend implemented** (this batch): `gateway/free_consent.py`
> `validate_free_consent` (HARD — returns a payload only when
> `voice_rights_confirmed is True`, strict bool) + `intercept_create_job` free
> branch gate (403 `consent_required` **before** the daily reserve / forward) +
> server `server_confirmed_at` stamp + anti-forge re-inject of the validated
> payload (strips any client-embedded value). Tests: `test_free_consent` (7
> validator) + handler (free + no consent → 403, never forwards). **Frontend
> attestation is implemented in §3; pending blocker is the §4.1 legal text.**

Mirror the existing validators but with **HARD-fail** semantics (like
`gateway/smart_consent.py`, NOT the soft-skip `gateway/express_consent.py`): a free job
without confirmed voice-rights consent must be **rejected**, not silently downgraded.

- **New `gateway/free_consent.py` — `validate_free_consent(raw) -> (parsed | None, reason)`**
  - Schema (strict types, mirror `express_consent` strict-bool):
    - `voice_rights_confirmed: bool` — **required True**. User attests they hold the rights
      to the source content and to reproducing its speakers' voices.
    - `client_confirmed_at: str | None` — untrusted UI timestamp (audit assist only).
  - Reasons: `free_consent_missing_or_invalid_type` / `voice_rights_not_confirmed` /
    `voice_rights_confirmed_not_bool` / `client_confirmed_at_not_string`.
  - Caller adds **`server_confirmed_at`** (authoritative UTC) when confirmed True.
- **Gate in `intercept_create_job` (free branch), BEFORE the upstream forward** — same
  placement as the Task 4 daily reserve:
  - `service_mode=="free"` + consent missing / `voice_rights_confirmed != True`
    → `403 consent_required` (don't forward, don't reserve daily quota, don't spend).
  - On success → stamp `server_confirmed_at` and persist (below).
  - Ordering vs Task 4 reserve: **consent check first** (cheapest, no DB), then daily
    reserve, then forward.
- **Persistence (liability evidence):** record `{voice_rights_confirmed, client_confirmed_at,
  server_confirmed_at}` as part of the job's snapshot/audit so a confirmed attestation is
  provable per job. Reuse the express audit-JSONL convention; do **not** invent a new store.
- **Tests** (mirror `test_phase2_free_tier_guards`): pure validator (confirmed / missing /
  false / wrong-type → reason); handler — free + no consent → 403 `consent_required`, never
  forwards; free + consent → proceeds + `server_confirmed_at` stamped; paid modes unaffected.

> The validator + gate are **text-agnostic** — they only check the boolean. The legal
> *wording* lives in the frontend (next), so the gate can be built now with the copy as a
> placeholder pending legal sign-off.

---

## §3 Frontend (blocking attestation at free job creation)

> ✅ **Implemented** (this batch): `TranslationForm.tsx` renders a blocking
> 声音授权声明 checkbox when `serviceMode === "free"` (gated by
> `NEXT_PUBLIC_ENABLE_FREE_TIER`); `validationError` keeps submit disabled until it
> is checked; the checkbox resets on mode switch; `submitTranslationJob` sends
> `free_consent` (`voice_rights_confirmed` + `client_confirmed_at`), forced false
> for non-free. Lint: 0 errors. **The checkbox copy is a PLACEHOLDER pending §4.1
> legal sign-off.**

Mirror `CosyVoiceConsentModal.tsx` / the `express_consent` collection in
`TranslationForm.tsx`:

- A **blocking** checkbox/modal on the free job-creation surface: the submit button stays
  disabled until the user checks "我确认对该视频内容及其中说话人声音的使用拥有合法授权…"
  (final wording = legal, see §4).
- Send `free_consent: { voice_rights_confirmed: true, client_confirmed_at: <ISO> }` in the
  create body. Gate `NEXT_PUBLIC_ENABLE_FREE_TIER` (already the UI flag).
- No "remember my choice" auto-send — consent must be an explicit per-job action (matches
  the paid-API "user must explicitly trigger" rule).

---

## §4 Decision points — **owner: user / legal** (blockers for flipping the flag)

1. **Consent text (LEGAL — must be drafted/approved before public launch).** Placeholder
   draft for review (NOT legal advice): *"我确认：我已获得该视频内容及其中所有说话人声音的合法授权，
   或该使用属于法律允许的范围；因使用本服务声音克隆功能产生的肖像权/声音权纠纷由我自行承担。"*
2. **HARD vs SOFT** — recommend **HARD** (reject without consent). Voice-rights is a legal
   liability, unlike the optional express auto-clone. Confirm.
3. **Require on preset-fallback too?** — recommend **yes for launch** (require for all free;
   simplest). Optional later: skip when `free_tier_voiceclone_enabled=false`.
4. **Consent evidence retention** — how long to keep `server_confirmed_at` records, and
   whether to surface them in admin (dispute handling).

---

## §5 Deployment / acceptance checklist (flag stays OFF until all ✅)

**Infra / config:**
- [ ] **Watermark font (CodeX flag, Task 8):** the app `Dockerfile` installs `ffmpeg` only —
      **no font package**, no default `AVT_WATERMARK_FONTFILE`. ffmpeg `drawtext` needs a
      font; without one, **free publish raises `PublishError`**. Either install a fonts pkg
      (e.g. `fonts-dejavu`) **or** mount a `.ttf` and set `AVT_WATERMARK_FONTFILE`. For a CJK
      watermark text, a CJK font is required. ASCII default lowers risk but the dependency is real.
- [ ] `AVT_ENABLE_FREE_TIER=true` (backend) **and** `NEXT_PUBLIC_ENABLE_FREE_TIER=1` (frontend)
      — both gates; flip together.
- [ ] Migration **034 `free_service_daily_usage`** applied (daily quota ledger, Task 4).
- [ ] `pricing_runtime.json` carries `debit_rates["free.standard"]=0` (Task 3 truth source;
      the frozen `DEBIT_RATES` baseline already overlays, but verify runtime).
- [ ] Consent gate (§2) deployed + frontend attestation (§3) live.

**Manual end-to-end (US host, flag on, internal only), verify:**
- [ ] Unknown service_mode never silently becomes express (Task 1).
- [ ] free job: credits **= 0** and still enters metering (Task 3).
- [ ] daily cap blocks the **2nd** free job same SH-day (Task 4); idempotent retry not 403'd.
- [ ] download/stream/eager-push only expose the **watermarked product + poster** — no
      `/stream/audio`, no `editor.*` via R2 prepush (Task 5).
- [ ] MiMo voiceclone per-segment failure → **visible** base-preset fallback, **no** paid
      clone (Task 6).
- [ ] free + duration > 10min → rejected **before** ASR/LLM/TTS; **untrusted duration →
      rejected** (fail-closed, Task 7).
- [ ] published video carries the **watermark**; voiceclone output stays time-aligned.
- [ ] free job **without consent → 403** `consent_required`; with consent → proceeds (§2).
- [ ] flag **off** → public free entry fully invisible (both gates).

---

## §6 Non-blocking follow-ups (post-launch / Phase 2b)

- `tests/test_tts_generator.py` stale-fake fix (separate uncommitted change; spun off, not
  part of any Phase 2a commit).
- Admin watermark config UI (text / position / font) — Phase 2b.
- CJK watermark text + bundled CJK font.
- MiMo voiceclone chunking (deferred — Express DSP alignment absorbs run-to-run variance).
- Relax consent on the preset-fallback path (§4.3) if desired.
