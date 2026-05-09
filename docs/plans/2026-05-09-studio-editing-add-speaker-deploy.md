# Studio Editing Add-Speaker — Deploy Checklist

Plan ref: [`2026-05-09-studio-editing-add-speaker.md`](2026-05-09-studio-editing-add-speaker.md)
Task 10 §3+§4 — produced 2026-05-09 by Task 10 Step 1+2.

This document is **only** the deployment checklist. Code is already
committed locally (10 commits ahead of `origin/main`). Step 1 (guard
tests) and Step 2 (full regression) are done; Step 3 (deploy) and
Step 4 (manual smoke) are deferred until the long-video soak test
finishes.

## Commits in this rollout (oldest → newest)

| SHA | Subject |
|-----|---------|
| `912493c` | feat(editing): add editing-mode speakers.json registry |
| `924e7d3` | fix(editing): stable color hash + tolerant JSON load (review findings) |
| `30701f8` | feat(editing): allow PATCH to assign editing-registered speakers |
| `579b670` | feat(editing): POST/GET /editing/speakers endpoints + baseline helper |
| `e7d757a` | fix(editing): correct baseline review_state.json path (no review/ subdir) |
| `31ae5cd` | fix(editing): narrow load_baseline_speakers except to StateError |
| `ba21c10` | test(phase1): guard editing/speakers gateway whitelist |
| `bfc8c29` | feat(editing): voice profile inference + retry endpoint for new speakers |
| `31d5c7e` | fix(editing): retry endpoint distinguishes unknown speaker + updated_at refresh |
| `a4d8e0d` | feat(frontend): editing speakers API client |
| `c882b46` | feat(frontend): EditPageSpeakerCreateDialog component |
| `7e2f0e2` | feat(frontend): wire add-speaker into edit page (both tabs) |
| `fff26dc` | feat(editing): commit merges editing speakers + voice_profiles into baseline |
| `fd58fa9` | fix(editing): preserve speaker_options insertion order in commit merge |
| `bd81918` | test(editing): consolidate phase1 guards for add-speaker plan |

## Pre-deploy sanity checks (host machine, before any docker cp)

```bash
# 1. Full unit-test sweep — must pass for everything in editing surface.
python -m pytest tests/ -k "editing or phase1 or speaker or post_edit" -x

# 2. Phase1 guards must be 51 passed + 1 skipped (the placeholder).
python -m pytest tests/test_phase1_guards.py tests/test_editing_voice_profile_async.py -v

# 3. Legacy / R2 / record redaction guards must stay green.
python -m pytest tests/test_legacy_cleanup_guards.py \
  tests/test_phase2_download_backend.py \
  tests/test_gateway_record_redaction.py
```

> **Known pre-existing failures** (NOT introduced by this rollout): 14 tests
> in `test_assemblyai_transcriber.py` / `test_process_pipeline.py` /
> `test_tts_generator.py` fail on main due to an unrelated transcriber
> sentence-merging change. Verified by `git stash` + retest. Do **not**
> block this deploy on them.

## Backend — `aivideotrans-app` container

`docker cp` the following Python files, then `docker restart aivideotrans-app`.
Order doesn't matter (single container, single restart).

```bash
# editing speakers registry + endpoints
docker cp src/services/jobs/editing_speakers.py \
  aivideotrans-app:/opt/aivideotrans/app/src/services/jobs/editing_speakers.py

# voice profile inference (Pass 3 mode='studio')
docker cp src/services/jobs/editing_voice_profile.py \
  aivideotrans-app:/opt/aivideotrans/app/src/services/jobs/editing_voice_profile.py

# segment patch path now allows editing-registered speakers
docker cp src/services/jobs/editing_segments.py \
  aivideotrans-app:/opt/aivideotrans/app/src/services/jobs/editing_segments.py

# commit pipeline merges editing speakers + voice_profiles into baseline
docker cp src/services/jobs/editing_commit.py \
  aivideotrans-app:/opt/aivideotrans/app/src/services/jobs/editing_commit.py

# Job API exposes the new endpoints
docker cp src/services/jobs/api.py \
  aivideotrans-app:/opt/aivideotrans/app/src/services/jobs/api.py

docker restart aivideotrans-app
```

> If the production deployment is using the bind-mount dev-mode setup
> (CLAUDE.md notes this is the current state), `docker cp` is unnecessary
> for `src/` — `docker restart aivideotrans-app` alone picks up host
> changes. Verify with:
> ```bash
> docker exec aivideotrans-app readlink -f /opt/aivideotrans/app/src/services/jobs/editing_voice_profile.py
> ```
> If the path is `/opt/aivideotrans/app/src/...` (no symlink) **and**
> `docker inspect` shows no bind mount on `/opt/aivideotrans/app/src`,
> then the container is image-baked and the `docker cp` block above is
> required.

