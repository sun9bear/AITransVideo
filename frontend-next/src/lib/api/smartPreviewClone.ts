/**
 * smartPreviewClone.ts — 智能版 3 分钟预览克隆（P3e-4c）前端数据层.
 *
 * 给「登录但未获 smart entitlement」的免费用户一条受限智能版预览 lane：选智能版 →
 * 按 gateway pricing 预扣克隆主说话人 → 拿到 3 分钟带水印、仅在线播放的 teaser；满意后「转完整」
 * 复用原视频 + 已克隆音色，走正式流程**按分钟正常扣点**（项目主 2026-06-15 决策：
 * 转换照常按分钟扣；entitlement 走正式流程·需升级套餐）。
 *
 * 设计取舍——**与主创建路径完全隔离**：本模块自建 ``/jobs`` body 并直发，
 * **不复用** ``submitTranslationJob``，以保证营收关键的主创建流零回归（同
 * ``anonymousPreview.ts`` 隔离匿名漏斗的做法）。后端契约真源见
 * ``gateway/job_intercept.py``（reuse 块 / Gate A / 600-reserve）+
 * ``gateway/smart_clone_reservation_service.py``（deny_reason 枚举）。
 *
 * 默认 inert：入口由 Next flag 展示（见 ``isSmartPreviewCloneEntryEnabled``），
 * 真正的开关是后端 admin ``smart_preview_clone_enabled``（默认 False）+ 生产
 * alembic 037/038。两者任一未就绪 → 后端拒绝并由下方 mapper 映射为中文文案。
 */

import { apiClient, ApiError } from '@/lib/api/client'
import { toJobSummary } from '@/lib/api/mappers'
import type { ApiJobRecord } from '@/types/api'
import type { CreateTranslationJobInput, JobSummary } from '@/types/jobs'

/**
 * 预览入口可见性 —— Next public flag（同 POST_EDIT / FREE_TIER / ANONYMOUS_PREVIEW
 * 约定：``=== "1"`` 才展示）。这是**展示闸**，不是安全闸——真正的 gate 在服务端
 * （admin ``smart_preview_clone_enabled`` + lane exemption + reservation）。flag 关
 * 时入口不渲染；即便误渲染，点击也会被后端拒绝并映射文案。
 */
export function isSmartPreviewCloneEntryEnabled(): boolean {
  return process.env.NEXT_PUBLIC_ENABLE_SMART_PREVIEW_CLONE === '1'
}

// ── /jobs body 构造（与 submitTranslationJob 同形，但隔离不复用）─────────────

/**
 * 构造与普通创建一致的 ``/jobs`` body 公共部分（source / speakers / 音色 /
 * 转录 / 语向）。与 ``jobs.ts::submitTranslationJob`` 行 75-93 + 152-155 同形——
 * 两处都对齐后端 create 契约，任一改动需同步（刻意不抽公共依赖以隔离主路径）。
 */
function buildSmartJobBody(input: CreateTranslationJobInput): Record<string, unknown> {
  const sourceType = input.sourceType ?? 'youtube_url'
  const source: Record<string, string> = {
    type: sourceType,
    value: sourceType === 'local_video' ? (input.localFilePath ?? '') : input.youtubeUrl,
  }
  if (sourceType === 'local_video' && input.localFileName) {
    source.filename = input.localFileName
  }
  const body: Record<string, unknown> = {
    job_type: 'localize_video',
    output_target: 'editor',
    source,
    speakers: input.speakers,
    voice_a: input.voiceA,
    voice_b: input.voiceB,
    transcription_method: input.transcriptionMethod ?? 'assemblyai',
    service_mode: 'smart',
  }
  // 仅在显式非默认语向时下发（与主路径一致：省略 → 锁 GA 默认 en->zh-CN）。
  if (input.sourceLanguage && input.targetLanguage) {
    body.source_language = input.sourceLanguage
    body.target_language = input.targetLanguage
  }
  return body
}

/**
 * 发起智能版 3 分钟预览任务。带 ``preview_mode: true`` + 完整 6 字段 smart_consent
 * （``auto_voice_clone: true`` 触发 600 预扣 + 主说话人克隆）。后端 lane exemption
 * 放行免费用户进入受限预览（3min 水印 / 跳分钟 / stream-only 全由服务端强制）。
 *
 * 成功 → 返回 JobSummary（预览任务，结果页放带水印 teaser）。
 * 失败 → 抛 ApiError；UI 用 ``mapSmartPreviewCreateError`` 映射中文文案。
 */
