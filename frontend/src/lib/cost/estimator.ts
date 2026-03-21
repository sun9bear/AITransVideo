/**
 * API 费用预估模块
 *
 * 根据视频时长和选择的模型/服务，预估各阶段费用。
 * 费用仅供参考，实际费用取决于具体内容和 API 计费方式。
 */

export interface StageEstimate {
  stage: string
  label: string
  provider: string
  model: string
  estimatedCostUsd: number
  estimatedCostCny: number
  note: string
}

export interface CostEstimateResult {
  stages: StageEstimate[]
  totalUsd: number
  totalCny: number
  videoDurationMinutes: number
}

// 汇率（近似）
const USD_TO_CNY = 7.2

// --- 各服务定价 ---

// AssemblyAI: $0.65/小时音频
const ASSEMBLYAI_PER_HOUR_USD = 0.65

// Gemini 模型定价（$/百万 token）
const GEMINI_MODELS: Record<string, { label: string; inputPerMtok: number; outputPerMtok: number }> = {
  'gemini-3.1-flash-lite-preview': { label: 'Gemini 3.1 Flash Lite', inputPerMtok: 0.15, outputPerMtok: 0.60 },
  'gemini-3.1-pro-preview': { label: 'Gemini 3.1 Pro', inputPerMtok: 1.25, outputPerMtok: 10.0 },
}

// Deepseek: ¥1/百万输入 token，¥2/百万输出 token
const DEEPSEEK_INPUT_PER_MTOK_CNY = 1.0
const DEEPSEEK_OUTPUT_PER_MTOK_CNY = 2.0

// MiniMax TTS: 约 ¥0.01/字（粗估）
const MINIMAX_TTS_PER_CHAR_CNY = 0.01

// MiniMax 音色克隆: ¥9.9/次
const MINIMAX_VOICE_CLONE_CNY = 9.9

// --- 预估参数 ---
// 视频转录约产生 ~150 英文单词/分钟 → ~200 token/分钟（输入）
// Gemini 视频理解: ~300 token/秒（高分辨率）→ ~18000 token/分钟
const GEMINI_VIDEO_TOKENS_PER_MINUTE = 18000
const GEMINI_TRANSCRIPT_OUTPUT_TOKENS_PER_MINUTE = 500

// 翻译: 输入约 300 token/分钟（英文+prompt），输出约 400 token/分钟（中文）
const TRANSLATION_INPUT_TOKENS_PER_MINUTE = 800
const TRANSLATION_OUTPUT_TOKENS_PER_MINUTE = 400

// 重写: 大约 30% 的段落需要重写，每次约 200 输入 + 100 输出 token
const REWRITE_RATIO = 0.3
const REWRITE_INPUT_TOKENS_PER_SEGMENT = 200
const REWRITE_OUTPUT_TOKENS_PER_SEGMENT = 100
const SEGMENTS_PER_MINUTE = 4

// TTS: 约 4.5 中文字/秒 → 270 字/分钟
const TTS_CHARS_PER_MINUTE = 270

