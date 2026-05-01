# Marketing FeaturedDemos Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a horizontally-scrolling demo carousel to the marketing homepage that plays 5 hand-picked English-source / Chinese-dubbed clip pairs, with auto-scroll, hover-zoom-others-shrink, and click-to-play coordination.

**Architecture:** Next.js Server Component reads a static JSON config; a `"use client"` shell owns a "currently playing card id" React Context; each card has its own EN/CN tab state and a native `<video>` element that registers with the context. Pure-CSS auto-scroll keyframe + `:has()` selector for the hover-shrink-others effect — no JS state for the visual layer. 5 demo asset triplets (`original.mp4` + `dubbed.mp4` + `poster.jpg`) are extracted once via ffmpeg + Pillow inside the production `aivideotrans-app` container, copied out to the dev workstation, and committed under `frontend-next/public/marketing/demos/`.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript 5, Tailwind v4, native HTML5 `<video>`, ffmpeg + Pillow (one-off asset gen).

**Reference spec:** [`docs/specs/2026-05-01-marketing-featured-demos-design.md`](../specs/2026-05-01-marketing-featured-demos-design.md) — read this first; it pins down all design decisions including the 5 demo segments, JSON schema, interaction model, and asset extraction commands.

---

## File Structure

**New frontend files:**

| Path | Responsibility | Boundary |
|---|---|---|
| `frontend-next/src/components/marketing/featured-demos.tsx` | Top-level Server Component. Statically imports the JSON, validates the shape, and renders `<FeaturedDemosClient>`. Returns `null` when the demo list is empty/malformed. | RSC (no `"use client"`) |
| `frontend-next/src/components/marketing/featured-demos-client.tsx` | Client shell. Owns `<FeaturedDemosContext.Provider>` (currentlyPlayingId state), renders the carousel section heading + the duplicated-twice card track, and applies the auto-scroll + reduced-motion + mobile classes. | `"use client"` |
| `frontend-next/src/components/marketing/featured-demos-context.tsx` | Defines `FeaturedDemosContext` with `{ currentlyPlayingId, setCurrentlyPlayingId }`. Exports a `useFeaturedDemos()` hook for consumers. | `"use client"` |
| `frontend-next/src/components/marketing/featured-demo-card.tsx` | Single card. Owns local `useState` for the active tab (`"zh"` / `"en"`), holds a `videoRef`, registers `onPlay` with the context, observes context changes and pauses itself when another card starts playing. | `"use client"` |

**New static assets:**

| Path | Source | Notes |
|---|---|---|
| `frontend-next/public/marketing/demos/featured-demos.json` | Hand-authored | Schema in spec §6 |
| `frontend-next/public/marketing/demos/karpathy-agent-engineering/{original.mp4, dubbed.mp4, poster.jpg}` | Generated in Phase A | ~3-5 MB MP4s, ~80-150 KB poster |
| `frontend-next/public/marketing/demos/muniba-disability-vs-ability/{original.mp4, dubbed.mp4, poster.jpg}` | Generated in Phase A | |
| `frontend-next/public/marketing/demos/buffett-2008-buy-stocks/{original.mp4, dubbed.mp4, poster.jpg}` | Generated in Phase A | |
| `frontend-next/public/marketing/demos/vox-why-ok/{original.mp4, dubbed.mp4, poster.jpg}` | Generated in Phase A | |
| `frontend-next/public/marketing/demos/jensen-matrix-multiplication/{original.mp4, dubbed.mp4, poster.jpg}` | Generated in Phase A | |

**Modified files:**

| Path | What changes | Lines |
|---|---|---|
| `frontend-next/src/app/(marketing)/page.tsx` | Add `import { FeaturedDemos }` + place `<FeaturedDemos />` between `<ProductProof />` and `<WorkflowShowcase />`. | ~3 LOC |
| `frontend-next/src/app/globals.css` | Append `@keyframes featured-demos-marquee`, `.demo-track`, `.demo-card`, hover/`:has()`/reduced-motion rules. | ~40-60 LOC |

**Verification context — no test framework**: `frontend-next/` does not have Jest / Vitest / Playwright configured. Verification is via `tsc --noEmit`, ESLint, dev-preview MCP browser inspection, and production verification against the live URL — same model used for every other marketing component this session has shipped.

---

## Phase A — Asset generation (one-off, production host)

> Phase A runs against the production host (`aivideotrans-app` container) using the existing SOCKS-over-154 SSH path. All ffmpeg / PIL work happens inside the container so we don't have to install anything on the dev workstation. Output assets are tarred and `docker cp`'d to the dev workstation, then committed.

### Task A1: SSH preflight + workspace setup

**Files:**
- Read-only / shell: no source files modified

- [ ] **Step 1: Start SOCKS-over-154 proxy if not already running**

```bash
PYTHONIOENCODING=utf-8 powershell.exe -ExecutionPolicy Bypass -File "D:/daili/scripts/Start-SSH-154-Test-Proxy.ps1"
```

Expected output: `SSH test proxy is listening on 127.0.0.1:11080` and `Current egress IP matches the 154 direct line: 154.29.155.252`.

- [ ] **Step 2: Verify ffmpeg + Pillow available in `aivideotrans-app` container**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'which ffmpeg && python3 -c \"from PIL import Image; print(Image.__version__)\"'"
```

Expected: `/usr/bin/ffmpeg` on first line, Pillow version (e.g. `10.x.x`) on second line.

- [ ] **Step 3: Discover available CJK font path inside the container**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'fc-list :lang=zh-cn | head -10'"
```

Expected: at least one of `Noto Sans CJK SC`, `Noto Serif CJK SC`, `Source Han Sans`. Note the file paths.

If output is empty (no CJK fonts), install them:

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'apt-get update && apt-get install -y fonts-noto-cjk'"
```

Then re-run Step 3 to capture the path. Record the title font path (Bold) and subtitle font path (Regular/Medium) for use in Phase A7.

- [ ] **Step 4: Create the working directory inside the container**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app mkdir -p /tmp/demos"
```

Expected: no output, exit 0.

---

### Task A2-A6: Extract clips for each of the 5 demos

> Each task runs three ffmpeg commands inside the container: English clip, Chinese clip, raw poster frame. Repeat for all 5 demos with the per-demo parameters.

**Per-demo parameter table** — paste these into the SSH commands:

| Slug | JOB_ID | START | DUR | MID |
|---|---|---|---|---|
| `karpathy-agent-engineering` | `cd545d7b1325439182a7db40ea1e2d8d` | 985.7 | 52.5 | 1011.95 |
| `muniba-disability-vs-ability` | `524c5253ad514692bcc06aa170f9567f` | 216.9 | 61.5 | 247.65 |
| `buffett-2008-buy-stocks` | `f08cabc1267642b98a9d774a9e2a5da4` | 577.1 | 69.0 | 611.6 |
| `vox-why-ok` | `8463bb721d1c4ad697d2bc0f317c8cdf` | 185.9 | 59.0 | 215.4 |
| `jensen-matrix-multiplication` | `3066774da2a64848b2f4d1d2824e022b` | 1261.3 | 58.6 | 1290.6 |