export async function createSmartPreviewJob(
  input: CreateTranslationJobInput,
): Promise<JobSummary> {
  const body = buildSmartJobBody(input)
  const sourceType = input.sourceType ?? 'youtube_url'
  if (sourceType === 'youtube_url') {
    // Gemini URL transcription cannot be bounded to the teaser window here.
    body.transcription_method = 'assemblyai'
  }
  // smart_consent 6 字段必须完整（后端 validate_smart_consent 严格校验）。
  // auto_voice_clone=true 是预览克隆的触发条件；其余字段与 jobs.ts 默认一致。
  body.smart_consent = {
    auto_voice_clone: true,
    auto_retranslate: false,
    auto_retts: true,
    auto_multimodal_verification: false,
    no_extra_charge_without_confirmation: true,
    confirm_paid_voice_clone_credits: true,
    on_budget_exhausted: 'degraded_delivery_with_report',
  }
  body.preview_mode = true
  const payload = await apiClient.post<ApiJobRecord>('/jobs', { body })
  return toJobSummary(payload)
}

/**
 * 预览转完整。**只发 ``reuse_preview_job_id``**（防越权——voice/source 由服务端
 * 从被复用的预览任务派生），后端强制 ``service_mode=smart`` +
 * ``auto_voice_clone=false``（不重扣 600、不重克隆）+ 清 ``preview_mode``
 * （按分钟正常扣点 + 完整交付）。body 仍带常规 job 字段供创建流读取。
 *
 * 免费用户（plan 不含 smart）走到这里会被 Gate A 拦为 403 ``smart_upgrade_required``
 * （决策 A）——UI 据此渲染「升级 Plus/Pro」CTA，而非死路。
 */
export async function convertPreviewToFull(
  input: CreateTranslationJobInput,
  previewJobId: string,
): Promise<JobSummary> {
  const body = buildSmartJobBody(input)
  // 防越权 + 最小化：音色 / 源 / consent 全由服务端从被复用的预览任务派生（job_intercept
  // reuse override，发生在任何源校验之前）。显式剔除 voice_a/voice_b，使转完整 body 只承载
  // 「复用意图」——即便未来 gateway 覆盖顺序变化，也不会把预览的临时克隆音色误当作新选音。
  delete body.voice_a
  delete body.voice_b
  body.reuse_preview_job_id = previewJobId
  const payload = await apiClient.post<ApiJobRecord>('/jobs', { body })
  return toJobSummary(payload)
}

// ── 结构化错误读取 + 中文文案映射 ──────────────────────────────────────────

interface GatewayErrorShape {
  error?: unknown
  message?: unknown
  detail?: unknown
}

interface ReadGatewayError {
  status: number
  code: string | null
  message: string | null
  detail: Record<string, unknown> | null
}

/** 从 ApiError 抽出 gateway ``_error_response`` 的 ``{error, message, detail}``。 */
function readGatewayError(err: unknown): ReadGatewayError {
  if (err instanceof ApiError) {
    const payload = err.payload
    if (payload && typeof payload === 'object') {
      const obj = payload as GatewayErrorShape
      return {
        status: err.status,
        code: typeof obj.error === 'string' ? obj.error : null,
        message: typeof obj.message === 'string' ? obj.message : null,
        detail:
          obj.detail && typeof obj.detail === 'object'
            ? (obj.detail as Record<string, unknown>)
            : null,
      }
    }
    return { status: err.status, code: null, message: err.message || null, detail: null }
  }
  return {
    status: 0,
    code: null,
    message: err instanceof Error ? err.message : null,
    detail: null,
  }
}

export type SmartPreviewCreateReason =
  | 'insufficient_credits' // 余额不足 600 → 引导充值
  | 'voice_library_full' // 个人音色库满 → 引导清理
  | 'cap_exceeded' // 全局/并发反滥用 cap → 稍后再试
  | 'disabled' // lane 未开放 / kill-switch
  | 'auth_required' // 未登录
  | 'consent_invalid' // 同意确认无效（理论上前端不会触发）
  | 'reserve_failed' // 预扣未成功（重放 / reserve 故障 / user 缺失）
  | 'unknown'

export interface SmartPreviewCreateError {
  reason: SmartPreviewCreateReason
  /** 直接可展示给用户的中文文案。 */
  message: string
}

export interface SmartPreviewCreateErrorOptions {
  /** Gateway pricing API returned ``smart_preview_clone_cost_credits``. */
  cloneCostCredits?: number | null
}

function formatCloneCostCredits(value: number | null | undefined): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return null
  }
  return `${Math.trunc(value)} 点`
}

/**
 * 映射「发起预览」失败。402 ``smart_preview_reserve_failed`` 的
 * ``detail.skipped_reason`` 才是真正原因（deny_reason 枚举见 reservation service）。
 * 未知 reason 走稳健兜底（不硬耦合穷举，后端新增 reason 不会崩 UI）。
 */