export function estimateCosts(options: {
  videoDurationMinutes: number
  transcriptionMethod: 'assemblyai' | 'gemini'
  transcriptionModel?: string
  translationModel?: string
  needsVoiceClone?: boolean
  speakerCount?: number
}): CostEstimateResult {
  const {
    videoDurationMinutes,
    transcriptionMethod,
    transcriptionModel = 'gemini-2.5-flash',
    translationModel = 'gemini-3.1-flash-lite-preview',
    needsVoiceClone = false,
    speakerCount = 1,
  } = options

  const stages: StageEstimate[] = []

  // 1. 转录
  if (transcriptionMethod === 'assemblyai') {
    const cost = (videoDurationMinutes / 60) * ASSEMBLYAI_PER_HOUR_USD
    stages.push({
      stage: 'transcription',
      label: '转录',
      provider: 'AssemblyAI',
      model: 'Universal',
      estimatedCostUsd: cost,
      estimatedCostCny: cost * USD_TO_CNY,
      note: '按音频时长计费',
    })
  } else {
    const geminiModel = GEMINI_MODELS[transcriptionModel] ?? GEMINI_MODELS['gemini-2.5-flash']
    const inputTokens = videoDurationMinutes * GEMINI_VIDEO_TOKENS_PER_MINUTE
    const outputTokens = videoDurationMinutes * GEMINI_TRANSCRIPT_OUTPUT_TOKENS_PER_MINUTE
    const cost = (inputTokens / 1_000_000) * geminiModel.inputPerMtok + (outputTokens / 1_000_000) * geminiModel.outputPerMtok
    stages.push({
      stage: 'transcription',
      label: '转录',
      provider: 'Gemini',
      model: geminiModel.label,
      estimatedCostUsd: cost,
      estimatedCostCny: cost * USD_TO_CNY,
      note: '视频 token 按高分辨率估算',
    })
  }

  // 2. 翻译
  const transModel = GEMINI_MODELS[translationModel]
  if (transModel) {
    const inputTokens = videoDurationMinutes * TRANSLATION_INPUT_TOKENS_PER_MINUTE
    const outputTokens = videoDurationMinutes * TRANSLATION_OUTPUT_TOKENS_PER_MINUTE
    const cost = (inputTokens / 1_000_000) * transModel.inputPerMtok + (outputTokens / 1_000_000) * transModel.outputPerMtok
    stages.push({
      stage: 'translation',
      label: '翻译',
      provider: 'Gemini',
      model: transModel.label,
      estimatedCostUsd: cost,
      estimatedCostCny: cost * USD_TO_CNY,
      note: '含 prompt 和上下文 token',
    })
  } else if (translationModel?.includes('deepseek')) {
    const inputTokens = videoDurationMinutes * TRANSLATION_INPUT_TOKENS_PER_MINUTE
    const outputTokens = videoDurationMinutes * TRANSLATION_OUTPUT_TOKENS_PER_MINUTE
    const costCny = (inputTokens / 1_000_000) * DEEPSEEK_INPUT_PER_MTOK_CNY + (outputTokens / 1_000_000) * DEEPSEEK_OUTPUT_PER_MTOK_CNY
    stages.push({
      stage: 'translation',
      label: '翻译',
      provider: 'Deepseek',
      model: 'Deepseek Chat',
      estimatedCostUsd: costCny / USD_TO_CNY,
      estimatedCostCny: costCny,
      note: '按 token 计费（人民币）',
    })
  }

  // 3. 重写（Post-TTS 对齐重写）
  const totalSegments = videoDurationMinutes * SEGMENTS_PER_MINUTE
  const rewriteSegments = Math.ceil(totalSegments * REWRITE_RATIO)
  const rewriteModel = GEMINI_MODELS[translationModel] // 重写和翻译通常用同一模型
  if (rewriteModel && rewriteSegments > 0) {
    const inputTokens = rewriteSegments * REWRITE_INPUT_TOKENS_PER_SEGMENT
    const outputTokens = rewriteSegments * REWRITE_OUTPUT_TOKENS_PER_SEGMENT
    const cost = (inputTokens / 1_000_000) * rewriteModel.inputPerMtok + (outputTokens / 1_000_000) * rewriteModel.outputPerMtok
    stages.push({
      stage: 'rewrite',
      label: '时长对齐重写',
      provider: rewriteModel === transModel ? stages[stages.length - 1]?.provider ?? 'Gemini' : 'Gemini',
      model: rewriteModel.label,
      estimatedCostUsd: cost,
      estimatedCostCny: cost * USD_TO_CNY,
      note: `预计约 ${rewriteSegments} 段需要重写`,
    })
  }

  // 4. TTS
  const totalChars = videoDurationMinutes * TTS_CHARS_PER_MINUTE
  const ttsCostCny = totalChars * MINIMAX_TTS_PER_CHAR_CNY
  stages.push({
    stage: 'tts',
    label: 'TTS 配音',
    provider: 'MiniMax',
    model: 'speech-2.8-turbo',
    estimatedCostUsd: ttsCostCny / USD_TO_CNY,
    estimatedCostCny: ttsCostCny,
    note: `约 ${totalChars} 字`,
  })

  // 5. 音色克隆（如果需要）
  if (needsVoiceClone) {
    const cloneCost = speakerCount * MINIMAX_VOICE_CLONE_CNY
    stages.push({
      stage: 'voice_clone',
      label: '音色克隆',
      provider: 'MiniMax',
      model: '音色克隆',
      estimatedCostUsd: cloneCost / USD_TO_CNY,
      estimatedCostCny: cloneCost,
      note: `${speakerCount} 位说话人 × ¥${MINIMAX_VOICE_CLONE_CNY}/次（已有音色可跳过）`,
    })
  }

  const totalUsd = stages.reduce((sum, s) => sum + s.estimatedCostUsd, 0)
  const totalCny = stages.reduce((sum, s) => sum + s.estimatedCostCny, 0)

  return {
    stages,
    totalUsd,
    totalCny,
    videoDurationMinutes,
  }
}

export function formatCostCny(value: number): string {
  if (value < 0.01) return '< ¥0.01'
  return `¥${value.toFixed(2)}`
}

export function formatCostUsd(value: number): string {
  if (value < 0.01) return '< $0.01'
  return `$${value.toFixed(2)}`
}