The path stem inside the container is `/opt/aivideotrans/app/projects/<workspace>/<job_id>/`. The plan author resolves `<workspace>` for each job via the job record. For Karpathy it's `342bbde3-903b-4944-a53c-12a1de0b5ca9`. For the other 4, run:

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "python3 -c '
import json
for jid in [\"524c5253ad514692bcc06aa170f9567f\",\"f08cabc1267642b98a9d774a9e2a5da4\",\"8463bb721d1c4ad697d2bc0f317c8cdf\",\"3066774da2a64848b2f4d1d2824e022b\"]:
    d=json.load(open(f\"/opt/aivideotrans/data/jobs/job_{jid}.json\"))
    print(f\"{jid[:8]} -> {d[\"project_dir\"]}\")
'"
```

Substitute the `<workspace>/<job_id>` segment of each project_dir into the commands below.

#### Task A2: Karpathy clip

**Files:**
- Output (in container): `/tmp/demos/karpathy-agent-engineering/{original.mp4, dubbed.mp4, poster-raw.jpg}`

- [ ] **Step 1: mkdir + extract English clip with frame-accurate output-seek**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'JOB=/opt/aivideotrans/app/projects/342bbde3-903b-4944-a53c-12a1de0b5ca9/job_cd545d7b1325439182a7db40ea1e2d8d; OUT=/tmp/demos/karpathy-agent-engineering; mkdir -p \$OUT && ffmpeg -y -i \$JOB/video/original.mp4 -ss 985.7 -t 52.5 -accurate_seek -vf scale=-2:720 -c:v libx264 -preset slow -crf 23 -c:a aac -b:a 128k -ac 2 -ar 44100 -movflags +faststart \$OUT/original.mp4 2>&1 | tail -3'"
```

Expected: `frame=... time=00:00:52.50 ...` followed by `video:N audio:N global headers:0 ...`.

- [ ] **Step 2: Extract Chinese dub clip (same time range)**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'JOB=/opt/aivideotrans/app/projects/342bbde3-903b-4944-a53c-12a1de0b5ca9/job_cd545d7b1325439182a7db40ea1e2d8d; OUT=/tmp/demos/karpathy-agent-engineering; ffmpeg -y -i \$JOB/publish/dubbed_video.mp4 -ss 985.7 -t 52.5 -accurate_seek -vf scale=-2:720 -c:v libx264 -preset slow -crf 23 -c:a aac -b:a 128k -ac 2 -ar 44100 -movflags +faststart \$OUT/dubbed.mp4 2>&1 | tail -3'"
```

Expected: same as Step 1.

- [ ] **Step 3: Extract poster raw frame at clip midpoint**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'JOB=/opt/aivideotrans/app/projects/342bbde3-903b-4944-a53c-12a1de0b5ca9/job_cd545d7b1325439182a7db40ea1e2d8d; OUT=/tmp/demos/karpathy-agent-engineering; ffmpeg -y -ss 1011.95 -i \$JOB/video/original.mp4 -frames:v 1 -vf scale=-2:720 \$OUT/poster-raw.jpg 2>&1 | tail -3'"
```

Expected: single-frame output, no errors.

- [ ] **Step 4: Verify file existence + sizes**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app ls -lh /tmp/demos/karpathy-agent-engineering/"
```

Expected: 3 files. `original.mp4` and `dubbed.mp4` should each be 3-6 MB. `poster-raw.jpg` should be 100-300 KB.

- [ ] **Step 5: Verify clip duration is exactly 52.5s (within 0.1s)**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'for f in /tmp/demos/karpathy-agent-engineering/*.mp4; do echo \$f; ffprobe -v error -show_entries format=duration -of default=nw=1 \$f; done'"
```

Expected: both files report `duration=52.5` (±0.1s).

#### Task A3: Muniba clip

Repeat Task A2's 5 steps with these substitutions:
- `OUT=/tmp/demos/muniba-disability-vs-ability`
- `JOB` = path resolved from job `524c5253ad514692bcc06aa170f9567f`
- `-ss 216.9 -t 61.5` (for both video clips)
- `-ss 247.65` (for poster)

#### Task A4: Buffett clip

- `OUT=/tmp/demos/buffett-2008-buy-stocks`
- `JOB` = path resolved from job `f08cabc1267642b98a9d774a9e2a5da4`
- `-ss 577.1 -t 69.0`
- `-ss 611.6` (for poster)

#### Task A5: Vox-OK clip

- `OUT=/tmp/demos/vox-why-ok`
- `JOB` = path resolved from job `8463bb721d1c4ad697d2bc0f317c8cdf`
- `-ss 185.9 -t 59.0`
- `-ss 215.4` (for poster)

#### Task A6: Jensen clip

- `OUT=/tmp/demos/jensen-matrix-multiplication`
- `JOB` = path resolved from job `3066774da2a64848b2f4d1d2824e022b`
- `-ss 1261.3 -t 58.6`
- `-ss 1290.6` (for poster)

---

### Task A7: Generate poster overlays for all 5 demos

**Files:**
- Output (in container): `/tmp/demos/<slug>/poster.jpg` (replaces poster-raw.jpg with title overlay)

- [ ] **Step 1: Write the PIL overlay script to a temp file in the container**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'cat > /tmp/demos/overlay_poster.py << EOF
from PIL import Image, ImageDraw, ImageFont
import sys, os

# Args: slug title segment_label
slug, title, segment_label = sys.argv[1], sys.argv[2], sys.argv[3]
in_path = f\"/tmp/demos/{slug}/poster-raw.jpg\"
out_path = f\"/tmp/demos/{slug}/poster.jpg\"

img = Image.open(in_path).convert(\"RGB\")
W, H = img.size
draw = ImageDraw.Draw(img)

# Resolve fonts — replace these paths with the ones discovered in Task A1 Step 3
FONT_TITLE = os.environ.get(\"FONT_TITLE\", \"/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc\")
FONT_SUB = os.environ.get(\"FONT_SUB\", \"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc\")

font_title = ImageFont.truetype(FONT_TITLE, 42)
font_sub = ImageFont.truetype(FONT_SUB, 22)

# Cinnabar accent strip in lower-left
strip_left = 64
strip_top = H - 140
strip_bottom = H - 60
draw.rectangle([(strip_left, strip_top), (strip_left + 4, strip_bottom)], fill=\"#C73E3A\")

# Title text + subtitle
title_x = strip_left + 22
draw.text((title_x, strip_top + 8), title, font=font_title, fill=(255, 255, 255))
draw.text((title_x, strip_top + 58), segment_label, font=font_sub, fill=(255, 255, 255, 200))

img.save(out_path, \"JPEG\", quality=85, optimize=True, progressive=True)
print(f\"OK {slug} -> {out_path}\")
EOF
echo Done'"
```

Expected: `Done`.

- [ ] **Step 2: Run the overlay for all 5 demos**

The plan author runs this 5× with the right arguments. Use `FONT_TITLE` / `FONT_SUB` env vars to inject the paths captured in Task A1 Step 3 if they differ from the defaults.

```bash
# Karpathy
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app python3 /tmp/demos/overlay_poster.py karpathy-agent-engineering '安德烈·卡帕西谈智能体工程' '16:25 – 17:18 · 52s'"

# Muniba
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app python3 /tmp/demos/overlay_poster.py muniba-disability-vs-ability '穆尼巴·马扎里：拥抱不完美' '03:36 – 04:38 · 61s'"

# Buffett
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app python3 /tmp/demos/overlay_poster.py buffett-2008-buy-stocks 'CNBC专访巴菲特：2008年与价值投资' '09:37 – 10:46 · 69s'"

# Vox-OK
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app python3 /tmp/demos/overlay_poster.py vox-why-ok '为什么我们总说\"OK\"' '03:05 – 04:04 · 59s'"

# Jensen
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app python3 /tmp/demos/overlay_poster.py jensen-matrix-multiplication '黄仁勋专访：英伟达的护城河' '21:01 – 21:59 · 58s'"
```

Expected: each command prints `OK <slug> -> /tmp/demos/<slug>/poster.jpg`.

- [ ] **Step 3: Verify all 5 poster.jpg files exist with non-zero size**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'for d in /tmp/demos/*/; do echo \$d; ls -lh \${d}poster.jpg 2>&1; done'"
```

Expected: 5 lines like `/tmp/demos/<slug>/poster.jpg` with sizes 80-200 KB.

---

### Task A8: docker cp the assets back to the dev workstation

**Files:**
- Output (host): `/tmp/demos.tar.gz` on production host, then transferred to dev workstation

- [ ] **Step 1: Tar up the demos directory inside the container**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker exec aivideotrans-app sh -c 'cd /tmp && tar -czf /tmp/demos.tar.gz demos/' && docker cp aivideotrans-app:/tmp/demos.tar.gz /tmp/demos.tar.gz && ls -lh /tmp/demos.tar.gz"
```

Expected: ~30-50 MB tarball at `/tmp/demos.tar.gz` on the production host.

- [ ] **Step 2: SFTP-pull the tarball to dev workstation**

Inline paramiko script (the helper script raises errors with `client.open_sftp()`; use `SFTPClient.from_transport(t)` directly):

```bash
PYTHONIOENCODING=utf-8 python -c "
import paramiko, socks
sock = socks.socksocket()
sock.set_proxy(socks.SOCKS5, '127.0.0.1', 11080, rdns=True)
sock.settimeout(20)
sock.connect(('5.78.122.220', 22))
sock.settimeout(None)
key = paramiko.Ed25519Key.from_private_key_file('C:/Users/Administrator/.ssh/id_ed25519')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname='5.78.122.220', port=22, username='root', pkey=key, sock=sock,
               timeout=20, banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=False)
sftp = paramiko.SFTPClient.from_transport(client.get_transport())
sftp.get('/tmp/demos.tar.gz', 'D:/Claude/temp/demos.tar.gz')
print('size:', __import__('os').path.getsize('D:/Claude/temp/demos.tar.gz'))
sftp.close(); client.close()
"
```

Expected: `size: 30000000` to `50000000` (bytes).

- [ ] **Step 3: Extract under `frontend-next/public/marketing/`**

```bash
mkdir -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/
cd D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/
tar -xzf D:/Claude/temp/demos.tar.gz
ls demos/
```

Expected: 5 directories listed (one per slug). Each contains `original.mp4`, `dubbed.mp4`, `poster.jpg` (the `poster-raw.jpg` files can be deleted after verification — they're not needed in the bundle).

- [ ] **Step 4: Clean up `poster-raw.jpg` files (not needed in production)**

```bash
find D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/demos -name "poster-raw.jpg" -delete
```

- [ ] **Step 5: Open one poster.jpg locally to verify the overlay text rendered correctly**

Open `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/demos/karpathy-agent-engineering/poster.jpg` in any image viewer. You should see the AI Ascent stage frame with `安德烈·卡帕西谈智能体工程` rendered in white over a cinnabar accent strip in the lower-left.

If the overlay text is missing or shows tofu boxes (□□□), the font path was wrong — go back to Task A1 Step 3, set `FONT_TITLE` / `FONT_SUB` to verified paths, re-run Task A7 Step 2.

- [ ] **Step 6: Verify all 15 expected files exist**

```bash
ls -R D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/demos/ | grep -E "\.(mp4|jpg)$" | wc -l
```

Expected: `15` (5 demos × 3 files each).

- [ ] **Step 7: Commit the assets**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/public/marketing/demos/
git commit -m "feat(marketing): add 5 hand-picked demo clip triplets for FeaturedDemos

5 sets of {original.mp4, dubbed.mp4, poster.jpg} extracted from completed
Studio jobs. All clips 720p H.264 + AAC, 52-69s each. DSP stretch ratios
verified within 0.85-1.20 in spec §3. Total ~50MB.

Source jobs:
- karpathy-agent-engineering    (job_cd545d7b)  16:25-17:18  52.5s
- muniba-disability-vs-ability  (job_524c5253)  03:36-04:38  61.5s
- buffett-2008-buy-stocks       (job_f08cabc1)  09:37-10:46  69.0s
- vox-why-ok                    (job_8463bb72)  03:05-04:04  59.0s
- jensen-matrix-multiplication  (job_3066774d)  21:01-21:59  58.6s

Posters generated with PIL inside aivideotrans-app container using
Noto CJK fonts. Title + segment label overlay in cinnabar+white style
matching marketing aesthetic. See docs/specs/2026-05-01-marketing-
featured-demos-design.md §7 for the extraction pipeline."
```

---

## Phase B — JSON config

### Task B1: Write `featured-demos.json`

**Files:**
- Create: `frontend-next/public/marketing/demos/featured-demos.json`

- [ ] **Step 1: Write the file**

Write this exact content to `frontend-next/public/marketing/demos/featured-demos.json`:

```json
{
  "version": 1,
  "demos": [
    {
      "id": "karpathy-agent-engineering",
      "display_name": "安德烈·卡帕西谈智能体工程",
      "source_label": "YouTube · 96jN2OCOfLs · AI Ascent 2025",
      "segment_label": "16:25 – 17:18 · 52s",
      "original_src": "/marketing/demos/karpathy-agent-engineering/original.mp4",
      "dubbed_src": "/marketing/demos/karpathy-agent-engineering/dubbed.mp4",
      "poster_src": "/marketing/demos/karpathy-agent-engineering/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    },
    {
      "id": "muniba-disability-vs-ability",
      "display_name": "穆尼巴·马扎里：拥抱不完美",
      "source_label": "YouTube · TEDx 演讲",
      "segment_label": "03:36 – 04:38 · 61s",
      "original_src": "/marketing/demos/muniba-disability-vs-ability/original.mp4",
      "dubbed_src": "/marketing/demos/muniba-disability-vs-ability/dubbed.mp4",
      "poster_src": "/marketing/demos/muniba-disability-vs-ability/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    },
    {
      "id": "buffett-2008-buy-stocks",
      "display_name": "CNBC专访巴菲特：2008年与价值投资",
      "source_label": "CNBC · Becky Quick interview",
      "segment_label": "09:37 – 10:46 · 69s",
      "original_src": "/marketing/demos/buffett-2008-buy-stocks/original.mp4",
      "dubbed_src": "/marketing/demos/buffett-2008-buy-stocks/dubbed.mp4",
      "poster_src": "/marketing/demos/buffett-2008-buy-stocks/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    },
    {
      "id": "vox-why-ok",
      "display_name": "为什么我们总说\"OK\"",
      "source_label": "Vox · YouTube explainer",
      "segment_label": "03:05 – 04:04 · 59s",
      "original_src": "/marketing/demos/vox-why-ok/original.mp4",
      "dubbed_src": "/marketing/demos/vox-why-ok/dubbed.mp4",
      "poster_src": "/marketing/demos/vox-why-ok/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    },
    {
      "id": "jensen-matrix-multiplication",
      "display_name": "黄仁勋专访：英伟达的护城河",
      "source_label": "YouTube · Dwarkesh podcast",
      "segment_label": "21:01 – 21:59 · 58s",
      "original_src": "/marketing/demos/jensen-matrix-multiplication/original.mp4",
      "dubbed_src": "/marketing/demos/jensen-matrix-multiplication/dubbed.mp4",
      "poster_src": "/marketing/demos/jensen-matrix-multiplication/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    }
  ]
}
```

> **Note:** the `natural_width`/`natural_height` fields are based on the spec §6 default (1280×720). After Task A8 the plan author should sanity-check by running `ffprobe` on one of the MP4s — if the actual dimensions differ (e.g. some source was 1280×692 due to letterboxing), update the JSON values.

- [ ] **Step 2: Verify shape with a quick parse + lint**

```bash
node -e "const d=require('D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/public/marketing/demos/featured-demos.json'); console.log('demos:', d.demos.length); d.demos.forEach(x => { if (!x.id || !x.original_src || !x.dubbed_src || !x.poster_src) throw new Error('missing field on '+x.id); }); console.log('all 5 demos present + complete')"
```

Expected: `demos: 5` then `all 5 demos present + complete`.

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/public/marketing/demos/featured-demos.json
git commit -m "feat(marketing): add featured-demos.json config for 5 demo clips

Lists the 5 demo entries with id, display_name, source_label,
segment_label, mp4 paths, poster path, and natural dimensions.
Matches the Phase 2 API response shape so the static→runtime
swap will be a one-line change in the Server Component."
```

---

## Phase C — Frontend components

> Phase C builds the 4 React components + the CSS in dependency order: context → card → client shell → server component → CSS → wire.

### Task C1: `featured-demos-context.tsx`

**Files:**
- Create: `frontend-next/src/components/marketing/featured-demos-context.tsx`

- [ ] **Step 1: Write the context module**

```tsx
"use client"

import { createContext, useCallback, useContext, useState, type ReactNode } from "react"

/**
 * FeaturedDemos — currently-playing-card coordination context.
 *
 * Each <FeaturedDemoCard> registers its id with this context the moment its
 * <video> fires `onPlay`. Other cards subscribe to `currentlyPlayingId` and
 * pause themselves when the id changes to a value other than their own.
 *
 * Design note: we use a single string id (or null) rather than a set, because
 * native browsers only allow one foreground audio stream at a time anyway,
 * and the UX is "starting one card pauses the previous" — the set semantics
 * would just complicate this.
 */

type FeaturedDemosContextValue = {
  currentlyPlayingId: string | null
  setCurrentlyPlayingId: (id: string | null) => void
}

const FeaturedDemosContext = createContext<FeaturedDemosContextValue>({
  currentlyPlayingId: null,
  setCurrentlyPlayingId: () => {},
})

export function FeaturedDemosProvider({ children }: { children: ReactNode }) {
  const [currentlyPlayingId, setCurrentlyPlayingIdState] = useState<string | null>(null)
  // Wrap the setter so we get a stable reference across renders — consumers
  // depend on it inside useEffect; an unstable identity would cause the
  // effect to re-fire each render.
  const setCurrentlyPlayingId = useCallback((id: string | null) => {
    setCurrentlyPlayingIdState(id)
  }, [])
  return (
    <FeaturedDemosContext.Provider value={{ currentlyPlayingId, setCurrentlyPlayingId }}>
      {children}
    </FeaturedDemosContext.Provider>
  )
}

export function useFeaturedDemos() {
  return useContext(FeaturedDemosContext)
}
```

- [ ] **Step 2: TypeScript check**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/components/marketing/featured-demos-context.tsx
git commit -m "feat(marketing): add FeaturedDemos playing-id context"
```

---

### Task C2: `featured-demo-card.tsx`

**Files:**
- Create: `frontend-next/src/components/marketing/featured-demo-card.tsx`

- [ ] **Step 1: Write the card component**

```tsx
"use client"

import { useEffect, useRef, useState } from "react"
import { useFeaturedDemos } from "./featured-demos-context"

/**
 * FeaturedDemoCard — single demo card.
 *
 * Owns:
 *   - Local `tab` state (`"zh"` | `"en"`) for the EN/CN toggle
 *   - `videoRef` for imperative pause / currentTime preservation
 *
 * Reads:
 *   - `currentlyPlayingId` from context — pauses self when another card
 *     starts playing
 *
 * Writes:
 *   - On <video> `onPlay`, calls `setCurrentlyPlayingId(demo.id)`
 *
 * Tab swap behaviour: when the user toggles between zh and en, we capture
 * `currentTime` from the current video, swap the `src`, restore `currentTime`,
 * and leave it paused. Both clips share the exact same time range so the
 * position is meaningful within each clip's local 0..duration timeline.
 */

export type Demo = {
  id: string
  display_name: string
  source_label: string
  segment_label: string
  original_src: string
  dubbed_src: string
  poster_src: string
  natural_width: number
  natural_height: number
}

export function FeaturedDemoCard({ demo, ariaHidden = false }: { demo: Demo; ariaHidden?: boolean }) {
  const [tab, setTab] = useState<"zh" | "en">("zh")
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const { currentlyPlayingId, setCurrentlyPlayingId } = useFeaturedDemos()

  // Pause-others coordination: if another card is playing and ours isn't, pause us.
  useEffect(() => {
    if (currentlyPlayingId && currentlyPlayingId !== demo.id) {
      const v = videoRef.current
      if (v && !v.paused) v.pause()
    }
  }, [currentlyPlayingId, demo.id])

  // Tab swap: capture currentTime, swap src, restore. Leave paused.
  function handleTabChange(next: "zh" | "en") {
    if (next === tab) return
    const v = videoRef.current
    const t = v?.currentTime ?? 0
    setTab(next)
    // After the src swap (driven by the tab state), restore currentTime in
    // a microtask so the new <source> has been mounted.
    queueMicrotask(() => {
      const v2 = videoRef.current
      if (v2) {
        v2.pause()
        try { v2.currentTime = t } catch { /* setting currentTime before metadata is loaded throws — ignore */ }
      }
    })
  }

  function handlePlay() {
    setCurrentlyPlayingId(demo.id)
  }

  const activeSrc = tab === "zh" ? demo.dubbed_src : demo.original_src

  return (
    <article
      className="demo-card group relative flex w-[320px] shrink-0 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-sm transition-[transform,opacity,box-shadow] duration-200 ease-out md:w-[360px]"
      aria-hidden={ariaHidden ? true : undefined}
      aria-label={demo.display_name}
    >
      {/* Tab row — segmented control above video */}
      <div role="tablist" aria-label="原片 / 配音版" className="flex border-b border-border">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "zh"}
          tabIndex={ariaHidden ? -1 : 0}
          onClick={() => handleTabChange("zh")}
          className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
            tab === "zh"
              ? "bg-[color:var(--cinnabar,#C73E3A)] text-white"
              : "bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))] text-[color:var(--cinnabar,#C73E3A)]"
          }`}
        >
          中文配音版
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "en"}
          tabIndex={ariaHidden ? -1 : 0}
          onClick={() => handleTabChange("en")}
          className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
            tab === "en"
              ? "bg-[color:var(--cinnabar,#C73E3A)] text-white"
              : "bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))] text-[color:var(--cinnabar,#C73E3A)]"
          }`}
        >
          英文原片
        </button>
      </div>

      {/* Video element */}
      <div role="tabpanel" className="relative aspect-video bg-black">
        <video
          ref={videoRef}
          key={activeSrc}  // force remount on src swap so currentTime restore lands
          src={activeSrc}
          poster={demo.poster_src}
          controls
          preload="none"
          playsInline
          onPlay={handlePlay}
          width={demo.natural_width}
          height={demo.natural_height}
          className="h-full w-full"
        >
          您的浏览器不支持 video 标签。
        </video>
      </div>

      {/* Footer attribution */}
      <div className="flex flex-col gap-0.5 px-4 py-3 text-xs text-muted-foreground">
        <span className="ink-heading text-sm font-semibold text-foreground">{demo.display_name}</span>
        <span>{demo.source_label}</span>
        <span>{demo.segment_label}</span>
      </div>
    </article>
  )
}
```

- [ ] **Step 2: TypeScript check**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/components/marketing/featured-demo-card.tsx
git commit -m "feat(marketing): add FeaturedDemoCard with EN/CN tab + pause-others"
```

---

### Task C3: `featured-demos-client.tsx`

**Files:**
- Create: `frontend-next/src/components/marketing/featured-demos-client.tsx`

- [ ] **Step 1: Write the client shell**

```tsx
"use client"

import { FeaturedDemosProvider } from "./featured-demos-context"
import { FeaturedDemoCard, type Demo } from "./featured-demo-card"

/**
 * FeaturedDemosClient — the "use client" shell that:
 *
 *   1. Provides FeaturedDemosContext (playing-id coordination)
 *   2. Renders the section heading + the duplicated-twice card track
 *   3. Applies the auto-scroll + hover + reduced-motion styling via classes
 *
 * The card list is rendered **twice** in DOM (`[...demos, ...demos]`) so the
 * CSS `@keyframes` can animate from `translateX(0)` to `translateX(-50%)` and
 * the loop seam lines up with an identical second copy. The duplicate copies
 * carry `aria-hidden` so screen readers and keyboard nav only see the
 * canonical 5 cards.
 *
 * No JS state is required for the visual carousel layer — auto-scroll,
 * pause-on-hover, and hover-shrink-others are all pure CSS using
 * @keyframes + animation-play-state + the :has() selector. See
 * globals.css §"FeaturedDemos carousel" for the rules.
 */

export function FeaturedDemosClient({ demos }: { demos: Demo[] }) {
  // Render demos twice for seamless infinite-loop. The second copy is a
  // visual repeat used only for the keyframe loop point — it's hidden from
  // assistive tech.
  const doubled = [...demos, ...demos]

  return (
    <section
      id="featured-demos"
      className="marketing-reading-surface py-20 sm:py-24"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            真实成片样例
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            听一段实际配音，比看十张截图更有说服力
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            下面 5 段都是已完成的真实任务片段，每张卡片可在「中文配音版」和「英文原片」之间切换。鼠标悬停可放大查看；触屏可左右滑动浏览。
          </p>
        </div>
      </div>

      <FeaturedDemosProvider>
        {/* The track sits OUTSIDE the max-w-6xl wrapper because we want the
            scroll area to extend edge-to-edge. The cards have their own
            internal padding via gap-6 + the section's left padding. */}
        <div className="demo-carousel mt-12 overflow-hidden">
          <div
            className="demo-track flex gap-6 px-4 sm:px-6 lg:px-8 [&>*]:flex-shrink-0"
            // No JS animation — keyframe in globals.css handles the marquee.
          >
            {doubled.map((demo, idx) => (
              <FeaturedDemoCard
                key={`${demo.id}-${idx < demos.length ? "primary" : "duplicate"}`}
                demo={demo}
                ariaHidden={idx >= demos.length}
              />
            ))}
          </div>
        </div>
      </FeaturedDemosProvider>
    </section>
  )
}
```

- [ ] **Step 2: TypeScript check**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/components/marketing/featured-demos-client.tsx
git commit -m "feat(marketing): add FeaturedDemosClient shell with provider + duplicated track"
```

---

### Task C4: `featured-demos.tsx` (Server Component)

**Files:**
- Create: `frontend-next/src/components/marketing/featured-demos.tsx`

- [ ] **Step 1: Write the server component**

```tsx
import { FeaturedDemosClient } from "./featured-demos-client"
import type { Demo } from "./featured-demo-card"
import demosJson from "../../../public/marketing/demos/featured-demos.json"

/**
 * FeaturedDemos — homepage carousel of real dubbed clips.
 *
 * Server Component. Statically imports the JSON config (Next.js inlines
 * small JSON files at build time). Validates the parse result is a list of
 * objects with the required shape, and:
 *
 *   - File missing → build-time module-resolution error (fails the build)
 *   - File malformed JSON → build-time parse error (fails the build)
 *   - File parses but `demos` is empty / not an array → returns null at
 *     runtime; section disappears silently from the page
 *
 * Phase 2 swap: replace the static import with a server-side fetch from
 * GET /api/featured-demos returning the same { version, demos } shape.
 *
 * See: docs/specs/2026-05-01-marketing-featured-demos-design.md
 */

type DemosConfig = {
  version: number
  demos: Demo[]
}

function isValidDemo(d: unknown): d is Demo {
  if (typeof d !== "object" || d === null) return false
  const o = d as Record<string, unknown>
  return (
    typeof o.id === "string" &&
    typeof o.display_name === "string" &&
    typeof o.source_label === "string" &&
    typeof o.segment_label === "string" &&
    typeof o.original_src === "string" &&
    typeof o.dubbed_src === "string" &&
    typeof o.poster_src === "string" &&
    typeof o.natural_width === "number" &&
    typeof o.natural_height === "number"
  )
}

export function FeaturedDemos() {
  const config = demosJson as DemosConfig
  const demos = Array.isArray(config?.demos) ? config.demos.filter(isValidDemo) : []
  if (demos.length === 0) return null
  return <FeaturedDemosClient demos={demos} />
}
```

- [ ] **Step 2: TypeScript check**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`. If TS complains about importing `.json` files, ensure `tsconfig.json` has `"resolveJsonModule": true` (Next.js 16 default does, but verify).

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/components/marketing/featured-demos.tsx
git commit -m "feat(marketing): add FeaturedDemos Server Component with validation + empty fallback"
```

---

### Task C5: CSS — auto-scroll keyframe + hover-shrink-others + reduced-motion

**Files:**
- Modify: `frontend-next/src/app/globals.css` — append new block at the end (after the existing `pulse-cinnabar` / `scroll-y-shot` blocks)

- [ ] **Step 1: Read the current end of globals.css** to know where to append

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json
wc -l D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/globals.css
```

Note the line count for the next step.

- [ ] **Step 2: Append the FeaturedDemos CSS block**

Add this block to the end of `frontend-next/src/app/globals.css`:

```css
/* ==========================================
   FeaturedDemos carousel — homepage section under <ProductProof>.

   Three behaviours implemented via pure CSS, no JS:

   1. Auto-scroll: .demo-track animates translateX from 0 to -50% over 35s
      linearly, infinitely. The card list is rendered TWICE in DOM (see
      <FeaturedDemosClient>) so when -50% is reached, the second copy is
      identically positioned to the first — the keyframe restart at 0%
      is visually seamless.

   2. Pause-on-hover: animation-play-state: paused while the cursor is
      anywhere within .demo-carousel.

   3. Hover-shrink-others: when any .demo-card is hovered, that card scales
      to 1.15 with deeper shadow + z-index lift; siblings scale to 0.92 and
      drop to 55% opacity. Implemented with the :has() selector — no JS.
      Browser baseline: Safari 15.4+, Chrome 105+, Firefox 121+. Older
      browsers degrade gracefully (hovered card still scales up; siblings
      simply don't shrink).

   On mobile / touch (@media (hover: none)) the auto-scroll is disabled
   and the track becomes scroll-snap horizontal. Hover scaling is also
   disabled because there's no cursor.

   prefers-reduced-motion disables the marquee. Hover scaling is preserved
   because it's not a vestibular-trigger (no continuous movement).
   ========================================== */

/* The seamless marquee — moves the track from start to a position where
   the second (duplicate) copy has slid into the first copy's place. */
@keyframes featured-demos-marquee {
  from { transform: translateX(0); }
  to   { transform: translateX(-50%); }
}

/* Auto-scroll track — desktop default. */
.demo-track {
  width: max-content;  /* let the flex children decide width, then scroll */
  animation: featured-demos-marquee 35s linear infinite;
  will-change: transform;
}

/* Pause the marquee when the user is interacting with the carousel. */
.demo-carousel:hover .demo-track {
  animation-play-state: paused;
}

/* Hover-shrink-others using :has(). When any card inside the track is
   hovered, EVERY card in the track is selected and dimmed; the hovered
   card itself wins via the more-specific :hover rule. */
.demo-track:has(.demo-card:hover) .demo-card {
  scale: 0.92;
  opacity: 0.55;
}
.demo-track:has(.demo-card:hover) .demo-card:hover {
  scale: 1.15;
  opacity: 1;
  z-index: 10;
  box-shadow: 0 20px 60px -10px rgba(0, 0, 0, 0.30);
}

/* Mobile / touch: disable both auto-scroll AND hover effects, switch to
   scroll-snap manual swipe. */
@media (hover: none) {
  .demo-carousel {
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    -webkit-overflow-scrolling: touch;
  }
  .demo-track {
    width: max-content;
    animation: none;
    /* On mobile we don't render the duplicate copies — but we leave them
       in DOM (cheap) and just hide them via aria-hidden. The user only
       sees the first 5 in their swipe. */
  }
  .demo-card {
    scroll-snap-align: start;
    width: min(85vw, 320px) !important;
  }
  .demo-card:hover {
    scale: 1 !important;  /* override desktop hover */
    opacity: 1 !important;
  }
  .demo-track:has(.demo-card:hover) .demo-card {
    scale: 1;
    opacity: 1;
  }
}

/* Reduced motion: kill the marquee. Track sits static at translateX(0).
   The hover scale is left alone — it's not a vestibular trigger.
   Note: the global @media (prefers-reduced-motion: reduce) rule at the
   top of this file already clamps animation-duration to 0.01ms for ALL
   animations. The override below makes the intent explicit and prevents
   confusion if the global rule is ever softened. */
@media (prefers-reduced-motion: reduce) {
  .demo-track {
    animation: none;
  }
}
```

- [ ] **Step 3: Verify nothing else in globals.css broke**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/app/globals.css
git commit -m "feat(marketing): add FeaturedDemos carousel CSS — marquee + :has() hover + a11y"
```

---

### Task C6: Wire `<FeaturedDemos />` into the homepage

**Files:**
- Modify: `frontend-next/src/app/(marketing)/page.tsx`

- [ ] **Step 1: Add import + render between ProductProof and WorkflowShowcase**

Edit the imports and the JSX:

```tsx
import { Hero } from "@/components/marketing/hero"
import { PainPoints } from "@/components/marketing/pain-points"
import { ProductProof } from "@/components/marketing/product-proof"
import { FeaturedDemos } from "@/components/marketing/featured-demos"  // ADD
import { Features } from "@/components/marketing/features"
import { WorkflowShowcase } from "@/components/marketing/workflow-showcase"
import { SuitedScenarios } from "@/components/marketing/suited-scenarios"
import { ToolComparison } from "@/components/marketing/tool-comparison"
import { TrustBanner } from "@/components/marketing/trust-banner"
import { PricingPreview } from "@/components/marketing/pricing-preview"
import { Faq } from "@/components/marketing/faq"
import { FinalCta } from "@/components/marketing/final-cta"
```

And in the JSX returned by `HomePage()`:

```tsx
return (
  <>
    <Hero />
    <PainPoints />
    <ProductProof />
    <FeaturedDemos />            {/* ADD — between ProductProof and WorkflowShowcase */}
    <WorkflowShowcase />
    <Features />
    <SuitedScenarios />
    <ToolComparison />
    <TrustBanner />
    <PricingPreview />
    <Faq variant="home" />
    <FinalCta />
  </>
)
```

Also update the JSDoc narrative-arc comment to include FeaturedDemos.

- [ ] **Step 2: TypeScript + lint**

```bash
"D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/node_modules/.bin/tsc" --noEmit -p D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/tsconfig.json; echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git add frontend-next/src/app/'(marketing)'/page.tsx
git commit -m "feat(marketing): wire <FeaturedDemos /> into homepage between ProductProof and WorkflowShowcase"
```

---

## Phase D — Local verification + production deploy

### Task D1: Local dev preview — happy-path verification

**Files:**
- Read-only / no source changes

- [ ] **Step 1: Start the dev server**

Use the Claude Preview MCP:

```
preview_start { name: "frontend-dev" }
```

Expected: `serverId` returned, listening on `:4180`.

- [ ] **Step 2: Navigate to homepage section anchor**

```
preview_eval {
  serverId: <serverId>,
  expression: "(() => { window.location.href = 'http://localhost:4180/#featured-demos'; return 'navigated' })()"
}
```

Wait 2 seconds for the page to load.

- [ ] **Step 3: Verify the section is present and has 10 cards in DOM (5 primary + 5 aria-hidden duplicates)**

```
preview_eval {
  serverId: <serverId>,
  expression: "(() => {
    const section = document.getElementById('featured-demos');
    if (!section) return { err: 'section missing' };
    const cards = section.querySelectorAll('.demo-card');
    const primary = section.querySelectorAll('.demo-card:not([aria-hidden=\"true\"])');
    return { totalCards: cards.length, primaryCards: primary.length };
  })()"
}
```

Expected: `{ totalCards: 10, primaryCards: 5 }`.

- [ ] **Step 4: Verify auto-scroll is animating**

```
preview_eval {
  serverId: <serverId>,
  expression: "(async () => {
    const track = document.querySelector('#featured-demos .demo-track');
    if (!track) return { err: 'track missing' };
    const t1 = getComputedStyle(track).transform;
    await new Promise(r => setTimeout(r, 1500));
    const t2 = getComputedStyle(track).transform;
    return { t1, t2, animating: t1 !== t2 };
  })()"
}
```

Expected: `animating: true` and the matrix tx values differ (e.g. t1 `matrix(1,0,0,1,-30,0)`, t2 `matrix(1,0,0,1,-50,0)`).

- [ ] **Step 5: Verify hover scaling using :has()**

```
preview_eval {
  serverId: <serverId>,
  expression: "(async () => {
    const cards = Array.from(document.querySelectorAll('#featured-demos .demo-card:not([aria-hidden=\"true\"])'));
    if (cards.length < 2) return { err: 'need 2+ cards' };
    cards[0].dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
    cards[0].dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    await new Promise(r => setTimeout(r, 250));
    const hovered = getComputedStyle(cards[0]).scale;
    const sibling = getComputedStyle(cards[1]).scale;
    return { hovered, sibling };
  })()"
}
```

Expected: `hovered: '1.15'`, `sibling: '0.92'`. (Note: programmatic `mouseenter` may not always trigger `:hover`; a follow-up screenshot test is the canonical verifier.)

- [ ] **Step 6: Take a screenshot of the section for manual visual review**

```
preview_screenshot { serverId: <serverId> }
```

Inspect the screenshot:
- Posters render with Chinese title overlay
- 5 cards visible (some may be partially scrolled in)
- EN/CN tabs visible at top of each card
- Carousel sits between ProductProof's deliverables zone and WorkflowShowcase's "从英文视频到中文成片" heading

- [ ] **Step 7: Verify reduced-motion override**

```
preview_eval {
  serverId: <serverId>,
  expression: "(() => {
    // Force reduced-motion via a media-query-emulation polyfill technique:
    // overwrite matchMedia for the test
    const track = document.querySelector('#featured-demos .demo-track');
    return {
      animationName: getComputedStyle(track).animationName,
    };
  })()"
}
```

Expected: `animationName: 'featured-demos-marquee'` (without reduced motion). For a true reduced-motion check, use Chrome DevTools Rendering → Emulate CSS prefers-reduced-motion and reload — the track should stop animating.

- [ ] **Step 8: Stop dev server**

```
preview_stop { serverId: <serverId> }
```

---

### Task D2: Local dev preview — interaction verification

> Steps that need a real cursor / video element interaction. These are best done via dev preview screenshots or a live local browser session.

- [ ] **Step 1: Start dev server again** (if stopped)

Same as Task D1 Step 1.

- [ ] **Step 2: Manually click play on the first card's video, then click play on the second card's video** in a real browser at `http://localhost:4180/#featured-demos`. Verify:
  - First video starts playing
  - When second video starts, first video pauses
  - Console has no errors

- [ ] **Step 3: Toggle the EN/CN tab on one card while playing**. Verify:
  - Video pauses when tab swaps
  - Position is preserved (currentTime ≈ same as before swap)
  - Visual switch is clean (no flash to black)

- [ ] **Step 4: Hover over the carousel and verify auto-scroll pauses**. Move mouse off — auto-scroll resumes.

- [ ] **Step 5: Resize browser to mobile width (~375px)**. Verify:
  - Auto-scroll animation stops
  - Cards become wider per-card (~85vw)
  - Manual swipe / horizontal scroll works
  - Hover scaling does NOT apply

- [ ] **Step 6: Stop dev server**

---

### Task D3: Push commits to main + deploy with `--no-cache`

**Files:**
- No source changes; this is the deploy step

- [ ] **Step 1: Verify all expected commits are on `main`**

```bash
cd D:/Claude/AIVideoTrans_Codex_web_mvp
git log origin/main..HEAD --oneline
```

Expected output should list (in this order, most recent first):
```
<sha> feat(marketing): wire <FeaturedDemos /> into homepage ...
<sha> feat(marketing): add FeaturedDemos carousel CSS ...
<sha> feat(marketing): add FeaturedDemos Server Component ...
<sha> feat(marketing): add FeaturedDemosClient shell ...
<sha> feat(marketing): add FeaturedDemoCard ...
<sha> feat(marketing): add FeaturedDemos playing-id context
<sha> feat(marketing): add featured-demos.json config ...
<sha> feat(marketing): add 5 hand-picked demo clip triplets ...
```

- [ ] **Step 2: Push to origin/main**

```bash
git push origin main
```

Expected: `<sha>..<sha> main -> main` with no errors.

- [ ] **Step 3: Build the deploy tarball**

```bash
rm -f D:/Claude/temp/next-deploy-demos.tar.gz
git archive HEAD frontend-next/ -o D:/Claude/temp/next-deploy-demos.tar.gz
ls -lh D:/Claude/temp/next-deploy-demos.tar.gz
```

Expected: tarball ~50-55 MB (existing 4.3 MB frontend + ~50 MB demo assets).

- [ ] **Step 4: Upload tarball to production /tmp**

```bash
PYTHONIOENCODING=utf-8 python -c "
import paramiko, socks
sock = socks.socksocket()
sock.set_proxy(socks.SOCKS5, '127.0.0.1', 11080, rdns=True)
sock.settimeout(20)
sock.connect(('5.78.122.220', 22))
sock.settimeout(None)
key = paramiko.Ed25519Key.from_private_key_file('C:/Users/Administrator/.ssh/id_ed25519')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname='5.78.122.220', port=22, username='root', pkey=key, sock=sock,
               timeout=20, banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=False)
sftp = paramiko.SFTPClient.from_transport(client.get_transport())
sftp.put('D:/Claude/temp/next-deploy-demos.tar.gz', '/tmp/next-deploy-demos.tar.gz', confirm=True)
print('size:', sftp.stat('/tmp/next-deploy-demos.tar.gz').st_size)
sftp.close(); client.close()
"
```

Expected: `size: 50000000` to `55000000`.

- [ ] **Step 5: Extract + start no-cache rebuild in background**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "set -e; cd /opt/aivideotrans && tar xzf /tmp/next-deploy-demos.tar.gz && LOG=/tmp/next-build-demos-\$(date +%s).log && echo \$LOG > /tmp/next-current-build.log && nohup bash -c 'cd /opt/aivideotrans && docker compose --env-file ./config/.env build --no-cache next 2>&1 && docker compose --env-file ./config/.env up -d --force-recreate next 2>&1' > \$LOG 2>&1 < /dev/null & echo PID=\$!; cat /tmp/next-current-build.log"
```

The SSH call will likely time out at 20s (paramiko default) — that's expected; the nohup'd build keeps running. Check by polling.

- [ ] **Step 6: Poll build status until done**

```bash
for i in {1..20}; do
  sleep 30
  res=$(PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "pgrep -f 'docker compose.*build --no-cache next' >/dev/null && echo BUILDING || echo DONE" 2>&1 | tail -1)
  echo "t+$((i*30))s: $res"
  [ "$res" = "DONE" ] && break
done
```

Expected: `DONE` within 9-12 minutes (no-cache rebuild from scratch).

- [ ] **Step 7: Verify container is healthy with the new image**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "docker ps --filter name=aivideotrans-next --format '{{.Status}} ({{.CreatedAt}})'; echo ---; docker exec aivideotrans-next sh -c 'ls /app/public/marketing/demos/karpathy-agent-engineering/ 2>&1'"
```

Expected:
- Container `Up <N> seconds (healthy)`, recent CreatedAt
- The container's `/app/public/marketing/demos/karpathy-agent-engineering/` lists `original.mp4`, `dubbed.mp4`, `poster.jpg`

- [ ] **Step 8: Cleanup tarball on remote**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "rm -f /tmp/next-deploy-demos.tar.gz"
```

---

### Task D4: Production verification

- [ ] **Step 1: Verify the section is in the live HTML**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "curl -s https://aitrans.video/ | grep -o '真实成片样例\|featured-demos\|听一段实际配音' | sort -u"
```

Expected: 3 lines:
```
featured-demos
听一段实际配音
真实成片样例
```

- [ ] **Step 2: Verify all 5 poster.jpg files return 200**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "for slug in karpathy-agent-engineering muniba-disability-vs-ability buffett-2008-buy-stocks vox-why-ok jensen-matrix-multiplication; do curl -s -o /dev/null -w \"\$slug poster: %{http_code} sz=%{size_download}\n\" https://aitrans.video/marketing/demos/\$slug/poster.jpg; done"
```

Expected: all 5 lines `200` with `sz=` between 80000 and 200000.

- [ ] **Step 3: Verify all 10 mp4 files return 200**

```bash
PYTHONIOENCODING=utf-8 python D:/daili/scripts/ssh_over_socks_command.py 127.0.0.1 11080 5.78.122.220 22 root C:/Users/Administrator/.ssh/id_ed25519 "for slug in karpathy-agent-engineering muniba-disability-vs-ability buffett-2008-buy-stocks vox-why-ok jensen-matrix-multiplication; do for kind in original dubbed; do curl -sI -o /dev/null -w \"\$slug \$kind: %{http_code}\n\" https://aitrans.video/marketing/demos/\$slug/\$kind.mp4; done; done"
```

Expected: all 10 lines report `200`.

- [ ] **Step 4: Browser sanity check** at https://aitrans.video/#featured-demos:
  - Section appears between ProductProof and WorkflowShowcase
  - Posters load with title overlays
  - Auto-scroll runs (track moves right-to-left)
  - Hover any card → that card scales up, others shrink, scroll pauses
  - Click play → video plays
  - Click play on another card → first pauses, second plays
  - Toggle EN/CN tab → swap works, currentTime preserved
  - On mobile devtools viewport (375px) → no auto-scroll, manual swipe works
  - Console: no errors

- [ ] **Step 5: Reduced-motion check** in Chrome DevTools:
  - Open DevTools → ⋮ → More tools → Rendering → "Emulate CSS media feature prefers-reduced-motion: reduce"
  - Reload page
  - Auto-scroll should stop, track sits static
  - Hover scale should still work

- [ ] **Step 6: Final sanity** — open the live page in an incognito window, hard refresh (Ctrl+Shift+R), confirm everything still works without browser cache.

---

## Acceptance criteria (from spec §14)

The plan author should be able to mark this complete when:

- [ ] All 5 demo asset triplets exist under `frontend-next/public/marketing/demos/<slug>/` and committed to git
- [ ] `featured-demos.json` parses successfully and lists all 5 demos
- [ ] All 4 new components type-check cleanly with `tsc --noEmit`
- [ ] No ESLint errors when running `next lint --dir frontend-next/src/components/marketing`
- [ ] Local dev preview shows the section with auto-scroll, hover-shrink, click-to-play, tab swap, and pause-others all working
- [ ] Production rebuild deployed via `docker compose build --no-cache next` (NOT plain `build`)
- [ ] Live https://aitrans.video/#featured-demos shows the section with all 5 demos, all assets loading 200
- [ ] Reduced-motion emulation disables auto-scroll cleanly
- [ ] No console errors on any of the above flows

---

## Risks + tripwires (from spec §13, restated for the executor)

1. **BuildKit cache poisoning observed earlier this session.** ALWAYS deploy this with `--no-cache`. Plain `docker compose build next` may produce a stale image even when the host filesystem has the latest code. We saw this happen 3+ times across previous deploys. The `--no-cache` rebuild takes ~9-12 min vs ~30s for a cache-hit; pay the cost.

2. **`<video preload="none">` is non-negotiable.** Without it, 10 video elements (5 primary + 5 duplicate) would race to download MP4s on first paint, blowing out LCP and bandwidth. Always set `preload="none"` on the `<video>` element. Posters carry the visual weight until the user clicks.

3. **Font discovery in the container is the most likely Phase A failure point.** If the overlay text shows tofu boxes (□□□), the font path was wrong. Default Debian/Ubuntu base images include CJK fonts at `/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc` and `NotoSansCJK-Regular.ttc` — verify in Task A1 Step 3.

4. **Container `/opt/aivideotrans/app/projects/` ≠ host path.** The job records store the container path. The container has a bind mount from `/opt/aivideotrans/data/projects` (host) → `/opt/aivideotrans/app/projects` (container), so when running ffmpeg INSIDE the container use the `/opt/aivideotrans/app/...` path; when running on the host use `/opt/aivideotrans/data/...`.

5. **`-ss BEFORE -i` is fast but not frame-accurate.** For the two video clips we use output-seek (`-i BEFORE -ss`) explicitly to ensure the EN and CN clips of the same demo land on the exact same frame. Don't optimize this back to input-seek "for speed" — the tab-swap currentTime restore relies on frame alignment.

---

## Plan summary by phase

| Phase | Tasks | Output | Time estimate |
|---|---|---|---|
| A | A1 preflight, A2-A6 ffmpeg per demo, A7 PIL overlays, A8 transfer | 5 asset triplets committed | 30-60 min (mostly ffmpeg encode) |
| B | B1 JSON config | featured-demos.json committed | 5 min |
| C | C1 context, C2 card, C3 client shell, C4 server component, C5 CSS, C6 wire | 4 new tsx files + globals.css updated + page wired, all committed | 60-90 min |
| D | D1-D2 local verify, D3 deploy with --no-cache, D4 prod verify | Live on aitrans.video | 30 min (15 min wall-clock for no-cache rebuild) |

Total wall-clock estimate: ~3 hours including ffmpeg encode time and deploy waits. Code work is ~2 hours.
