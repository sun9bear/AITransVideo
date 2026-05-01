# Marketing Homepage — Featured Demos Section (Phase 1)

**Date:** 2026-05-01
**Status:** Design approved by project owner; ready for implementation planning
**Scope:** Phase 1 only (frontend module + manually generated assets). Phase 2 admin tooling is deferred to a separate effort and explicitly out of scope here.

---

## 1. Goal

Add a horizontally-scrolling demo carousel to the marketing homepage that plays real, side-by-side English-source / Chinese-dubbed clips from completed Studio jobs. The aim is to give first-time visitors **product proof at the actual output level** — they hear what the dubbed content sounds like, not just see screenshots of the workspace.

The carousel sits between **`<ProductProof>`** and **`<WorkflowShowcase>`** so the narrative reads:

```
... ProductProof (workspace screenshots)  →  FeaturedDemos (real output clips)  →  WorkflowShowcase (4-step process) ...
```

ProductProof proves the workbench is real; FeaturedDemos proves the output is real. They reinforce each other.

---

## 2. Out of scope (Phase 2, separate effort)

Captured here so future readers don't think these are missing:

- Admin UI for selecting jobs, scrubbing for clip windows, and triggering automated extraction
- Backend `/admin/api/featured-demos` CRUD endpoints
- DB table `featured_demos`
- ffmpeg / PIL extraction worker
- Public `/api/featured-demos` API endpoint
- R2 storage of demo assets

Phase 1 ships static assets baked into the Next.js public/ directory + a static JSON config. Updating the demo set in Phase 1 = manual ops (rebuild image). Phase 2 will replace the JSON with a runtime API and add admin tooling once Phase 1 UX is validated on production.

---

## 3. The 5 demo segments (frozen for Phase 1)

All 5 segments come from real Studio jobs in the project owner's account. DSP `dsp_speed_ratio_used` (or `dsp_speed_param`) verified for every constituent segment to be within 0.85–1.20 — meaning no audible time-stretch artifacts. Single-speaker dominance ≥85% within each window so the dub sounds coherent rather than choppy.

| # | Slug (used in paths + JSON id) | Display title (zh) | Source job_id | Time range | Duration | Primary speaker | DSP ratios |
|---|---|---|---|---|---|---|---|
| 1 | `karpathy-agent-engineering` | 安德烈·卡帕西谈智能体工程 | `cd545d7b1325439182a7db40ea1e2d8d` | 985.7 – 1038.2 s (16:25 – 17:18) | 52.5 s | 安德烈·卡帕西 | 1.000 |
| 2 | `muniba-disability-vs-ability` | 穆尼巴·马扎里：拥抱不完美 | `524c5253ad514692bcc06aa170f9567f` | 216.9 – 278.4 s (03:36 – 04:38) | 61.5 s | 穆尼巴·马扎里 | 1.000 / 1.126 / 1.000 |
| 3 | `buffett-2008-buy-stocks` | CNBC 专访巴菲特：2008 年与价值投资 | `f08cabc1267642b98a9d774a9e2a5da4` | 577.1 – 646.1 s (09:37 – 10:46) | 69.0 s | 沃伦·巴菲特 + 主持人 | 1.000 / 1.000 / 1.000 / 0.944 |
| 4 | `vox-why-ok` | 为什么我们总说 "OK" | `8463bb721d1c4ad697d2bc0f317c8cdf` | 185.9 – 244.9 s (03:05 – 04:04) | 59.0 s | 旁白 | 1.000 |
| 5 | `jensen-matrix-multiplication` | 黄仁勋专访：英伟达的护城河 | `3066774da2a64848b2f4d1d2824e022b` | 1261.3 – 1319.9 s (21:01 – 21:59) | 58.6 s | 黄仁勋 | 1.000 |

