"use client"

/**
 * "音色修改" Tab — 镜像主流程 VoiceSelectionPanel 的交互方式，
 * 只把提交动作从 review-gate 的 `approveVoiceSelection` 换成
 * 写入 editing 期的 `voice_map`。
 *
 * 相同部分（来自 plan §7.3 "复用 VoiceSelectionPanel 的 UI + 后端
 * 音色查询 API"）：
 *
 * - 从 `/jobs/{id}/review-state` 加载 voice_selection_review 阶段
 *   payload（speakers / all_providers / default_provider 等）
 * - 三引擎 Tab 切换（MiniMax / CosyVoice / 豆包）
 * - 音色下拉 optgroups：🎯 智能推荐 · 我的音色（仅 MiniMax）·
 *   女声/男声/其他（全量目录）
 * - 智能推荐行附 `★ 自动匹配` / `#N 推荐` 前缀
 * - 下拉选项带 `· X.X 字/秒(快/中/慢)` 语速档次（Phase 1 校准）
 * - 试听按钮（`POST /jobs/{id}/review/voice/preview` 取 base64 wav）
 * - 克隆音色按钮（复用 VoiceCloneModal，选段 → `/voice-clone`，
 *   付费 API 仍是用户显式点击触发，符合 CLAUDE.md 约束）
 * - 过期音色 banner
 *
 * 不同部分（editing 专属）：
 *
 * - 不存在全局"确认音色选择"按钮。每说话人有独立的"应用到此
 *   说话人"按钮，按一次就把这个说话人所有段的 voice_map 批量
 *   写成选定音色。这是因为编辑态不是一次性 gate，而是增量改。
 * - 新增"恢复原音色"按钮：有 override 时显示，清除该说话人
 *   所有段的 override。
 * - 音色变更 **不会立即触发重合成**——只写 voice_map。页面底
 *   部提示用户需要到"翻译修改"Tab 手动重合成。
 * - 状态 label 体现"覆盖"而非"已选择"：没有覆盖（baseline）/
 *   已覆盖 X/Y 段 / 已覆盖全部段。
 *
 * 付费 API 约束（CLAUDE.md）：
 * - 克隆流程走 `/voice-clone`，仍然由用户显式点击 + modal 确认
 *   触发。editing Tab 不降低任何信号（不自动克隆、不默认建议、
 *   按钮也不隐身）。
 * - 试听是常规 TTS 合成，按原价复用。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { apiClient } from "@/lib/api/client"
import {
  clearVoiceOverride,
  setVoiceOverride,
  type EditingSegment,
  type EditingSpeaker,
  type VoiceMapEntry,
} from "@/lib/api/editing"
import { EditPageSpeakerProfileBadge } from "@/components/workspace/EditPageSpeakerProfileBadge"
import { getErrorMessage } from "@/lib/api/errors"
import {
  deleteUserVoice,
  getUserVoices,
  getVoiceCandidates,
  getVoiceSelectionPricing,
  previewVoice,
  type UserVoiceEntry,
  type VoiceCandidate,
  type VoiceCandidatesResponse,
  type VoiceMatchScope,
  type VoiceSelectionPricingResponse,
} from "@/lib/api/voiceSelection"
import { VoiceCloneModal, SpeakerAudioAuditModal } from "@/components/workspace/VoiceSelectionPanel"
// Phase 4.2 E.1 — CosyVoice clone wiring in editing-state tab. Mirror of
// VoiceSelectionPanel: button onClick splits by provider; MiniMax stays in
// the legacy VoiceCloneModal (re-exported from VoiceSelectionPanel),
// CosyVoice opens the dedicated D.2 modal.
import { CosyVoiceCloneModal } from "@/components/voice-clone/CosyVoiceCloneModal"
import {
  getCosyvoiceCloneGate,
  type CosyvoiceCloneGateResponse,
} from "@/lib/api/cosyvoiceClone"

/* ---------- Types (mirror VoiceSelectionPanel) ---------- */

interface SpeakerPayload {
  speakerId: string
  speakerName: string
  segmentCount: number
  totalDurationS: number
  canClone: boolean
  autoMatchedByProvider: Record<
    string,
    { voiceId: string; label: string; backups: { voiceId: string; label: string }[] } | null
  >
  autoMatchedVoice: { voiceId: string; label: string } | null
  probeTexts: Array<{ segmentId: number; sourceText: string; cnText: string }>
}

interface AvailableVoice {
  voiceId: string
  label: string
  gender: string
  provider: string
  charsPerSecond?: number | null
  speedCalibratedAt?: string | null
}

interface ProviderInfo {
  label: string
  availableVoices: AvailableVoice[]
  supportsClone: boolean
}

// Local per-speaker UI state (what's in the dropdowns RIGHT NOW before
// the user clicks "应用到此说话人"). On Tab mount this is seeded from
// voice_map; after that, user edits drive it until they apply.
interface SpeakerDraftState {
  voiceId: string
  selectedProvider: string
  voiceSource: "catalog" | "cloned" | "auto_matched"
  voiceReuse: boolean
  /** 主流程 VoiceSelectionPanel 同款：MiniMax 下的音质档位。
   *  目前和主流程一样是**纯展示**（approve payload / voice_map 都
   *  不带它，下游 TTS 不读），只影响右侧 "30 / 50 点/分钟" 文案。
   *  保留是为了 UX 一致——用户在主流程看到这两档，编辑态不应
   *  无端消失。未来如要真的分档计费，前后端一起扩 schema。*/
  minimaxModel: "turbo" | "hd"
}

interface VoiceModifyTabProps {
  jobId: string
  segments: EditingSegment[]
  voiceMap: Record<string, VoiceMapEntry>
  onVoiceMapChange: (next: Record<string, VoiceMapEntry>) => void
  /** Editing-mode speakers (baseline + user-added). Drives the per-card
   *  profile badge + lets the "新增说话人" entry surface brand-new
   *  speakers immediately. 2026-05-09 plan ``studio-editing-add-speaker``
   *  Task 8. */
  editingSpeakers: EditingSpeaker[]
  /** Open the parent's create-speaker dialog. */
  onRequestCreateSpeaker: () => void
  /** Click handler for the badge's "重试" button on failed profiles. */
  onRetryProfile: (speakerId: string) => void
}

const PROVIDER_TAB_ORDER = ["minimax", "cosyvoice", "volcengine"] as const
const PROVIDER_SHORT_LABELS: Record<string, string> = {
  minimax: "MiniMax",
  cosyvoice: "CosyVoice",
  volcengine: "豆包",
}

function formatVoiceOptionLabel(v: AvailableVoice): string {
  const base = v.label || v.voiceId
  const cps = v.charsPerSecond
  if (cps == null) return base
  let tier = "中"
  if (cps < 3.5) tier = "慢"
  else if (cps >= 4.5) tier = "快"
  return `${base} · ${cps.toFixed(1)}字/秒(${tier})`
}

function minimaxModelKey(model: "turbo" | "hd" | undefined): string {
  return model === "hd" ? "speech-2.8-hd" : "speech-2.8-turbo"
}

/** Phase 2 (plan 2026-05-17): short badge for personal-voice candidate
 *  match scope — same vocabulary as VoiceSelectionPanel. */
function matchScopeBadge(scope: VoiceMatchScope): string {
  switch (scope) {
    case "same_source_strong":
      return "★ 强匹配"
    case "same_source_named":
      return "● 同视频同名"
    case "same_source_speaker_id_changed":
      return "● 同视频"
    case "cross_source_named_person":
      return "○ 跨视频同名"
    default:
      return "○ 可能匹配"
  }
}

function formatCandidateSourceHint(candidate: VoiceCandidate): string {
  const title = candidate.evidence.sourceVideoTitle
  if (!title) return ""
  return ` · ${title}`
}