## Gateway — `aivideotrans-gateway` container

```bash
# Adds editing/speakers to mutation whitelist + retry-profile dynamic path
docker cp gateway/job_intercept.py \
  aivideotrans-gateway:/opt/aivideotrans/gateway/job_intercept.py

docker restart aivideotrans-gateway
```

Gateway feature flag (already set in production env):
```
AVT_ENABLE_POST_EDIT=true
```

## Frontend — `frontend-next/`

Two new components + modified edit page. Standalone build needed.

```bash
cd frontend-next
npm run build      # next build (standalone output in .next/standalone)
```

Then deploy via the existing Caddy-served standalone path (whatever the
project's standard frontend deploy does — typically `Upload-Via-154.cmd` /
`Deploy-Via-154.cmd` per CLAUDE.md feedback memo on remote deploy).

Frontend feature flag (already set):
```
NEXT_PUBLIC_ENABLE_POST_EDIT=1
```

Files in this rollout:
- `frontend-next/src/lib/api/editing.ts` — speakers CRUD client
- `frontend-next/src/components/workspace/EditPageSpeakerCreateDialog.tsx` — new dialog
- `frontend-next/src/components/workspace/EditPageSpeakerProfileBadge.tsx` — profile status badge
- `frontend-next/src/app/(app)/workspace/[jobId]/edit/VoiceModifyTab.tsx` — voice tab integration
- `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` — text tab integration

## Restart order

1. **Backend first** — `aivideotrans-app` exposes the new
   `POST/GET /editing/speakers` and `POST /editing/speakers/{id}/retry-profile`
   endpoints. If gateway is restarted before backend, gateway proxies
   the call and gets 404 from upstream (transient, recovers when
   backend comes up).
2. **Gateway second** — picks up the updated mutation whitelist.
3. **Frontend last** — UI surfaces won't 404 once gateway is live.

No `--force-recreate` needed (no env_file or compose-level changes).
Plain `docker restart` reloads the bind-mounted code per the dev-mode
setup. If env vars `AVT_ENABLE_POST_EDIT` / `NEXT_PUBLIC_ENABLE_POST_EDIT`
need to change, then `docker compose up -d --force-recreate <svc>` is
required (compose `restart` does NOT reread env_file — see CLAUDE.md
TLS/Caddy memo).

## Post-deploy verification (manual smoke — Step 4, deferred)

Once the long-video soak finishes and the user runs the manual smoke:

1. Open a `succeeded` Studio job → click "修改".
2. **Voice tab**: click "新增说话人" → type "测试C" → submit. Verify:
   - New speaker appears with `profile_status: pending_segments`.
   - PATCH a segment to assign the new speaker → status flips to
     `inferring` → after a few seconds → `ready` (or `failed` with retry).
   - Click retry on a `failed` profile → status flips back to `inferring`.
3. **Text tab**: same speaker dropdown should include the new speaker.
4. Click commit (overwrite). Verify:
   - `editor/speakers.json` baseline has the new speaker merged in
     (insertion order preserved).
   - `voice_profiles.json` baseline has the new profile under the
     correct speaker_id.
   - Alignment + publish runs without re-invoking TTS for unchanged
     segments (D26 invariant — guarded by
     `test_editing_commit_module_no_paid_api_imports`).

## Rollback

If post-deploy smoke fails:

```bash
# Backend rollback
git checkout 10b3e68 -- src/services/jobs/editing_*.py src/services/jobs/api.py
docker cp ... aivideotrans-app:/opt/aivideotrans/app/...
docker restart aivideotrans-app

# Gateway rollback
git checkout 10b3e68 -- gateway/job_intercept.py
docker cp gateway/job_intercept.py aivideotrans-gateway:...
docker restart aivideotrans-gateway

# Frontend rollback: redeploy previous standalone build
```

`10b3e68` (R2 publisher Stage A) is the last commit before the
add-speaker plan started.

> Important: rolling back **after** a user has clicked "新增说话人" on
> a job means the rollback codebase will see new fields in
> `editor/editing/speakers.json` it doesn't recognise. The pre-existing
> `tolerant JSON load` (commit `924e7d3`) is FORWARD-tolerant only;
> if a user is mid-edit during rollback, abandon their `editor/editing/`
> by force-cancelling that job (`editing_idle_scanner` admin path).