Segment selection rationale (one-liner each):
1. Karpathy literally explains the term "智能体工程" — matches the video's Chinese title exactly.
2. Muniba's most-quoted line about disability-as-ability inversion; spans segs 8–10.
3. Hosts asks about Munger's "trouble" prediction → Buffett invokes his 2008 NYT op-ed → host follow-up → Buffett on value investing. Q&A format matches the screenshot title's 2008 framing.
4. Vox narrator's core etymological claim about why "K" is a visually/auditorily distinctive letter in English.
5. Jensen's "矩阵乘法是基石但不是全部" framing — directly answers the title's "护城河" question.

---

## 4. Component architecture

### 4.1 File layout

```
frontend-next/src/components/marketing/
├ featured-demos.tsx               (new — Server Component, reads JSON)
├ featured-demos-client.tsx        (new — "use client" shell, owns context + renders cards)
├ featured-demo-card.tsx           (new — "use client", single card with EN/CN tab + video)
└ featured-demos-context.tsx       (new — "use client", currently-playing-id context for pause-others)

frontend-next/public/marketing/demos/
├ featured-demos.json              (new — static config, schema in §6)
├ karpathy-agent-engineering/
│  ├ original.mp4                   (English source clip, 720p H.264 + AAC)
│  ├ dubbed.mp4                     (Chinese dub clip, same time range)
│  └ poster.jpg                     (1280×720, frame at clip midpoint, with Chinese title overlay)
├ muniba-disability-vs-ability/    (same 3 files)
├ buffett-2008-buy-stocks/         (same 3 files)
├ vox-why-ok/                      (same 3 files)
└ jensen-matrix-multiplication/    (same 3 files)
```

### 4.2 Wiring

`src/app/(marketing)/page.tsx` adds:

```tsx
import { FeaturedDemos } from "@/components/marketing/featured-demos"
// ...
<Hero />
<PainPoints />
<ProductProof />
<FeaturedDemos />          {/* new */}
<WorkflowShowcase />
// ... rest unchanged
```

### 4.3 Server-side data fetch + client boundary

The component graph splits on the server/client boundary because the auto-scroll, hover scaling, video element, tab state, and pause-others context all need to run in the browser, but the JSON read must run on the server (filesystem at build time, no client-side network round-trip).

```
<FeaturedDemos />                   ← Server Component
   reads featured-demos.json
   passes parsed { demos } as props →

      <FeaturedDemosClient demos={...} />   ← "use client"
         owns FeaturedDemosContext (currentlyPlayingId state)
         renders the carousel track + .demo-card * 2 (duplicated for loop)

            <FeaturedDemoCard demo={...} />   ← "use client"
               local useState for active tab (zh / en)
               <video ref> registers with context onPlay
```

`<FeaturedDemos>` itself contains zero React state. It performs a static `import demosJson from "../../../public/marketing/demos/featured-demos.json"` (small file, build-time inlined). It validates the parse result against the JSON schema in §6 and:

| Condition | Behavior |
|---|---|
| File missing | Build-time module-resolution error. Build fails loudly. (Treated as a deploy bug.) |
| File present but JSON malformed | Build-time JSON parse error. Build fails loudly. |
| File parses but `demos` array is empty or malformed | At runtime, `<FeaturedDemos>` returns `null`. The section silently disappears from the page. No placeholder, no error UI. |

`<FeaturedDemosClient>` and below carry the `"use client"` directive. The Server-Component → Client-Component handoff passes the parsed `demos` array as a serialisable prop.

Phase 2 swap path: the static import in `<FeaturedDemos>` becomes a server-side `fetch("/api/featured-demos")` call returning the same JSON shape. `<FeaturedDemosClient>` and the card hierarchy stay untouched.

---

## 5. Interaction model

### 5.1 Default (idle) state — desktop only

