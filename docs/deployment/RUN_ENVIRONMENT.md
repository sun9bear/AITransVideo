# RUN_ENVIRONMENT

Last updated: 2026-03-18

## Verified Baseline

Most recently verified in this workspace:

- OS: Windows
- Python: 3.12
- `pytest -q` -> `474 passed, 2 warnings`
- `python main.py --help` prints usage text

Important note:

- `python main.py --help` currently exits through the usage/SystemExit path, so it prints successfully but exits with code `1`

## Minimum Practical Runtime Assumption

Treat the repository today as:

- Python `3.11+`
- a local Python environment with the required packages already installed
- no lockfile-based dependency bootstrap yet

This repo currently does **not** provide:

- `pyproject.toml`
- `requirements.txt`
- `requirements-dev.txt`

So the environment should currently be documented and reproduced explicitly rather than assumed from package metadata.

## Core Python Packages By Feature

### Broadly useful

- `pytest`
- `requests`
- `pydub`

### Needed for YouTube download flow

- `yt-dlp`

### Needed for real transcription path

- `assemblyai`

### Needed for Gemini translation path

- `google-genai`

### Notes

- Not every feature path needs every package
- mock/demo flows can run with a smaller environment than full real-provider flows

## External Tools

### `ffmpeg`

`ffmpeg` must be available in `PATH` for the practical media paths that involve:

- YouTube download audio extraction
- audio processing
- publish video rendering

If `ffmpeg` is missing, the current YouTube/media flow will fail clearly.

### `yt-dlp`

`yt-dlp` should currently be treated as a **feature dependency**, not a universal baseline dependency.

You need it when running:

- `python main.py process <youtube_url> ...`
- YouTube-oriented ingestion paths

You do **not** need it for every purely local review/document/testing task.

## Project-Local Config

Current project-local config file:

- `autodub.local.json`

Current behavior:

- if the file exists, services read it
- if the file does not exist, services fall back to env/defaults

Current config priority:

- process env
- Windows user persisted env
- Windows machine persisted env
- `autodub.local.json`
- code defaults

On non-Windows systems, the Windows registry layers do not apply.

## Alipay Website Payment

Current project recommendation for real Alipay collection:

- Use one single `网页应用` APPID for both:
  - `电脑网站支付` (`alipay.trade.page.pay`)
  - `手机网站支付` (`alipay.trade.wap.pay`)
- Do **not** split PC and H5 across different APPIDs unless the codebase is
  explicitly upgraded to a dual-app routing model.

Current production-facing values for the `aitrans.video` deployment:

- `AVT_ALIPAY_APP_ID=2021006147642779`
- `AVT_ALIPAY_NOTIFY_URL=https://aitrans.video/api/billing/webhooks/alipay`
- `AVT_ALIPAY_RETURN_URL=https://aitrans.video/settings/billing`

Other required env vars:

- `AVT_ALIPAY_APP_PRIVATE_KEY`
- `AVT_ALIPAY_PUBLIC_KEY`

Optional but recommended:

- `AVT_ALIPAY_SELLER_ID`

Operational notes:

- `应用网关` should point to:
  - `https://aitrans.video/api/billing/webhooks/alipay`
- 本项目唯一的线上域名是 `aitrans.video`（不含 TLD 变体）。所有对外回调
  （支付宝 / SMS / 第三方 webhook）一律指向 `https://aitrans.video/...`。
  早期文档曾出现过 `aitransvideo.com` 系列子域（如 `api.aitransvideo.com`），
  **那不是本项目拥有的域名**，是历史文档里的误写，不应作为任何配置、
  回调、Tunnel ingress 或业务代码的依据。
- `授权回调地址` is **not required** for the current payment-only flow.
  It belongs to Alipay user authorization / `openid` style scenarios, not
  website payment return handling.
- `接口内容加密方式` (AES) is **not used** by the current PC/H5 payment path.
  If an AES key was exposed in chat or screenshots, rotate it in the Alipay
  console and do not rely on the leaked value.

## Current Recommended Commands

Run from the repository root.

### Show CLI surface

```bash
python main.py --help
```

### Start services (Docker Compose deployment)

Current architecture uses Gateway + Job API + Next.js frontend:

| Service | Port | Purpose |
|---------|------|---------|
| Gateway | 8880 | Auth, routing, proxy |
| Job API | 8877 | Job CRUD, status, logs, artifacts |
| Next.js | 3000 | Frontend pages |