export function mapSmartPreviewCreateError(
  err: unknown,
  options: SmartPreviewCreateErrorOptions = {},
): SmartPreviewCreateError {
  const { status, code, detail } = readGatewayError(err)
  if (status === 401 || code === 'auth_required') {
    return { reason: 'auth_required', message: '请先登录后再试用智能版预览。' }
  }
  if (code === 'smart_consent_invalid' || code === 'smart_paid_clone_confirmation_required') {
    return { reason: 'consent_invalid', message: '同意确认无效，请刷新页面后重试。' }
  }
  if (code === 'smart_disabled' || code === 'reuse_disabled') {
    return { reason: 'disabled', message: '智能版预览暂未开放。' }
  }
  if (code === 'smart_preview_reserve_failed') {
    const skipped =
      detail && typeof detail.skipped_reason === 'string' ? detail.skipped_reason : ''
    switch (skipped) {
      case 'insufficient_credits': {
        const costLabel = formatCloneCostCredits(options.cloneCostCredits)
        return {
          reason: 'insufficient_credits',
          message: costLabel
            ? `余额不足：本次预览需预扣 ${costLabel} 克隆额度，请充值后再试。`
            : '余额不足：本次预览需预扣克隆额度，请充值后再试。',
        }
      }
      case 'voice_library_full':
        return {
          reason: 'voice_library_full',
          message: '你的个人音色库已满，请先到「我的音色」清理后再试。',
        }
      case 'inflight_cap_exceeded':
      case 'daily_cap_exceeded':
        return { reason: 'cap_exceeded', message: '预览通道当前繁忙，请稍后再试。' }
      case 'clone_disabled':
        return { reason: 'disabled', message: '智能版预览暂未开放。' }
      default:
        // duplicate_create / reserve_error / user_not_found / 其它
        return { reason: 'reserve_failed', message: '预览预扣未成功，请稍后重试。' }
    }
  }
  return { reason: 'unknown', message: '发起预览失败，请稍后重试。' }
}

export type SmartPreviewReuseReason =
  | 'upgrade_required' // 决策 A：免费 plan 缺 smart → 渲染升级 CTA
  | 'duration_upgrade' // A 方案：原视频超当前套餐但 ≤ 最高自助套餐 → 升级可解决（→ /pricing）
  | 'duration_over_max' // A 方案：原视频超最高自助套餐 → 升级也没用（用更短视频 / 联系客服）
  | 'disabled' // 转完整 / 智能版 kill-switch 未开放
  | 'auth_required' // 未登录
  | 'preview_unavailable' // 预览不存在 / 无权 / 状态不可复用（need re-generate）
  | 'invalid' // reuse 请求格式错误
  | 'unknown'

export interface SmartPreviewReuseError {
  reason: SmartPreviewReuseReason
  message: string
}

/**
 * 映射「预览转完整」失败。``upgrade_required`` 是决策 A 的可区分信号——UI 据此
 * 渲染「升级 Plus/Pro」CTA（沿用后端给的精准文案，含「复用不重复扣费」说明），
 * 而不是 ``smart_disabled`` 那种误导性的「联系管理员」死路。
 */
export function mapSmartPreviewReuseError(err: unknown): SmartPreviewReuseError {
  const { status, code, message } = readGatewayError(err)
  if (code === 'smart_upgrade_required') {
    return {
      reason: 'upgrade_required',
      message:
        message ?? '转完整智能版需升级到 Plus / Pro 套餐后再试。复用不会重复扣除预览已支付的克隆费用。',
    }
  }
  // A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX）——与 D7 匿名转完整同款两档
  // 可区分 reason（后端 _anon_convert_duration_error_response 把 code 放 body.error、
  // 含具名套餐推荐的友好文案放 body.message）。沿用后端文案；fallback 仅在缺失时兜底，
  // 用**不具名**通用文案，避免再误导买某档（CodeX P1）。
  if (code === 'duration_upgrade_required') {
    return {
      reason: 'duration_upgrade',
      message: message ?? '原视频时长超出当前套餐上限，升级套餐即可处理更长视频。',
    }
  }
  if (code === 'duration_over_max_plan') {
    return {
      reason: 'duration_over_max',
      message: message ?? '原视频时长超过当前最高套餐上限，请改用更短的视频，或联系客服。',
    }
  }
  if (status === 401 || code === 'auth_required') {
    return { reason: 'auth_required', message: '登录已过期，请重新登录后再转完整。' }
  }
  if (code === 'reuse_disabled' || code === 'smart_disabled') {
    return { reason: 'disabled', message: '预览转完整功能暂未开放。' }
  }
  if (code === 'reuse_request_invalid') {
    return { reason: 'invalid', message: '转完整请求无效，请刷新页面后重试。' }
  }
  // preview_not_found(404) / preview_forbidden(403) / preview_*(409 状态不可复用)
  if (
    code === 'preview_not_found' ||
    code === 'preview_forbidden' ||
    code === 'preview_clone_not_captured' ||
    code === 'preview_voice_unavailable' ||
    code === 'preview_source_unavailable' ||
    code === 'preview_reuse_rejected' ||
    status === 404 ||
    status === 409
  ) {
    return {
      reason: 'preview_unavailable',
      message: '该预览已不可用，请重新生成预览后再转完整。',
    }
  }
  return { reason: 'unknown', message: '转完整失败，请稍后重试。' }
}