- Cards sit on a single horizontal track.
- The track auto-scrolls right-to-left at constant speed.
- One full loop = **35 seconds** for a 5-card set. Tweakable as a single CSS variable; not finalised at build time, but 35 s is the chosen default.
- Implemented via `@keyframes` on `transform: translateX(...)`. The card list is rendered **twice in DOM** (`[...demos, ...demos]`) so the keyframe can animate from `translateX(0)` to `translateX(-50%)` and the loop point lines up with an identical second copy — visually seamless.
- Each card is `aria-hidden="true"` on its second (duplicate) copy so screen readers and keyboard nav don't see ten cards.

### 5.2 Hover state — desktop only

When the cursor enters any card:

- The track's animation freezes via `animation-play-state: paused`.
- The hovered card scales to **1.15** with a deepened shadow and `z-index: 10`.
- All other cards in the track scale to **0.92** with `opacity: 0.55`.

Implemented purely in CSS using `:has()`:

```css
.demo-track:has(.demo-card:hover) .demo-card { scale: 0.92; opacity: 0.55 }
.demo-track:has(.demo-card:hover) .demo-card:hover { scale: 1.15; opacity: 1; z-index: 10 }
.demo-track:hover { animation-play-state: paused }
```

No JS state required for the visual effect. Transitions: 200 ms ease-out on `scale`, `opacity`, `box-shadow`.