/* ---------- Main Component ---------- */

export function VoiceModifyTab({
  jobId,
  segments,
  voiceMap,
  onVoiceMapChange,
  editingSpeakers,
  onRequestCreateSpeaker,
  onRetryProfile,
}: VoiceModifyTabProps) {
  // speaker_id → EditingSpeaker (for profile badge lookup). Memoized
  // separately from segmentsBySpeaker so the badge doesn't recompute
  // on every segment update.
  const editingSpeakerById = useMemo(() => {
    const m = new Map<string, EditingSpeaker>()
    for (const sp of editingSpeakers) {
      if (sp.speaker_id) m.set(sp.speaker_id, sp)
    }
    return m
  }, [editingSpeakers])
  const [speakers, setSpeakers] = useState<SpeakerPayload[]>([])
  // 2026-05-09 v2: baseline review_state 的 speaker_review.payload.speaker_names
  // 是 commit-after-add-speaker 路径里**唯一**完整的 baseline speakers 索引
  // (voice_selection_review.payload.speakers 是 S2 阶段写的,后来 commit merge
  // 时漏 append; 详见 Task 9 deploy round 3 追修 commit fff26dc)。这里作为
  // displaySpeakers 的 fallback 来源——任何 baseline speaker_names 里有、
  // segments 里也有、但 voice_selection_review.speakers 里没有的 speaker_id
  // 都用一个最小 SpeakerPayload 渲染出来。
  const [baselineSpeakerNames, setBaselineSpeakerNames] = useState<Record<string, string>>({})
  const [providerMap, setProviderMap] = useState<Record<string, ProviderInfo>>({})
  const [fallbackVoices, setFallbackVoices] = useState<AvailableVoice[]>([])
  const [defaultProvider, setDefaultProvider] = useState("")
  const [hasMultiProvider, setHasMultiProvider] = useState(false)
  const [personalVoices, setPersonalVoices] = useState<UserVoiceEntry[]>([])
  const [expiredVoiceIds, setExpiredVoiceIds] = useState<string[]>([])
  const [pricing, setPricing] = useState<VoiceSelectionPricingResponse | null>(null)
  const [cloneCostCredits, setCloneCostCredits] = useState(0)
  const [draftStates, setDraftStates] = useState<Record<string, SpeakerDraftState>>({})
  // Phase 2 (plan 2026-05-17): per-speaker personal-voice candidates loaded
  // best-effort on mount. Strong matches preselect ONLY when no voice_map
  // override exists for this speaker — never clobber a user-saved override.
  const [voiceCandidates, setVoiceCandidates] = useState<Record<string, VoiceCandidatesResponse>>({})
  const [applyingSpeakerIds, setApplyingSpeakerIds] = useState<Set<string>>(new Set())
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({})
  const [previewError, setPreviewError] = useState<Record<string, string | null>>({})
  const [cloneModalSpeaker, setCloneModalSpeaker] = useState<string | null>(null)
  // Phase 4.2 E.1: parallel state for CosyVoice clone. Mirrors
  // VoiceSelectionPanel — disjoint from cloneModalSpeaker so only one
  // modal is mounted at a time. Provider-aware dispatch in the clone
  // button onClick below.
  const [cosyvoiceCloneModalSpeaker, setCosyvoiceCloneModalSpeaker] =
    useState<string | null>(null)
  const [cosyvoiceCloneGate, setCosyvoiceCloneGate] =
    useState<CosyvoiceCloneGateResponse | null>(null)
  // 2026-05-09: 核对原音弹窗 — readOnly 模式 (editing 状态后端的 reassign /
  // keep-original 端点 require voice_selection_review 阶段未 approved,
  // editing 状态会 409,所以这里只播放,不暴露 mutation 控件)。
  const [auditModalSpeaker, setAuditModalSpeaker] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const previewAudioRef = useRef<HTMLAudioElement | null>(null)

  // ---- Group segments by speaker_id (mirror voice_map's per-segment model) ----
  const segmentsBySpeaker = useMemo(() => {
    const map = new Map<string, EditingSegment[]>()
    for (const seg of segments) {
      const sid = seg.speaker_id || "(未标注)"
      const list = map.get(sid) ?? []
      list.push(seg)
      map.set(sid, list)
    }
    return map
  }, [segments])

  // 2026-05-09 fix: editing-mode speakers (新增的 speaker_c 等) 在
  // editing/speakers.json 里, 不在 voice_selection_review.payload.speakers
  // 里. 直接 render `speakers` 会让用户在音色修改 Tab 看不到新加的说话人.
  // 合并: baseline speakers + editing 端注册但 baseline 没有的 speakers,
  // 后者的 segmentCount/totalDurationS 现场从 segments 算.
  //
  // v2 (round 3): 上次 commit 的 editing-added speakers 已经写到 baseline
  // speaker_review.payload.speaker_names + voice_profiles, 但**没**追加到
  // voice_selection_review.payload.speakers (Task 9 merge 漏的)。重进编辑
  // 后,editingSpeakers 是空 (editing/speakers.json 已删),speakers state
  // 也是空 (voice_selection_review 没 append)。需要从 baselineSpeakerNames
  // 兜底,让用户能在音色 Tab 看到 commit 后的新 speaker。
  const displaySpeakers = useMemo<SpeakerPayload[]>(() => {
    const synthFromName = (speakerId: string, displayName: string): SpeakerPayload => {
      const segs = segmentsBySpeaker.get(speakerId) ?? []
      const totalMs = segs.reduce((acc, s) => {
        const dur = Number(s.end_ms ?? 0) - Number(s.start_ms ?? 0)
        return acc + (dur > 0 ? dur : 0)
      }, 0)
      return {
        speakerId,
        speakerName: displayName,
        segmentCount: segs.length,
        totalDurationS: totalMs / 1000,
        canClone: true,
        autoMatchedByProvider: {},
        autoMatchedVoice: null,
        probeTexts: [],
      }
    }

    const baselineIds = new Set(speakers.map((s) => s.speakerId))
    const editingExtras: SpeakerPayload[] = (editingSpeakers ?? [])
      .filter((es) => es.source === "editing" && !baselineIds.has(es.speaker_id))
      .map((es) => synthFromName(es.speaker_id, es.display_name))

    // v2 fallback: baseline speaker_names 里有但 voice_selection_review.speakers
    // 没有的 speaker (commit-after-add 路径)
    const editingExtraIds = new Set(editingExtras.map((e) => e.speakerId))
    const fromBaselineNames: SpeakerPayload[] = []
    for (const [sid, name] of Object.entries(baselineSpeakerNames)) {
      if (baselineIds.has(sid)) continue
      if (editingExtraIds.has(sid)) continue
      if (!segmentsBySpeaker.has(sid)) continue  // 没段不显示
      fromBaselineNames.push(synthFromName(sid, name))
    }

    return [...speakers, ...editingExtras, ...fromBaselineNames]
  }, [speakers, editingSpeakers, segmentsBySpeaker, baselineSpeakerNames])

  // ---- Bootstrap: same load sequence as VoiceSelectionPanel ----
  useEffect(() => {
    let cancelled = false
    async function load() {
      setIsLoading(true)
      setLoadError(null)
      try {
        const [reviewState, userVoices, pricingResult] = await Promise.all([
          apiClient.get<{
            results?: {
              review_flow?: {
                stages?: Record<string, { payload?: Record<string, unknown> }>
              }
            }
          }>(`/jobs/${jobId}/review-state`),
          getUserVoices().catch(() => [] as UserVoiceEntry[]),
          getVoiceSelectionPricing().catch(() => null as VoiceSelectionPricingResponse | null),
        ])
        if (cancelled) return

        const stages = reviewState.results?.review_flow?.stages ?? {}
        // v2 (round 3): 用 speaker_review.payload.speaker_names 作 fallback
        // 来源,补 voice_selection_review.speakers 漏的 commit-after-add speakers
        const speakerReviewPayload = stages.speaker_review?.payload ?? {}
        const rawNames = (speakerReviewPayload as Record<string, unknown>).speaker_names
        if (rawNames && typeof rawNames === "object" && !Array.isArray(rawNames)) {
          const cleaned: Record<string, string> = {}
          for (const [sid, name] of Object.entries(rawNames as Record<string, unknown>)) {
            if (typeof sid === "string" && typeof name === "string" && name.trim()) {
              cleaned[sid] = name
            }
          }
          setBaselineSpeakerNames(cleaned)
        } else {
          setBaselineSpeakerNames({})
        }
        const payload = stages.voice_selection_review?.payload ?? {}

        // Parse speakers + auto_matched_by_provider
        const rawSpeakers = Array.isArray(payload.speakers) ? payload.speakers : []
        const loadedSpeakers: SpeakerPayload[] = rawSpeakers.map((s: Record<string, unknown>) => {
          const byProv = (s.auto_matched_by_provider ?? {}) as Record<string, Record<string, unknown> | null>
          const amByProv: Record<string, { voiceId: string; label: string; backups: { voiceId: string; label: string }[] } | null> = {}
          for (const [prov, match] of Object.entries(byProv)) {
            if (match && typeof match === "object") {
              const rawBackups = Array.isArray(match.backup_voices) ? match.backup_voices : []
              const backups = rawBackups
                .map((b: unknown) => {
                  if (b && typeof b === "object") {
                    const obj = b as Record<string, unknown>
                    return { voiceId: String(obj.voice_id ?? ""), label: String(obj.label ?? obj.voice_id ?? "") }
                  }
                  return { voiceId: "", label: "" }
                })
                .filter((b: { voiceId: string }) => b.voiceId)
              amByProv[prov] = {
                voiceId: String(match.voice_id ?? ""),
                label: String(match.label ?? match.voice_id ?? ""),
                backups,
              }
            } else {
              amByProv[prov] = null
            }
          }
          const rawAuto = (s.auto_matched_voice ?? null) as Record<string, unknown> | null
          const rawProbeTexts = Array.isArray(s.probe_texts) ? s.probe_texts : []
          return {
            speakerId: String(s.speaker_id ?? ""),
            speakerName: String(s.speaker_name ?? s.speaker_id ?? ""),
            segmentCount: Number(s.segment_count ?? 0),
            totalDurationS: Number(s.total_duration_s ?? 0),
            canClone: Boolean(s.can_clone),
            autoMatchedByProvider: amByProv,
            autoMatchedVoice: rawAuto
              ? {
                  voiceId: String(rawAuto.voice_id ?? ""),
                  label: String(rawAuto.label ?? rawAuto.voice_id ?? ""),
                }
              : null,
            probeTexts: rawProbeTexts
              .map((p: unknown) => {
                if (p && typeof p === "object") {
                  const obj = p as Record<string, unknown>
                  return {
                    segmentId: Number(obj.segment_id ?? 0),
                    sourceText: String(obj.source_text ?? ""),
                    cnText: String(obj.cn_text ?? ""),
                  }
                }
                return { segmentId: 0, sourceText: "", cnText: "" }
              }),
          }
        }).filter((s: SpeakerPayload) => s.speakerId)

        // Default provider
        const loadedDefaultProvider = String(payload.default_provider ?? "minimax")
        setDefaultProvider(loadedDefaultProvider)

        // Pricing
        if (pricingResult) {
          setPricing(pricingResult)
          setCloneCostCredits(pricingResult.voice_clone_cost_credits)
        }

        // all_providers → providerMap
        const rawAllProviders = payload.all_providers as Record<string, Record<string, unknown>> | undefined
        const multiProvider = !!rawAllProviders && Object.keys(rawAllProviders).length > 0
        setHasMultiProvider(multiProvider)
        if (multiProvider && rawAllProviders) {
          const pm: Record<string, ProviderInfo> = {}
          for (const [prov, info] of Object.entries(rawAllProviders)) {
            const rawVoices = Array.isArray(info.available_voices) ? info.available_voices : []
            pm[prov] = {
              label: String(info.label ?? prov),
              supportsClone: Boolean(info.supports_clone),
              availableVoices: rawVoices.map((v: Record<string, unknown>) => ({
                voiceId: String(v.voice_id ?? ""),
                label: String(v.label ?? v.voice_id ?? ""),
                gender: String(v.gender ?? ""),
                provider: String(v.provider ?? prov),
                charsPerSecond: v.chars_per_second != null ? Number(v.chars_per_second) : null,
                speedCalibratedAt: v.speed_calibrated_at != null ? String(v.speed_calibrated_at) : null,
              })).filter((v: AvailableVoice) => v.voiceId),
            }
          }
          setProviderMap(pm)
        }

        // Fallback flat available_voices (old payloads)
        const rawAvailableVoices = Array.isArray(payload.available_voices) ? payload.available_voices : []
        setFallbackVoices(rawAvailableVoices.map((v: Record<string, unknown>) => ({
          voiceId: String(v.voice_id ?? ""),
          label: String(v.label ?? v.voice_id ?? ""),
          gender: String(v.gender ?? ""),
          provider: String(v.provider ?? ""),
          charsPerSecond: v.chars_per_second != null ? Number(v.chars_per_second) : null,
          speedCalibratedAt: v.speed_calibrated_at != null ? String(v.speed_calibrated_at) : null,
        })).filter((v: AvailableVoice) => v.voiceId))

        // Expired voice ids (main flow's pipeline validation)
        const payloadExpired = Array.isArray(payload.expired_voice_ids)
          ? payload.expired_voice_ids.map(String)
          : []
        setExpiredVoiceIds(payloadExpired)

        // Personal voices (clone library)
        setPersonalVoices(userVoices)

        // Phase 2: fetch personal-voice candidates per speaker (best-effort).
        // Done BEFORE building draft state so we can preselect strong matches
        // on speakers WITHOUT an existing voice_map override / baseline. Critical:
        // existing override/baseline must NOT be clobbered — those represent
        // user / pipeline decisions already in effect. Failures degrade silently.
        const candidateMap: Record<string, VoiceCandidatesResponse> = {}
        await Promise.allSettled(
          loadedSpeakers.map(async (sp) => {
            try {
              const result = await getVoiceCandidates({
                jobId,
                speakerId: sp.speakerId,
                speakerName: sp.speakerName,
                selectedProvider: loadedDefaultProvider,
              })
              candidateMap[sp.speakerId] = result
            } catch (err) {
              // Best-effort; skip this speaker's candidates on failure.
              console.warn('getVoiceCandidates failed for speaker', sp.speakerId, err)
            }
          }),
        )
        if (cancelled) return

        // Seed draft state per speaker. Precedence:
        //   1. voice_map override (user already changed in this session)
        //   2. Baseline = first segment's voice_id + tts_provider / provider
        //      — this is what the original pipeline actually used at TTS
        //      time, including user's original voice pick / cloned voice.
        //      Dropdown default of "★ 自动匹配" would be misleading: it's
        //      just a suggestion, not what's in effect.
        //   3. Phase 2: personal-voice strong match (auto-reuse) — only fires
        //      when no override AND no baseline exists. voice_reuse=true so
        //      the eventual voice_map write includes the audit flag.
        //   4. Fallback to auto_matched only when nothing else is set
        //      (e.g. legacy task without voice_id on segments).
        const initial: Record<string, SpeakerDraftState> = {}
        for (const sp of loadedSpeakers) {
          const ownSegments = segmentsBySpeakerForSeed(segments, sp.speakerId)
          const firstOverriddenSeg = ownSegments.find((seg) => voiceMap[seg.segment_id])
          const override = firstOverriddenSeg ? voiceMap[firstOverriddenSeg.segment_id] : null

          // 2026-05-09: minimaxModel 默认值要从 segment.tts_model_key 推,
          // 不能硬编码 "turbo" — 主流程 / 上次编辑保存的是 "speech-X.X-hd"
          // (旗舰音质) 或 "speech-X.X-turbo" (高级音质)。voice_map override
          // 若携带 tts_model_key,说明用户在编辑页重新选择了音质。
          const firstSeg = ownSegments[0]
          const segModelKey = (firstSeg as { tts_model_key?: unknown } | undefined)?.tts_model_key
          const overrideModelKey = override?.tts_model_key
          const effectiveModelKey = typeof overrideModelKey === "string" && overrideModelKey
            ? overrideModelKey
            : segModelKey
          const inferredModel: "turbo" | "hd" =
            typeof effectiveModelKey === "string" && effectiveModelKey.toLowerCase().includes("hd")
              ? "hd"
              : "turbo"

          if (override) {
            // Existing user-saved override — never clobber with auto-reuse.
            initial[sp.speakerId] = {
              voiceId: override.voice_id,
              selectedProvider: override.provider,
              voiceSource: "catalog",
              voiceReuse: false,
              minimaxModel: inferredModel,
            }
            continue
          }

          const baselineVoiceId = firstSeg?.voice_id
            ? String(firstSeg.voice_id).trim()
            : ""
          const baselineProvider = firstSeg?.tts_provider
            ? String(firstSeg.tts_provider).trim()
            : firstSeg?.provider
              ? String(firstSeg.provider).trim()
              : ""

          if (baselineVoiceId) {
            // Pipeline already picked / cloned a voice — keep it. The user
            // can still pick a candidate manually from the dropdown.
            initial[sp.speakerId] = {
              voiceId: baselineVoiceId,
              selectedProvider: baselineProvider || loadedDefaultProvider,
              voiceSource: "catalog",
              voiceReuse: false,
              minimaxModel: inferredModel,
            }
            continue
          }

          // Phase 2: no override, no baseline — try preselecting strong match.
          // MiniMax-only (personal voices live in MiniMax registry).
          const candidate = candidateMap[sp.speakerId]?.autoReuseVoice
          const candidateUsable =
            candidate
            && !payloadExpired.includes(candidate.voiceId)
            && (loadedDefaultProvider === "minimax" || !loadedDefaultProvider)
          if (candidateUsable && candidate) {
            initial[sp.speakerId] = {
              voiceId: candidate.voiceId,
              selectedProvider: "minimax",
              voiceSource: "cloned",
              voiceReuse: true,
              minimaxModel: inferredModel,
            }
            continue
          }

          const provMatch = sp.autoMatchedByProvider[loadedDefaultProvider]
          initial[sp.speakerId] = {
            voiceId: provMatch?.voiceId ?? "",
            selectedProvider: loadedDefaultProvider,
            voiceSource: provMatch?.voiceId ? "auto_matched" : "catalog",
            voiceReuse: false,
            minimaxModel: inferredModel,
          }
        }

        setSpeakers(loadedSpeakers)
        setVoiceCandidates(candidateMap)
        setDraftStates(initial)
      } catch (err) {
        if (cancelled) return
        setLoadError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
    // Intentionally do not depend on segments / voiceMap — we only seed
    // draft state on mount. After mount, user edits the dropdowns and
    // we sync to server via the apply button. If upstream voice_map
    // changes (e.g. user ran batch regen from text Tab), the user
    // explicitly re-enters this Tab to re-seed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId])

  // Cleanup preview audio on unmount
  useEffect(() => {
    return () => {
      if (previewAudioRef.current) {
        previewAudioRef.current.pause()
        previewAudioRef.current = null
      }
    }
  }, [])

  // Phase 4.2 E.1: fetch CosyVoice clone-gate once on mount (mirrors
  // VoiceSelectionPanel). Failure → null state → CosyVoice button hidden,
  // matching the safe denied default. No periodic refresh — gate is per
  // user policy state.
  useEffect(() => {
    let cancelled = false
    getCosyvoiceCloneGate()
      .then((data) => {
        if (cancelled) return
        setCosyvoiceCloneGate(data)
      })
      .catch(() => {
        /* silent — null state hides the button safely */
      })
    return () => {
      cancelled = true
    }
  }, [])

  // ---- Handlers ----

  const handleProviderChange = useCallback((speakerId: string, provider: string) => {
    setDraftStates((prev) => {
      const sp = displaySpeakers.find((s) => s.speakerId === speakerId)
      const provMatch = sp?.autoMatchedByProvider[provider]
      return {
        ...prev,
        [speakerId]: {
          ...prev[speakerId],
          selectedProvider: provider,
          voiceId: provMatch?.voiceId ?? "",
          voiceSource: provMatch?.voiceId ? "auto_matched" : "catalog",
          voiceReuse: false,
        },
      }
    })
    setPreviewError((p) => ({ ...p, [speakerId]: null }))

    // Phase 2 follow-up (plan 2026-05-17 review P2-4): candidates were
    // fetched once on mount with the default provider. Personal voices
    // are provider-isolated (a MiniMax clone never appears in the
    // CosyVoice candidate set), so switching providers mid-session used
    // to leave the candidate optgroups stale or empty. Refetch for this
    // speaker only — best-effort, log on failure.
    const sp = displaySpeakers.find((s) => s.speakerId === speakerId)
    if (!sp) return
    void (async () => {
      try {
        const result = await getVoiceCandidates({
          jobId,
          speakerId,
          speakerName: sp.speakerName,
          selectedProvider: provider,
        })
        setVoiceCandidates((prev) => ({ ...prev, [speakerId]: result }))
      } catch (err) {
        console.warn("getVoiceCandidates refetch failed for speaker", speakerId, err)
      }
    })()
  }, [displaySpeakers, jobId])

  const handleVoiceChange = useCallback((speakerId: string, voiceId: string) => {
    // Phase 2 (plan 2026-05-17): both matched candidates AND voices
    // picked from "其他个人音色" optgroup are reuse events — no clone
    // provider call, no clone points. Both must carry voiceReuse=true
    // so the gateway's setVoiceOverride() write feeds the post-edit
    // audit trail (``_record_voice_reuse_events`` filter
    // ``voice_reuse is True``) uniformly. Without this, picking an
    // arbitrary personal voice silently skips the audit row.
    const candidates = voiceCandidates[speakerId]
    const matchedCandidate = candidates
      ? candidates.autoReuseVoice?.voiceId === voiceId
        ? candidates.autoReuseVoice
        : candidates.personalVoiceCandidates.find((c) => c.voiceId === voiceId) ?? null
      : null
    const isOtherPersonal =
      !matchedCandidate && personalVoices.some((v) => v.voiceId === voiceId)
    const isPersonalVoice = !!matchedCandidate || isOtherPersonal
    setDraftStates((prev) => ({
      ...prev,
      [speakerId]: {
        ...prev[speakerId],
        voiceId,
        voiceSource: isPersonalVoice ? "cloned" : "catalog",
        voiceReuse: isPersonalVoice,
      },
    }))
    setPreviewError((p) => ({ ...p, [speakerId]: null }))
  }, [voiceCandidates, personalVoices])

  const handleCloneComplete = useCallback((speakerId: string, voiceId: string, options?: { reused?: boolean }) => {
    setDraftStates((prev) => ({
      ...prev,
      [speakerId]: { ...prev[speakerId], voiceId, voiceSource: "cloned", voiceReuse: options?.reused ?? false },
    }))
    setCloneModalSpeaker(null)
    getUserVoices().then(setPersonalVoices).catch(() => {})
  }, [])

  const handlePreview = useCallback(async (speakerId: string) => {
    const state = draftStates[speakerId]
    if (!state?.voiceId) return
    if (previewAudioRef.current) {
      previewAudioRef.current.pause()
      previewAudioRef.current = null
    }
    setPreviewLoading((p) => ({ ...p, [speakerId]: true }))
    setPreviewError((p) => ({ ...p, [speakerId]: null }))
    try {
      const sp = displaySpeakers.find((s) => s.speakerId === speakerId)
      const probeText = sp?.probeTexts?.[0]?.cnText || undefined
      const result = await previewVoice(jobId, state.voiceId, {
        ttsProvider: state.selectedProvider,
        sampleText: probeText,
      })
      if (result.expired) {
        setPreviewError((p) => ({ ...p, [speakerId]: "音色已失效，请重新选择" }))
        setDraftStates((prev) => ({
          ...prev,
          [speakerId]: { ...prev[speakerId], voiceId: "", voiceSource: "catalog", voiceReuse: false },
        }))
        setExpiredVoiceIds((prev) => [...prev, state.voiceId])
        await deleteUserVoice(state.voiceId).catch(() => {})
        setPersonalVoices((prev) => prev.filter((v) => v.voiceId !== state.voiceId))
        return
      }
      if (result.error) {
        setPreviewError((p) => ({ ...p, [speakerId]: result.error }))
        return
      }
      if (result.audioBase64) {
        const audio = new Audio(`data:audio/wav;base64,${result.audioBase64}`)
        audio.onended = () => {
          previewAudioRef.current = null
        }
        audio.play().catch(() => {})
        previewAudioRef.current = audio
      }
    } catch (err) {
      setPreviewError((p) => ({ ...p, [speakerId]: getErrorMessage(err) }))
    } finally {
      setPreviewLoading((p) => ({ ...p, [speakerId]: false }))
    }
  }, [draftStates, jobId, speakers])

  const setSpeakerApplying = useCallback((speakerId: string, applying: boolean) => {
    setApplyingSpeakerIds((prev) => {
      const next = new Set(prev)
      if (applying) next.add(speakerId)
      else next.delete(speakerId)
      return next
    })
  }, [])

  // Apply current draft voice → write voice_map for every segment of
  // this speaker. Partial failure is surfaced but the rest succeed
  // (same contract as batch re-TTS D38).
  const handleApplySpeaker = useCallback(
    async (speakerId: string) => {
      const state = draftStates[speakerId]
      if (!state?.voiceId) return
      const ownSegments = segmentsBySpeaker.get(speakerId) ?? []
      if (ownSegments.length === 0) return

      setSpeakerApplying(speakerId, true)
      const next: Record<string, VoiceMapEntry> = { ...voiceMap }
      const failures: string[] = []
      const ttsModelKey = state.selectedProvider === "minimax"
        ? minimaxModelKey(state.minimaxModel)
        : undefined
      for (const seg of ownSegments) {
        try {
          await setVoiceOverride(
            jobId,
            seg.segment_id,
            state.selectedProvider,
            state.voiceId,
            ttsModelKey,
            state.voiceReuse,
          )
          next[seg.segment_id] = {
            provider: state.selectedProvider,
            voice_id: state.voiceId,
            ...(ttsModelKey ? { tts_model_key: ttsModelKey } : {}),
          }
        } catch (err) {
          failures.push(seg.segment_id)
          console.warn("setVoiceOverride failed", seg.segment_id, err)
        }
      }
      onVoiceMapChange(next)
      setSpeakerApplying(speakerId, false)
      if (failures.length > 0) {
        toast.error(`${failures.length} 段音色保存失败，其余成功`)
      } else {
        toast.success(`${ownSegments.length} 段音色已覆盖`)
      }
    },
    [draftStates, segmentsBySpeaker, voiceMap, jobId, onVoiceMapChange, setSpeakerApplying],
  )

  const handleRestoreSpeaker = useCallback(
    async (speakerId: string) => {
      const ownSegments = segmentsBySpeaker.get(speakerId) ?? []
      const toClear = ownSegments.filter((s) => voiceMap[s.segment_id])
      if (toClear.length === 0) return

      setSpeakerApplying(speakerId, true)
      const next: Record<string, VoiceMapEntry> = { ...voiceMap }
      const failures: string[] = []
      for (const seg of toClear) {
        try {
          await clearVoiceOverride(jobId, seg.segment_id)
          delete next[seg.segment_id]
        } catch (err) {
          failures.push(seg.segment_id)
          console.warn("clearVoiceOverride failed", seg.segment_id, err)
        }
      }
      onVoiceMapChange(next)
      setSpeakerApplying(speakerId, false)
      if (failures.length > 0) {
        toast.error(`${failures.length} 段恢复失败，其余成功`)
      } else {
        toast.success("已恢复原音色")
        // Also reset the draft state to the auto-match so the dropdown
        // no longer shows the overridden voice.
        const sp = displaySpeakers.find((s) => s.speakerId === speakerId)
        const prov = draftStates[speakerId]?.selectedProvider ?? defaultProvider
        const provMatch = sp?.autoMatchedByProvider[prov]
        setDraftStates((prev) => ({
          ...prev,
          [speakerId]: {
            ...prev[speakerId],
            voiceId: provMatch?.voiceId ?? "",
            voiceSource: provMatch?.voiceId ? "auto_matched" : "catalog",
            voiceReuse: false,
          },
        }))
      }
    },
    [segmentsBySpeaker, voiceMap, jobId, onVoiceMapChange, setSpeakerApplying, speakers, draftStates, defaultProvider],
  )

  // ---- Helpers ----

  const getVoicesForSpeaker = useCallback((speakerId: string): AvailableVoice[] => {
    const state = draftStates[speakerId]
    if (!state) return fallbackVoices
    if (hasMultiProvider && providerMap[state.selectedProvider]) {
      return providerMap[state.selectedProvider].availableVoices
    }
    return fallbackVoices
  }, [draftStates, fallbackVoices, hasMultiProvider, providerMap])

  const canSpeakerClone = useCallback((speakerId: string): boolean => {
    const sp = displaySpeakers.find((s) => s.speakerId === speakerId)
    if (!sp?.canClone) return false
    const state = draftStates[speakerId]
    if (!state) return false
    const provider = hasMultiProvider
      ? state.selectedProvider
      : defaultProvider
    // Runtime availability (A0b backend supports_clone).
    const supportsClone = hasMultiProvider
      ? providerMap[state.selectedProvider]?.supportsClone ?? false
      : defaultProvider === "minimax"
    if (!supportsClone) return false
    // Phase 4.2 E.1: CosyVoice second gate — display-layer authorization
    // visibility (D.1 /clone-gate). MiniMax legacy path unchanged.
    //
    // PR #15 P2 fix (Codex 2026-05-27): also AND runtime_ready via the
    // backend-computed `can_show_clone_button`. See VoiceSelectionPanel
    // for the full rationale; mirrored here for the editing-state tab.
    if (provider === "cosyvoice") {
      return cosyvoiceCloneGate?.can_show_clone_button === true
    }
    return true
  }, [speakers, draftStates, hasMultiProvider, providerMap, defaultProvider, cosyvoiceCloneGate])

  // ---- Render ----

  if (isLoading) {
    return (
      <section className="surface-card p-8 text-center">
        <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-[color:var(--cinnabar)] border-t-transparent" />
        <h3 className="text-lg font-semibold text-foreground">加载音色候选...</h3>
      </section>
    )
  }

  if (loadError && displaySpeakers.length === 0) {
    return (
      <section className="surface-card p-6">
        <p className="text-red-500">{loadError}</p>
        <p className="mt-2 text-xs text-muted-foreground">
          音色候选数据加载失败；请先到&ldquo;翻译修改&rdquo;Tab 完成文本编辑。
        </p>
      </section>
    )
  }

  const selectedSpeakerRef = cloneModalSpeaker
    ? (() => {
        const sp = displaySpeakers.find((s) => s.speakerId === cloneModalSpeaker)
        return sp ? { speakerId: sp.speakerId, speakerName: sp.speakerName } : null
      })()
    : null

  return (
    <>
      <section className="surface-card p-6 space-y-6">
        {/* Expired voices banner */}
        {expiredVoiceIds.length > 0 && (
          <div className="rounded-lg border border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/5 p-3">
            <p className="text-sm text-red-600 dark:text-red-400">
              检测到 {expiredVoiceIds.length} 个音色已失效，已从选项中移除。请重新选择音色。
            </p>
          </div>
        )}

        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1 flex-1 min-w-0">
            <h2 className="text-lg font-semibold text-foreground">音色修改</h2>
            <p className="text-sm text-muted-foreground">
              修改某个说话人的音色后，点击&ldquo;应用到此说话人&rdquo;即覆盖该说话人所有段的音色。
              覆盖后需要回到&ldquo;翻译修改&rdquo;Tab 点击&ldquo;一键重新合成&rdquo;才会生效。
            </p>
          </div>
          <button
            type="button"
            onClick={onRequestCreateSpeaker}
            className="shrink-0 h-9 rounded-md border border-border bg-background px-3 text-sm font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
          >
            + 新增说话人
          </button>
        </div>

        {/* Speaker list */}
        <div className="space-y-3">
          {displaySpeakers.map((sp, index) => {
            const state = draftStates[sp.speakerId]
            const currentProvider = state?.selectedProvider ?? defaultProvider
            const voicesForProvider = getVoicesForSpeaker(sp.speakerId)
            const showClone = canSpeakerClone(sp.speakerId)
            const ownSegments = segmentsBySpeaker.get(sp.speakerId) ?? []
            const overriddenCount = ownSegments.filter((s) => voiceMap[s.segment_id]).length
            const hasOverride = overriddenCount > 0
            const applying = applyingSpeakerIds.has(sp.speakerId)

            // Active override entry (what the speaker's segments currently
            // use). Show as "当前生效" so users see server state, not
            // just dropdown draft.
            const firstOverrideSeg = ownSegments.find((s) => voiceMap[s.segment_id])
            const appliedEntry = firstOverrideSeg ? voiceMap[firstOverrideSeg.segment_id] : null

            // Effective voice = override if present, else baseline from
            // the first segment's voice_id / tts_provider. This is what
            // actually plays at TTS time for this speaker.
            const firstSeg = ownSegments[0]
            const baselineVoiceId = firstSeg?.voice_id
              ? String(firstSeg.voice_id).trim()
              : ""
            const baselineProvider = firstSeg?.tts_provider
              ? String(firstSeg.tts_provider).trim()
              : firstSeg?.provider
                ? String(firstSeg.provider).trim()
                : ""
            const effectiveVoiceId = appliedEntry?.voice_id ?? baselineVoiceId
            const effectiveProvider = appliedEntry?.provider ?? baselineProvider

            // Disable Apply when the draft matches whatever's effectively
            // running (override OR baseline) — no point writing a no-op
            // override, and it'd clutter voice_map with redundant entries.
            const draftMatchesEffective =
              state != null
              && state.voiceId === effectiveVoiceId
              && state.selectedProvider === effectiveProvider

            // Nice label for the effective voice (used by the "当前生效"
            // row and the "原任务音色 / 当前覆盖" pinned option).
            const effectiveLabel = (() => {
              if (!effectiveVoiceId) return ""
              const fromProvider = (() => {
                const src = hasMultiProvider
                  ? providerMap[effectiveProvider]?.availableVoices
                  : fallbackVoices
                return src?.find((v) => v.voiceId === effectiveVoiceId)
              })()
              if (fromProvider) return formatVoiceOptionLabel(fromProvider)
              const fromPersonal = personalVoices.find((v) => v.voiceId === effectiveVoiceId)
              if (fromPersonal) return fromPersonal.label || fromPersonal.voiceId
              return effectiveVoiceId
            })()

            // Status label
            const statusLabel = hasOverride
              ? overriddenCount === ownSegments.length
                ? `已覆盖 ${ownSegments.length} 段`
                : `已覆盖 ${overriddenCount}/${ownSegments.length} 段`
              : "原音色"
            const statusColor = hasOverride
              ? "text-[color:var(--ochre)]"
              : "text-muted-foreground"

            return (
              <div
                key={sp.speakerId}
                className="rounded-lg border border-border bg-card/60 p-4"
              >
                <div className="flex items-start gap-3">
                  {/* Avatar — uses ink palette tokens for consistency with
                   *  the project's data-theme="ink"/"ink-dark" (user
                   *  feedback 2026-05-17: slate-grey clashed with cream/
                   *  charcoal palette). */}
                  <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10 text-sm font-bold text-primary shrink-0">
                    {String.fromCharCode(65 + index)}
                  </div>

                  <div className="flex-1 min-w-0 space-y-2">
                    {/* Name + status */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-foreground text-sm">{sp.speakerName || sp.speakerId}</span>
                      <span className="text-xs text-muted-foreground">{sp.speakerId}</span>
                      <span className="text-xs text-muted-foreground">
                        {ownSegments.length} 段 · {sp.totalDurationS.toFixed(1)}s
                      </span>
                      <span className={`text-xs font-medium ${statusColor}`}>{statusLabel}</span>
                      {/* Profile-status badge — only renders for editing-mode
                          speakers (source !== "baseline"); baseline returns null. */}
                      {(() => {
                        const editingSp = editingSpeakerById.get(sp.speakerId)
                        if (!editingSp) return null
                        return (
                          <EditPageSpeakerProfileBadge
                            speaker={editingSp}
                            onRetry={() => onRetryProfile(editingSp.speaker_id)}
                          />
                        )
                      })()}
                    </div>

                    {/* Effective voice — what actually plays at TTS time.
                        Shows voice_map override when present, otherwise
                        baseline from the pipeline's last run (cloned
                        voice / user's original pick). Always rendered so
                        users see server truth, not just dropdown draft. */}
                    {effectiveVoiceId && (
                      <div className="text-xs text-muted-foreground">
                        {hasOverride ? "当前覆盖：" : "当前生效（原任务）："}
                        <span className="font-mono">
                          {effectiveProvider ? `[${effectiveProvider}] ` : ""}{effectiveLabel}
                        </span>
                      </div>
                    )}

                    {/* Provider Tabs */}
                    {hasMultiProvider && (
                      <div className="flex gap-1">
                        {PROVIDER_TAB_ORDER.filter((p) => !!providerMap[p]).map((prov) => {
                          const isActive = currentProvider === prov
                          return (
                            <button
                              key={prov}
                              type="button"
                              className={`h-7 rounded-md px-3 text-xs font-medium transition ${
                                isActive
                                  ? "bg-primary text-primary-foreground"
                                  : "bg-muted text-muted-foreground hover:bg-muted/70 hover:text-foreground"
                              }`}
                              onClick={() => handleProviderChange(sp.speakerId, prov)}
                              disabled={applying}
                            >
                              {PROVIDER_SHORT_LABELS[prov] ?? prov}
                            </button>
                          )
                        })}
                      </div>
                    )}

                    {/* Voice select + preview + clone */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <select
                        className="h-8 rounded border border-border bg-background px-2 text-sm text-foreground w-[220px] truncate disabled:opacity-50"
                        onChange={(e) => handleVoiceChange(sp.speakerId, e.target.value)}
                        value={state?.voiceId ?? ""}
                        disabled={applying}
                      >
                        <option value="">-- 选择音色 --</option>
                        {/* Pinned "current" row — ensures the effective voice
                            is always visible/selectable even when it's missing
                            from catalog (expired clone, legacy voice_id, etc.).
                            Only shown when the effective provider matches the
                            current provider Tab AND the voice isn't already
                            naturally surfaced by catalog / personal groups.
                         */}
                        {(() => {
                          if (!effectiveVoiceId) return null
                          if (effectiveProvider && effectiveProvider !== currentProvider) return null
                          const inCatalog = voicesForProvider.some((v) => v.voiceId === effectiveVoiceId)
                          const inPersonal = currentProvider === "minimax"
                            && personalVoices.some((v) => v.voiceId === effectiveVoiceId)
                          if (inCatalog || inPersonal) return null
                          return (
                            <optgroup label={hasOverride ? "📌 当前覆盖" : "📌 当前生效（原任务）"}>
                              <option value={effectiveVoiceId}>
                                {effectiveLabel}
                              </option>
                            </optgroup>
                          )
                        })()}
                        {/* Phase 2 (plan 2026-05-17): personal-voice candidate
                           groups, ordered above official recommendations. Only
                           meaningful for MiniMax. Order:
                           1) 强匹配 — auto-reuse (voice_reuse=true preselected)
                           2) 可能匹配 — requires_user_confirmation
                           3) 其他个人音色 — full library minus matched ones */}
                        {(() => {
                          if (currentProvider !== "minimax") return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const auto = candidates?.autoReuseVoice ?? null
                          if (!auto || expiredVoiceIds.includes(auto.voiceId)) return null
                          return (
                            <optgroup label="个人音色 · 强匹配 (不扣点)">
                              <option value={auto.voiceId}>
                                {`★ ${auto.label}${formatCandidateSourceHint(auto)}`}
                              </option>
                            </optgroup>
                          )
                        })()}
                        {(() => {
                          if (currentProvider !== "minimax") return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const list = (candidates?.personalVoiceCandidates ?? [])
                            .filter((c) => c.requiresUserConfirmation
                              && !expiredVoiceIds.includes(c.voiceId))
                          if (list.length === 0) return null
                          return (
                            <optgroup label="个人音色 · 可能匹配 (需要确认)">
                              {list.map((c) => (
                                <option key={`pvc-${c.voiceId}`} value={c.voiceId}>
                                  {`${matchScopeBadge(c.matchScope)} ${c.label}${formatCandidateSourceHint(c)}`}
                                </option>
                              ))}
                            </optgroup>
                          )
                        })()}
                        {(() => {
                          if (currentProvider !== "minimax") return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const candidateIds = new Set<string>()
                          if (candidates?.autoReuseVoice) {
                            candidateIds.add(candidates.autoReuseVoice.voiceId)
                          }
                          for (const c of candidates?.personalVoiceCandidates ?? []) {
                            candidateIds.add(c.voiceId)
                          }
                          const others = personalVoices.filter((v) =>
                            !expiredVoiceIds.includes(v.voiceId)
                            && !candidateIds.has(v.voiceId),
                          )
                          if (others.length === 0) return null
                          return (
                            <optgroup label="其他个人音色">
                              {others.map((v) => (
                                <option key={v.voiceId} value={v.voiceId}>
                                  {v.label || v.voiceId}
                                </option>
                              ))}
                            </optgroup>
                          )
                        })()}
                        {/* Phase 4.2 E.1 PR #15 Codex P2² fix: CosyVoice
                            personal clones — mirrored from VoiceSelectionPanel
                            so Studio edit flow also surfaces newly cloned
                            voices in the dropdown. See parent file for
                            full rationale. */}
                        {(() => {
                          if (currentProvider !== "cosyvoice") return null
                          const cosyClones = personalVoices.filter((v) => {
                            if (expiredVoiceIds.includes(v.voiceId)) return false
                            return (
                              v.provider === "cosyvoice_voice_clone"
                              || v.ttsProvider === "cosyvoice"
                            )
                          })
                          if (cosyClones.length === 0) return null
                          return (
                            <optgroup label="我的 CosyVoice 克隆音色">
                              {cosyClones.map((v) => (
                                <option key={`cosy-clone-${v.voiceId}`} value={v.voiceId}>
                                  {v.label || v.voiceId}
                                </option>
                              ))}
                            </optgroup>
                          )
                        })()}
                        {/* Smart recommendations */}
                        {(() => {
                          const provMatch = sp.autoMatchedByProvider[currentProvider]
                          if (!provMatch?.voiceId) return null
                          const voiceById = new Map(voicesForProvider.map((v) => [v.voiceId, v]))
                          const recIds: string[] = [provMatch.voiceId]
                          for (const b of provMatch.backups) {
                            if (!recIds.includes(b.voiceId)) recIds.push(b.voiceId)
                          }
                          if (recIds.length === 0) return null
                          return (
                            <optgroup label="🎯 智能推荐 (按匹配度排序)">
                              {recIds.map((vid, i) => {
                                const v = voiceById.get(vid)
                                const fallbackLabel =
                                  vid === provMatch.voiceId
                                    ? provMatch.label
                                    : provMatch.backups.find((b) => b.voiceId === vid)?.label || vid
                                const baseLabel = v ? formatVoiceOptionLabel(v) : fallbackLabel
                                const prefix = i === 0 ? "★ 自动匹配" : `#${i + 1} 推荐`
                                return (
                                  <option key={`rec-${vid}`} value={vid}>
                                    {`${prefix} · ${baseLabel}`}
                                  </option>
                                )
                              })}
                            </optgroup>
                          )
                        })()}
                        {/* Catalog grouped by gender */}
                        {(() => {
                          const femaleVoices = voicesForProvider.filter((v) => v.gender === "female")
                          const maleVoices = voicesForProvider.filter((v) => v.gender === "male")
                          const otherVoices = voicesForProvider.filter(
                            (v) => v.gender !== "male" && v.gender !== "female",
                          )
                          return (
                            <>
                              {femaleVoices.length > 0 && (
                                <optgroup label={`女声 (${femaleVoices.length})`}>
                                  {femaleVoices.map((v) => (
                                    <option key={v.voiceId} value={v.voiceId}>
                                      {formatVoiceOptionLabel(v)}
                                    </option>
                                  ))}
                                </optgroup>
                              )}
                              {maleVoices.length > 0 && (
                                <optgroup label={`男声 (${maleVoices.length})`}>
                                  {maleVoices.map((v) => (
                                    <option key={v.voiceId} value={v.voiceId}>
                                      {formatVoiceOptionLabel(v)}
                                    </option>
                                  ))}
                                </optgroup>
                              )}
                              {otherVoices.length > 0 && (
                                <optgroup label={`其他 (${otherVoices.length})`}>
                                  {otherVoices.map((v) => (
                                    <option key={v.voiceId} value={v.voiceId}>
                                      {formatVoiceOptionLabel(v)}
                                    </option>
                                  ))}
                                </optgroup>
                              )}
                            </>
                          )
                        })()}
                      </select>

                      {/* Preview */}
                      {state?.voiceId && (
                        <button
                          type="button"
                          className="h-8 rounded border border-border px-3 text-xs font-medium text-muted-foreground transition hover:text-foreground hover:bg-muted/60 disabled:opacity-50"
                          disabled={previewLoading[sp.speakerId] || applying}
                          onClick={() => {
                            void handlePreview(sp.speakerId)
                          }}
                        >
                          {previewLoading[sp.speakerId] ? "试听中..." : "试听"}
                        </button>
                      )}

                      {/* 核对原音 — 与主流程一致(VoiceSelectionPanel),弹出
                          SpeakerAudioAuditModal readOnly 模式: 只播放、
                          不允许 reassign / keep-original (editing 状态
                          后端这俩端点会 409)。 */}
                      <button
                        type="button"
                        className="h-8 rounded border border-border px-3 text-xs font-medium text-muted-foreground transition hover:text-foreground hover:bg-muted/60 disabled:opacity-50"
                        disabled={ownSegments.length <= 0 || applying}
                        onClick={() => setAuditModalSpeaker(sp.speakerId)}
                      >
                        核对原音
                      </button>

                      {/* Clone */}
                      {showClone && (
                        <button
                          type="button"
                          className="h-8 rounded px-3 text-xs font-medium transition disabled:opacity-50 border border-[color:var(--cinnabar)]/40 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20"
                          disabled={applying}
                          onClick={() => {
                            // Phase 4.2 E.1 — provider-aware dispatch.
                            // Mirrors VoiceSelectionPanel. MiniMax keeps
                            // legacy in-file VoiceCloneModal (untouched);
                            // CosyVoice opens the dedicated D.2 modal.
                            if (currentProvider === "cosyvoice") {
                              setCosyvoiceCloneModalSpeaker(sp.speakerId)
                            } else {
                              setCloneModalSpeaker(sp.speakerId)
                            }
                          }}
                        >
                          克隆音色
                        </button>
                      )}

                      {/* Apply */}
                      <button
                        type="button"
                        className="h-8 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground transition hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
                        disabled={
                          applying
                          || !state?.voiceId
                          || draftMatchesEffective
                        }
                        onClick={() => void handleApplySpeaker(sp.speakerId)}
                        title={
                          draftMatchesEffective
                            ? "当前音色已是生效状态，无需重复应用"
                            : `将音色应用到该说话人的 ${ownSegments.length} 段`
                        }
                      >
                        {applying ? "应用中..." : "应用到此说话人"}
                      </button>

                      {/* Restore */}
                      {hasOverride && (
                        <button
                          type="button"
                          className="h-8 rounded border border-border px-3 text-xs font-medium text-muted-foreground transition hover:text-foreground hover:bg-muted/60 disabled:opacity-50"
                          disabled={applying}
                          onClick={() => void handleRestoreSpeaker(sp.speakerId)}
                        >
                          恢复原音色
                        </button>
                      )}
                    </div>

                    {/* Pricing / quality tier. MiniMax saves the selected tier
                        to voice_map.tts_model_key for post-edit TTS. */}
                    {pricing && (() => {
                      const prov = currentProvider
                      const cpm = pricing.credits_per_minute
                      if (prov === "minimax") {
                        const model = state?.minimaxModel ?? "turbo"
                        return (
                          <div className="flex items-center gap-4 flex-wrap">
                            <label
                              className="flex items-center gap-1.5 cursor-pointer"
                              onClick={() => setDraftStates((prev) => ({
                                ...prev,
                                [sp.speakerId]: { ...prev[sp.speakerId], minimaxModel: "turbo" },
                              }))}
                            >
                              <span
                                className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${
                                  model === "turbo"
                                    ? "border-[color:var(--cinnabar)]"
                                    : "border-border"
                                }`}
                              >
                                {model === "turbo" && (
                                  <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" />
                                )}
                              </span>
                              <span className="text-xs text-foreground">高级音质</span>
                              <span className="text-xs text-muted-foreground">{cpm.minimax_turbo} 点/分钟</span>
                            </label>
                            <label
                              className="flex items-center gap-1.5 cursor-pointer"
                              onClick={() => setDraftStates((prev) => ({
                                ...prev,
                                [sp.speakerId]: { ...prev[sp.speakerId], minimaxModel: "hd" },
                              }))}
                            >
                              <span
                                className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${
                                  model === "hd"
                                    ? "border-[color:var(--cinnabar)]"
                                    : "border-border"
                                }`}
                              >
                                {model === "hd" && (
                                  <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" />
                                )}
                              </span>
                              <span className="text-xs text-foreground">旗舰音质</span>
                              <span className="text-xs text-muted-foreground">{cpm.minimax_hd} 点/分钟</span>
                            </label>
                          </div>
                        )
                      }
                      const pts = prov === "cosyvoice"
                        ? cpm.cosyvoice
                        : prov === "volcengine"
                        ? cpm.volcengine
                        : null
                      return pts != null ? (
                        <div className="flex items-center gap-1.5">
                          <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 border-[color:var(--cinnabar)]">
                            <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" />
                          </span>
                          <span className="text-xs text-foreground">标准音质</span>
                          <span className="text-xs text-muted-foreground">{pts} 点/分钟</span>
                        </div>
                      ) : null
                    })()}

                    {previewError[sp.speakerId] && (
                      <p className="text-xs text-red-500">{previewError[sp.speakerId]}</p>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {displaySpeakers.length === 0 && (
          <p className="text-sm text-muted-foreground">该任务没有说话人信息。</p>
        )}

        {loadError && displaySpeakers.length > 0 && (
          <p className="text-xs text-red-500">加载时出现部分错误：{loadError}</p>
        )}
      </section>

      {/* Clone Modal — MiniMax legacy path (reused from VoiceSelectionPanel
          export). Function body untouched, locked by G_MX.2 / G6.1.5. */}
      {selectedSpeakerRef && (
        <VoiceCloneModal
          cloneCostCredits={cloneCostCredits}
          jobId={jobId}
          speaker={selectedSpeakerRef}
          onClose={() => setCloneModalSpeaker(null)}
          onComplete={handleCloneComplete}
        />
      )}

      {/* Phase 4.2 E.2 §0 决策 1：editing 路径只保留 file upload，不接
          source_segments picker（避免 baseline 段 vs editing 段语义混淆）。
          故意**不传** defaultSourceJobId —— modal 自然回落 file-only
          分支（segmentsModeAvailable === false）。等 edit-aware segment
          endpoint 出来再开放 source_segments。 */}
      {cosyvoiceCloneModalSpeaker ? (
        <CosyVoiceCloneModal
          open={true}
          onClose={() => setCosyvoiceCloneModalSpeaker(null)}
          speakerId={cosyvoiceCloneModalSpeaker}
          speakerName={
            displaySpeakers.find(
              (s) => s.speakerId === cosyvoiceCloneModalSpeaker,
            )?.speakerName ?? cosyvoiceCloneModalSpeaker
          }
          onSuccess={(voice) => {
            handleCloneComplete(
              cosyvoiceCloneModalSpeaker,
              voice.voice_id,
              { reused: false },
            )
            setCosyvoiceCloneModalSpeaker(null)
          }}
        />
      ) : null}

      {/* 核对原音 Modal — readOnly 模式 (editing 状态) */}
      {(() => {
        if (!auditModalSpeaker) return null
        const sp = displaySpeakers.find((s) => s.speakerId === auditModalSpeaker)
        if (!sp) return null
        return (
          <SpeakerAudioAuditModal
            jobId={jobId}
            speaker={{ speakerId: sp.speakerId, speakerName: sp.speakerName }}
            speakerOptions={displaySpeakers.map((s) => ({
              speakerId: s.speakerId,
              speakerName: s.speakerName,
            }))}
            onClose={() => setAuditModalSpeaker(null)}
            // editing 模式只读 — onReassigned 不会触发,但要传一个 noop
            // 因为 prop 是必填; 主流程 onReassigned 会刷新 speaker 列表,
            // editing 模式不需要。
            onReassigned={() => {}}
            readOnly
          />
        )
      })()}
    </>
  )
}

// ---------------------------------------------------------------------------
// Helper: seed-time segment grouping that doesn't require the memoized
// segmentsBySpeaker (because it runs inside the useEffect before that
// memo has re-computed).
// ---------------------------------------------------------------------------
function segmentsBySpeakerForSeed(segments: EditingSegment[], speakerId: string): EditingSegment[] {
  return segments.filter((s) => (s.speaker_id || "(未标注)") === speakerId)
}
