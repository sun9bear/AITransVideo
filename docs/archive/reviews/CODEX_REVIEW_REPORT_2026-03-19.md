# Codex Review Report

Date: 2026-03-19
Workspace: `D:\Claude\AIVideoTrans_Codex`

## Scope

Reviewed the current remote-workbench and Web UI surfaces directly from the workspace. This directory is not a Git worktree, so the review was based on the latest files on disk rather than a Git diff.

## Summary

- 4 findings
- 3 high-priority correctness/security issues
- 1 medium-priority startup validation issue

## Findings

### 1. [P1] Review endpoints trust caller-supplied `project_dir`

- File: `src/services/web_ui.py`
- Lines: 6587-6738

The review endpoints accept any `project_dir` from the request body and pass it into helpers that read and write `review_state.json`, `transcript/transcript.json`, and `translation/segments.json`.

Unlike the result-download path, these handlers do not verify that the path stays under `projects/`. An authenticated caller can therefore modify any directory with the expected structure instead of being limited to the active project.

Affected routes:

- `/api/review/speaker/save`
- `/api/review/speaker/approve`
- `/api/review/voice/approve`
- `/api/review/voice/cancel`
- `/api/review/translation/save`
- `/api/review/translation/approve`

### 2. [P1] Audio preview endpoint bypasses the manifest whitelist

- File: `src/services/web_ui.py`
- Lines: 6456-6477

The `/api/project-file` route serves any audio file whose absolute path is under the repository root.

That bypasses the manifest-derived whitelist used by the public result surface. A caller who knows a path can fetch unrelated audio files under the repo, such as files in `voice_bank` or other non-result folders.

### 3. [P1] Job API mode drops selected speakers and voice overrides

- Files:
- `src/services/web_ui.py` lines 564-606
- `src/services/jobs/process_runner.py` lines 186-199

The Web UI accepts `speakers`, `voice_a`, and `voice_b`, and the handler validates them, but the `POST /jobs` payload never sends those values to the Job API.

Downstream, the process runner hardcodes:

```text
--speakers auto
```

and does not pass `--voice-a` or `--voice-b`.

As a result, remote-workbench jobs ignore the choices made in the UI.

### 4. [P2] Public-entry startup can be reported as healthy after Caddy exits

- Files:
- `scripts/start_remote_workbench.ps1` lines 223-233
- `src/services/public_entry_caddy.py` lines 207-219

`Wait-WorkbenchServiceStartup` returns as soon as it sees the startup marker in stdout.

However, `run_caddy_public_entry()` prints that marker before `subprocess.run(...)` blocks on the long-lived Caddy process. If Caddy exits immediately after launch, the script can still treat startup as successful even though no public entry listener remains alive.

## Verification

Executed:

```powershell
python -m pytest -q
```

Result:

- `542 passed`
- 2 deprecation warnings

## Notes

- These findings were identified by source inspection and confirmed against the current workspace behavior.
- The current tests pass, which suggests these cases are not yet covered by automated tests.