**Browser baseline**: `:has()` shipped in Chrome 105 (Aug 2022), Safari 15.4 (Mar 2022), Firefox 121 (Dec 2023). The marketing site's supported-browser baseline already aligns with these — every other section in the existing layout uses Tailwind v4 + modern CSS that requires this baseline or newer. No `:has()` polyfill or JS fallback is shipped; on older browsers (Firefox <121) the hover-shrink-others effect simply doesn't apply (the hovered card still scales up; siblings just don't shrink). That graceful degradation is acceptable — the carousel remains fully functional, just less expressive.

### 5.3 Click-to-play + pause-others

Each card embeds a native `<video>` element with `controls preload="none"`. Poster image is set on the video element so the static frame is what visitors see initially; the video data is only fetched when the user clicks play.

When a video starts playing:

1. Card's `onPlay` handler dispatches the card's `id` to `<FeaturedDemosContext>`.
2. Context updates `currentlyPlayingId` state.
3. All other cards subscribe; if their `id !== currentlyPlayingId` and their video is currently playing, they call `videoRef.current.pause()`.

This achieves "starting a new card pauses the previous one" without polling. The track's auto-scroll continues to be paused as long as the cursor is anywhere over it.

### 5.4 Tab switching (EN / CN) inside a card

Each card has two top-row tabs labelled **「中文配音版」** (default, cinnabar) and **「英文原片」** (cinnabar-soft outline). Switching:

1. Pause the current video.
2. Capture `currentTime`.
3. Swap the `<video>` element's `src` to the other clip.
4. Set `currentTime` to the captured value (both clips share the exact same time range so the position is meaningful).
5. Leave it paused — user resumes manually.

The tab state is local to each card (`useState`), not in context.

### 5.5 Mobile / touch (`@media (hover: none)`)

- Auto-scroll **off**.
- Hover scale **off**.
- Track becomes a `scroll-snap-type: x mandatory` container with `scroll-snap-align: start` on each card. User swipes manually.
- Single-card width on mobile: `min(85vw, 320px)` so a small slice of the next card peeks in to signal scrollability.
- Tab switching, click-to-play, pause-others all behave the same as desktop.

### 5.6 Accessibility & reduced motion

- `@media (prefers-reduced-motion: reduce)` disables the auto-scroll keyframe; track sits static showing the first 5 cards (with overflow-x scroll still available).
- The hover scale animation is preserved (it's a focus tool, not a movement that triggers vestibular issues).
- Each card has a focusable `<video>` element + a `<button>` for each tab, all keyboard-reachable.
- Tabs use `role="tab"` and the video/poster wrapper uses `role="tabpanel"`.

---

## 6. JSON schema (forward-compatible with Phase 2 API)

`public/marketing/demos/featured-demos.json`:

```jsonc
{
  "version": 1,
  "demos": [
    {
      "id": "karpathy-agent-engineering",
      "display_name": "安德烈·卡帕西谈智能体工程",
      "source_label": "YouTube · 96jN2OCOfLs · AI Ascent 2025",
      "segment_label": "16:25 – 17:18 · 52s",
      "original_src": "/marketing/demos/karpathy-agent-engineering/original.mp4",
      "dubbed_src":   "/marketing/demos/karpathy-agent-engineering/dubbed.mp4",
      "poster_src":   "/marketing/demos/karpathy-agent-engineering/poster.jpg",
      "natural_width": 1280,
      "natural_height": 720
    }
    // ...4 more entries with same shape
  ]
}
```

Field semantics:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Stable slug, also used as DOM key + URL hash anchor. ASCII lowercase + hyphens. |
| `display_name` | yes | Chinese title shown in poster overlay and screen-reader label. |
| `source_label` | yes | Public attribution line shown under the card. Format guideline: `<platform> · <video-id-or-source> · <event-or-channel>`. |
| `segment_label` | yes | Human-readable time range + duration shown alongside source label. |
| `original_src` / `dubbed_src` | yes | Public URLs to the two MP4 clips. Phase 1: `/marketing/demos/<slug>/original.mp4`. Phase 2: may be R2 presigned or CDN. |
| `poster_src` | yes | Public URL to the JPG poster. |
| `natural_width` / `natural_height` | yes | Encoded dimensions of the MP4. Used to set `<video>` aspect ratio so the layout reserves space before media loads (no CLS). |

The schema is intentionally a list of self-contained card descriptors — no nested "original/dubbed pair" object — so Phase 2's API can return the same shape unchanged, and the React component doesn't care whether the list comes from a static JSON or from a runtime fetch.

---

## 7. Asset generation pipeline (Phase 1, manual)

All 5 demos are extracted via the same shell loop, run on the production host via `docker exec` into the `aivideotrans-app` container (which already has ffmpeg installed):

```bash
# Per-demo extraction template
JOB_DIR=/opt/aivideotrans/app/projects/<workspace>/<job_id>
START=985.7         # demo start in source seconds
DUR=52.5            # demo duration in seconds
MID=$(echo "$START + $DUR/2" | bc -l)
SLUG=karpathy-agent-engineering
OUT=/tmp/demos/$SLUG
mkdir -p $OUT

# Frame-accurate output-seek used for the two video clips. Input-seek
# (`-ss before -i`) is faster but only seeks to the nearest keyframe, which
# can cause a few-frames drift between original.mp4 and dubbed.mp4 — and
# that drift is visible when a user toggles the EN/CN tab and resumes from
# the same currentTime. Output-seek (`-i before -ss`) re-decodes from the
# start until $START, so both clips land on the exact same frame.
# `-accurate_seek` is the default for output-seek; we set it explicitly
# for clarity. The poster grab (single frame) can stay on input-seek.

# English clip — frame-accurate
docker exec aivideotrans-app ffmpeg -y \
  -i $JOB_DIR/video/original.mp4 -ss $START -t $DUR -accurate_seek \
  -vf scale=-2:720 -c:v libx264 -preset slow -crf 23 \
  -c:a aac -b:a 128k -ac 2 -ar 44100 \
  -movflags +faststart \
  $OUT/original.mp4

# Chinese dub clip — frame-accurate, same time range
docker exec aivideotrans-app ffmpeg -y \
  -i $JOB_DIR/publish/dubbed_video.mp4 -ss $START -t $DUR -accurate_seek \
  -vf scale=-2:720 -c:v libx264 -preset slow -crf 23 \
  -c:a aac -b:a 128k -ac 2 -ar 44100 \
  -movflags +faststart \
  $OUT/dubbed.mp4

# Poster — single frame at clip midpoint. Input-seek is fine; one-frame
# output, sub-second drift doesn't matter for a still image.
docker exec aivideotrans-app ffmpeg -y \
  -ss $MID -i $JOB_DIR/video/original.mp4 -frames:v 1 \
  -vf scale=-2:720 \
  $OUT/poster-raw.jpg
```

The resulting `poster-raw.jpg` then gets the Chinese title overlay added with PIL inside the `aivideotrans-app` container (which already has Pillow installed). Running it on the host instead would require installing Pillow + CJK fonts on the host first, which is unnecessary churn — the container is the canonical place.

**Font resolution**: the plan author runs the overlay inside the container and **must verify which CJK fonts are actually present** before fixing on a font path. Likely candidates in a Debian/Ubuntu base image: `/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc`, `/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc`. If the container image does not ship CJK fonts, plan author either (a) `apt-get install fonts-noto-cjk` inside the running container before generating posters, or (b) bundles the font files into the project under `frontend-next/scripts/fonts/` and mounts them. Option (a) is simpler for a one-off run; option (b) is required if the asset pipeline becomes recurrent in Phase 2.

```python
# Poster overlay — font_title_path / font_sub_path resolved per the note above
from PIL import Image, ImageDraw, ImageFont
img = Image.open("poster-raw.jpg")
draw = ImageDraw.Draw(img)
font_title = ImageFont.truetype(font_title_path, 42)
font_sub = ImageFont.truetype(font_sub_path, 22)

# Title in lower-left, with cinnabar accent strip
draw.rectangle([(64, 580), (68, 660)], fill="#C73E3A")
draw.text((86, 588), "安德烈·卡帕西谈智能体工程", font=font_title, fill="white")
draw.text((86, 638), "16:25 – 17:18 · 52s", font=font_sub, fill=(255, 255, 255, 200))

img.save("poster.jpg", quality=85, optimize=True, progressive=True)
```

Approximate output sizes per demo: ~3–5 MB each MP4, ~80–150 KB poster JPG. Five demos × 2 MP4s + 1 poster = ~33–53 MB total assets — comfortably small for the Next.js public/ directory.

The exact extraction commands for all 5 demos, including the SLUG / time range substitutions, will be inlined in the implementation plan (writing-plans step) so the plan author can audit them line-by-line.

---

## 8. Empty state & failure modes

| Condition | Behavior |
|---|---|
| `featured-demos.json` missing | Build-time module-resolution error. Build fails loudly. Treated as a deploy bug, not a runtime concern. |
| `featured-demos.json` malformed JSON | Build-time JSON parse error. Build fails loudly. Same class as above — caught before deploy. |
| `featured-demos.json` parses but `demos` list is empty / not an array | At runtime, `<FeaturedDemos>` returns `null`. Section disappears from the page, no placeholder, no error UI. |
| `featured-demos.json` parses but a particular demo's MP4 is 404 | Browser shows broken video (native `<video>` failure). Other cards continue working. We accept this — Phase 2's admin tooling will validate asset existence at write time. |
| User has Reduced Motion enabled | Track is static; cards still hoverable; manual horizontal scroll still works. Nothing is hidden. |
| User on touch device | No auto-scroll, no hover scaling. Manual swipe-snap. Tap any card → starts video. |

---

## 9. Visual styling tokens

Reuses the ink theme already established for marketing surfaces:

- Card background: `var(--card)` (rice paper)
- Card border: `1px solid var(--border)`
- Card shadow (default): `0 1px 3px rgba(0,0,0,0.08)`
- Card shadow (hovered): `0 20px 60px -10px rgba(0,0,0,0.30)`
- Tab active: `bg-[var(--cinnabar)] text-white`
- Tab inactive: `bg-[var(--cinnabar-soft)] text-[var(--cinnabar)] border border-[var(--cinnabar)]/40`
- Section heading: `ink-display` class, mirrors PainPoints / WorkflowShowcase typography.
- Eyebrow label "真实成片样例" with cinnabar accent strip — consistent with the eyebrow style used on every other marketing section.

---

## 10. Section copy

Eyebrow: **真实成片样例**

Heading (h2, ink-display): **听一段实际配音，比看十张截图更有说服力**

Sub-paragraph (zh-body, muted-foreground): 下面 5 段都是已完成的真实任务片段，每张卡片可在「中文配音版」和「英文原片」之间切换。鼠标悬停可放大查看；触屏可左右滑动浏览。

---

## 11. Dependencies on other systems

None. Phase 1 is entirely self-contained inside `frontend-next/`. No backend changes, no DB changes, no API additions, no admin work. The only external touchpoint is the docker-exec'd ffmpeg + PIL run on the production host to extract the 5 sets of assets — that's a one-off shell session, not a deploy-coupled step.

---

## 12. Verification plan (what counts as "Phase 1 done")

1. **Build green**: `npx next build` completes locally without errors after the new component is added.
2. **TypeScript clean**: `npx tsc --noEmit` passes.
3. **All 5 demo asset triplets present**: 5 MP4 pairs + 5 JPGs in `public/marketing/demos/<slug>/`. Each MP4 plays standalone in a browser (sanity check).
4. **Live page check on prod (https://aitrans.video/)**:
   - The new section appears between ProductProof and WorkflowShowcase.
   - All 5 posters load (HTTP 200).
   - Auto-scroll is animating on desktop with no visible loop seam.
   - Hover on any card pauses the track, scales the card up, scales siblings down + dims them.
   - Click play → video plays. Click another card's play → previous video pauses.
   - Tab switch within a card pauses + swaps src + preserves currentTime.
   - On a mobile viewport (DevTools): no auto-scroll, no hover effects, swipe works, tap-play works.
5. **Reduced motion check**: With `prefers-reduced-motion: reduce` set in DevTools, auto-scroll is disabled but the section is fully usable.
6. **No console errors** on any of the above flows.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Buildkit cache poisoning (observed earlier this session — old image content shipped in spite of fresh files) | Always deploy with `docker compose build --no-cache next` for the Phase 1 ship-out. Spec authors and CodeX should both treat this as the default for any frontend-touching change until the underlying buildkit issue is understood. |
| LCP regression from auto-loading 5 MP4s | `preload="none"` on every `<video>` plus posters served as JPG/WebP-fallback ensures the initial load is image-only (~600 KB total for 5 posters). Videos only fetched on click. |
| 5 simultaneous keyframe animations on the duplicated cards lead to layout jank | Keyframe animates a single `transform: translateX` on the parent track only — composited, GPU-accelerated, single rendering layer. The 10 child cards do not individually animate. |
| Future expansion to 7 / 8+ demos breaks the 35s loop tuning (cards visible too briefly) | Loop duration is a single CSS variable. Plan author should make this trivially adjustable, not magic-numbered into the keyframe. |
| Authorisation / IP for source videos | All 5 videos are publicly available on YouTube and are interview / lecture / explainer content from major channels. Source attribution shown on every card. If any source channel objects, removal is a one-line edit to the JSON. |

---

## 14. Acceptance summary

When this spec moves into the writing-plans skill, the implementation plan must produce, in this order:

1. Backend-host shell session that extracts and uploads all 5 demo asset triplets to `frontend-next/public/marketing/demos/<slug>/`.
2. `featured-demos.json` populated with all 5 demos using the exact metadata in §3.
3. `<FeaturedDemos>`, `<FeaturedDemoCard>`, and the playing-id Context, fully wired into the homepage between ProductProof and WorkflowShowcase.
4. CSS for auto-scroll keyframe, hover-scale-others-shrink with `:has()`, and reduced-motion fallback added to `globals.css`.
5. Type checks + lint pass.
6. Local dev preview verified for all the items in §12.
7. Commit + push to main.
8. Production deploy via `docker compose build --no-cache next` + `up -d --force-recreate next`.
9. Post-deploy verification of all items in §12 against the live site.