> Note: Web UI (8876) has been deprecated. All functionality has been migrated to the services above.

### Start remote workbench services

Remote-workbench now uses:

- runtime config: `remote_workbench.local.json`
- startup script: `scripts/start_remote_workbench.ps1`
- default runtime logs: `runtime_logs/`

Start Job API only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service job-api
```

Start all local services:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service all
```

### Start public entry (Caddy)

Public entry uses:

- `Caddy` for HTTPS + reverse proxy
- API traffic reverse proxy target: `127.0.0.1:8880` (Gateway)
- Page traffic reverse proxy target: `127.0.0.1:3000` (Next.js)
- `Job API` and `control-panel` remain localhost-only

Before starting public entry:

- set `public_entry.enabled=true` in `remote_workbench.local.json`
- set:
  - `public_entry.site_host`
  - `public_entry.https_url`
- install `caddy.exe` and place it in `PATH`, or set `public_entry.executable_path` in `remote_workbench.local.json`
- set:
  - `AUTODUB_PUBLIC_ENTRY_USERNAME`
  - `AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH`
- generate the password hash with:

```powershell
caddy hash-password --plaintext "your-strong-password"
```

Preflight only, without starting the background service:

```powershell
python scripts/run_remote_workbench_service.py public-entry --config remote_workbench.local.json --check-only
```

Or through the Windows startup wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service public-entry -CheckOnly
```

Current expected behavior:

- `--check-only` / `-CheckOnly` exits non-zero and prints a direct failure reason when:
  - `caddy.exe` cannot be found
  - `AUTODUB_PUBLIC_ENTRY_USERNAME` or `AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH` is missing
  - `caddy validate --config ... --adapter caddyfile` fails
- `scripts/start_remote_workbench.ps1 -Service public-entry` and `-Service all` now run this public-entry preflight before background launch, so they stop before spawning `public-entry` if any of the above blockers remain

Start only the public entry:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service public-entry
```

Start local services plus public entry:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service all
```

Public-entry troubleshooting files:

- `runtime_logs/public-entry.Caddyfile`
- `runtime_logs/public-entry.access.log`
- `runtime_logs/public-entry.stdout.log`
- `runtime_logs/public-entry.stderr.log`

Minimal pass criteria for P2 public-entry validation:

- preflight prints `Public entry preflight passed for https://...`
- it also prints the resolved `Caddy executable`, generated `Caddyfile`, `Access log`, and reverse-proxy upstream
- `powershell -ExecutionPolicy Bypass -File scripts/start_remote_workbench.ps1 -Service all` completes without a public-entry preflight error
- browsing to the configured HTTPS URL shows Basic Auth first, then reaches the Web UI after valid credentials

### Start control panel

```bash
python main.py control-panel
```

Default URL:

- `http://127.0.0.1:8765`

### Run tests

```bash
python -m pytest -q
```

### Legacy end-to-end YouTube path

```bash
python main.py process <youtube_url>
```

### Workflow-oriented local audio demo

```bash
python main.py local-audio-demo <local_audio_path> [translation_mode] [tts_mode] [--output editor|publish|both]
```

### Workflow-oriented local video demo

```bash
python main.py local-video-demo <local_video_path> [translation_mode] [tts_mode] [--output editor|publish|both]
```

## Current Practical Expectations

### Most stable today

- `process`
- Gateway + Job API + Next.js (Docker Compose)
- `control-panel`
- `python -m pytest -q`

### Workflow demos

- `local-audio-demo` and `local-video-demo` are already on the newer output-dispatch path
- `local-audio-demo` still only supports `editor` in practice because publish needs a source video

## Windows / Unix Notes

### Windows

This workspace has been verified on Windows.

Current Windows-specific behavior includes:

- reading persisted user/machine env values from the registry
- local operator flow primarily validated with Windows-style paths

### Unix-like systems

Unix-like use is still plausible, but should currently be treated as less verified in this workspace.

Practical implications:

- rely on process env and `autodub.local.json` rather than Windows registry persistence
- double-check shell quoting and path handling
- ensure `ffmpeg` is installed in `PATH`

## Current Documentation Recommendation

For someone resuming the project, the best reading order is:

- `CURRENT_PROJECT_STATUS.md`
- `REFACTOR_PHASE1_SUMMARY.md`
- `RUN_ENVIRONMENT.md`
- `WEB_UI_STATUS.md`
- `README.md`
