"use client"

import { useEffect, useState } from 'react'
import { toast } from 'sonner'

interface AdminSettings {
  tts_provider: string
  // review_model / translation_model 字段已被 2026-04-09 prompt-model-management 方案的
  // per-prompt + per-mode 模型选择取代（见 admin/prompts 页面）。前端不再持有也不再传递
  // 这两个字段；Gateway AdminSettings Pydantic 模型里仍保留字段以兼容 JSON 回滚。
  skip_translation_config_for_users: boolean
  skip_all_reviews_for_free_users: boolean
  free_user_max_duration_minutes: number
  enable_pre_tts_rewrite: boolean
  express_tts_provider: string
  studio_tts_provider: string
  cosyvoice_runtime_endpoint_mode: string
  cosyvoice_offline_endpoint_mode: string
  translation_char_range_min_factor: number
  translation_char_range_max_factor: number
  // Phase 2 Task 1 — translation-duration-alignment
  tts_speed_adjustment_enabled: boolean
  tts_speed_mode: string
  // Phase 2 force-DSP — when enabled, S5 alignment skips rewrite entirely
  // and always DSP-stretches every TTS segment to the original English
  // duration. Trades quality for guaranteed time alignment.
  force_dsp_alignment: boolean
  // Phase 2 Task 2 — voice match speed dimension (W_SPEED in reranker).
  // When OFF, voice matching ignores target_chars_per_second and uses the
  // legacy 8-dimension persona/age/pitch scoring. Default OFF for canary.
  voice_match_speed_dimension_enabled: boolean
  // Phase D — Whisper subtitle alignment (2026-05-05).
  // Admin master + 3 sub-policy fields. Runtime additionally requires
  // AVT_WHISPER_ALIGN_ENABLED=1 env (ops capability switch).
  whisper_alignment_enabled: boolean
  whisper_alignment_trigger: string  // "publish" | "deliverable" | "manual"
  whisper_alignment_skip_cache: boolean
  whisper_alignment_model: string    // "tiny" | "base" | "small" | "medium" | "large-v3"
  // --- Smart MVP P2 (pre-Phase-3): per-user MiniMax voice library cap ---
  // Drives Smart auto_voice_review's quota water mark (default 30).
  smart_user_voice_clone_cap: number
  // --- Phase 3 (plan 2026-05-17-user-voice-candidate-first) ---
  // Three independent admin policy switches. Default truth-table preserves
  // legacy Smart behavior (allow clone + reuse, ignore weak candidates).
  smart_auto_clone_enabled: boolean
  smart_reuse_user_voice_enabled: boolean
  smart_pause_on_possible_user_voice_match: boolean
  // --- Phase 5 (2026-05-24, P5 data analysis follow-up) ---
  // When True (default), possible (non-strong) personal voice candidates
  // are auto-promoted to REUSED instead of pausing the pipeline. Wins
  // over smart_pause_on_possible_user_voice_match when both are True.
  smart_auto_reuse_on_possible_user_voice_match: boolean
  // --- Smart Auto Pipeline kill switch — Layer 2 (Task #23, P2 blocker #1) ---
  // Admin runtime toggle for the smart kill switch. False = emergency
  // stop active = smart removed from every user's allowed_service_modes
  // even if env-level AVT_ENABLE_SMART_MODE=true. Hot-reloadable (no
  // gateway restart needed).
  smart_mode_enabled: boolean
  // --- Phase 4.2 A.2c / D.1: CosyVoice clone full admin field set ---
  // **D.1 PR #13 Codex P1 修订（2026-05-27）**：``POST /api/admin/settings``
  // 是 full-body replace 语义 —— 用户翻 GA toggle 时整页 settings 会被
  // ``JSON.stringify(settings)`` 上传。如果其它 5 个 ``cosyvoice_clone_*``
  // 字段不在前端 state，保存会让 backend Pydantic 走默认值 —— 等于"翻 GA
  // 顺手关掉 clone worker / 擦掉 beta allowlist"。所以这里必须**显式列出
  // 全部 6 个**字段，``DEFAULT_SETTINGS`` 也必须和 ``gateway/admin_settings.py``
  // Pydantic 默认值严格一致（守卫 ``test_phase42_d1_admin_settings_ui_guard.py``）。
  //
  // 字段语义（详细注释见 ``gateway/admin_settings.py:194-230``）：
  // - ``worker_enabled``：runtime 总开关（武汉 worker reachable）。
  // - ``default_target_model``：DashScope model id（flash / plus）。
  // - ``user_allowlist``：admin 之外可见 clone 入口的 user_id 列表（beta）。
  // - ``general_availability_enabled``：D.1 新增 GA toggle，全用户开放。
  // - ``max_voices_per_user``：每用户克隆数硬上限（C.2 已生效）。
  // - ``max_concurrent_jobs``：并发上限（Phase 4.2 占位，未生效）。
  //
  // 前端 D.1 只渲染 GA toggle UI；其它 5 个字段进 state 但不渲染入口（防止
  // 误操作 worker_enabled / allowlist 这种核弹按钮；将来需要 admin UI 暴露
  // 单独的 toggle 再加渲染）。后端 Pydantic ``StrictBool`` /
  // ``_resolve_clone_gate`` 仍是唯一安全边界。
  cosyvoice_clone_worker_enabled: boolean
  cosyvoice_clone_default_target_model: string
  cosyvoice_clone_user_allowlist: string[]
  cosyvoice_clone_general_availability_enabled: boolean
  cosyvoice_clone_max_voices_per_user: number
  cosyvoice_clone_max_concurrent_jobs: number
  // --- Phase 4.3a Express CosyVoice 自动 clone canary (2026-05-28) ---
  // spec §8.1: full-body POST 语义同 Phase 4.2 D.1 — 全部 8 个字段必须
  // 进 state，否则 ``cosyvoice_clone_general_availability_enabled`` toggle
  // 一保存就会把后端这 8 个字段静默重置为 Pydantic 默认。
  //
  // 字段语义（详见 gateway/admin_settings.py 中 Phase 4.3a 段）：
  // - ``enabled``: Layer 1 主开关，StrictBool；默认 false（fail-safe）
  // - ``user_allowlist``: Layer 3 灰度白名单（user_id UUID strings）
  // - ``main_speaker_min_ratio`` / ``min_lines``: pipeline 主说话人筛选阈值
  // - ``sample_max_seconds``: 样本拼接 cap，[10, 60] 秒
  // - ``target_model``: 固定 cosyvoice-v3.5-flash（spec §1.1 G1）
  // - ``per_user_daily_cap`` / ``per_user_active_temp_cap``: 成本闸
  //
  // 渲染：只暴露主开关 toggle；其它 7 个进 state 但不渲染入口（防止
  // canary 期间误操作；将来 Phase 4.3 全量时再加 UI）。
  express_cosyvoice_auto_clone_enabled: boolean
  express_cosyvoice_auto_clone_user_allowlist: string[]
  express_cosyvoice_auto_clone_main_speaker_min_ratio: number
  express_cosyvoice_auto_clone_main_speaker_min_lines: number
  express_cosyvoice_auto_clone_sample_max_seconds: number
  express_cosyvoice_auto_clone_target_model: string
  express_cosyvoice_auto_clone_per_user_daily_cap: number
  express_cosyvoice_auto_clone_per_user_active_temp_cap: number
}

const DEFAULT_SETTINGS: AdminSettings = {
  tts_provider: 'minimax',
  skip_translation_config_for_users: true,
  skip_all_reviews_for_free_users: true,
  enable_pre_tts_rewrite: true,
  free_user_max_duration_minutes: 10,
  express_tts_provider: 'cosyvoice',
  studio_tts_provider: 'minimax',
  cosyvoice_runtime_endpoint_mode: 'international',
  cosyvoice_offline_endpoint_mode: 'mainland',
  translation_char_range_min_factor: 0.85,
  translation_char_range_max_factor: 1.15,
  tts_speed_adjustment_enabled: false,
  tts_speed_mode: 'default',
  force_dsp_alignment: false,
  voice_match_speed_dimension_enabled: false,
  whisper_alignment_enabled: false,
  whisper_alignment_trigger: 'deliverable',
  whisper_alignment_skip_cache: false,
  whisper_alignment_model: 'small',
  smart_user_voice_clone_cap: 30,
  smart_auto_clone_enabled: true,
  smart_reuse_user_voice_enabled: true,
  smart_pause_on_possible_user_voice_match: false,
  smart_auto_reuse_on_possible_user_voice_match: true,
  smart_mode_enabled: false,
  // --- Phase 4.2 D.1 — CosyVoice clone 全部 6 个 admin 字段（PR #13 Codex P1）---
  // 必须与 ``gateway/admin_settings.py`` Pydantic 默认值严格一致：
  //   worker_enabled                       = False
  //   default_target_model                 = "cosyvoice-v3.5-flash"
  //   user_allowlist                       = []
  //   general_availability_enabled         = False
  //   max_voices_per_user                  = 3
  //   max_concurrent_jobs                  = 2
  // 否则用户翻 GA toggle 时 full-body save 会把后端这些字段重置。
  cosyvoice_clone_worker_enabled: false,
  cosyvoice_clone_default_target_model: 'cosyvoice-v3.5-flash',
  cosyvoice_clone_user_allowlist: [],
  cosyvoice_clone_general_availability_enabled: false,
  cosyvoice_clone_max_voices_per_user: 3,
  cosyvoice_clone_max_concurrent_jobs: 2,
  // --- Phase 4.3a Express CosyVoice 自动 clone canary 默认值（2026-05-28）---
  // 必须与 ``gateway/admin_settings.py`` Pydantic 默认值严格一致：
  //   enabled                       = False
  //   user_allowlist                = []
  //   main_speaker_min_ratio        = 0.30
  //   main_speaker_min_lines        = 5
  //   sample_max_seconds            = 20.0
  //   target_model                  = "cosyvoice-v3.5-flash"
  //   per_user_daily_cap            = 5
  //   per_user_active_temp_cap      = 3
  // 否则用户翻 Phase 4.2 GA toggle 时 full-body save 会把后端这 8 个字段重置。
  express_cosyvoice_auto_clone_enabled: false,
  express_cosyvoice_auto_clone_user_allowlist: [],
  express_cosyvoice_auto_clone_main_speaker_min_ratio: 0.30,
  express_cosyvoice_auto_clone_main_speaker_min_lines: 5,
  express_cosyvoice_auto_clone_sample_max_seconds: 20.0,
  express_cosyvoice_auto_clone_target_model: 'cosyvoice-v3.5-flash',
  express_cosyvoice_auto_clone_per_user_daily_cap: 5,
  express_cosyvoice_auto_clone_per_user_active_temp_cap: 3,
}

const WHISPER_TRIGGER_OPTIONS = [
  {
    value: 'deliverable',
    label: '仅在交付时（默认）',
    description: '用户点击「生成剪映草稿」/「打包素材」且勾选了字幕时才执行；publish 阶段不动，速度最快',
  },
  {
    value: 'publish',
    label: '每次发布都做',
    description: '每个任务在 publish 阶段都跑一次 Whisper；首次交付字幕已是精准对齐，但 publish 多约 5-15s',
  },
  {
    value: 'manual',
    label: '只手动触发',
    description: '关闭所有自动入口；只能由管理员通过专门接口触发（适合调试或排障）',
  },
]

const WHISPER_MODEL_OPTIONS = [
  {
    value: 'tiny',
    label: 'tiny（~75MB）',
    description: '最小最快，约 0.5× 实时，仅作冒烟用，中文 ASR 精度有限',
  },
  {
    value: 'base',
    label: 'base（~150MB）',
    description: '比 tiny 准但更慢，约 1× 实时',
  },
  {
    value: 'small',
    label: 'small（~466MB，推荐）',
    description: '精度/速度折中，约 3× 实时；线上对比试运行后选定的默认值',
  },
  {
    value: 'medium',
    label: 'medium（~1.5GB）',
    description: '更高精度，约 6× 实时；适合长视频字幕工作流（峰值 RAM ~3GB）',
  },
  {
    value: 'large-v3',
    label: 'large-v3（~3GB）',
    description: '最高精度，约 10× 实时；建议有 GPU 才启用',
  },
]

const TTS_SPEED_MODE_OPTIONS = [
  { value: 'default',    label: '默认 ±8%',  description: '限幅 [0.92, 1.08]，听感无损（推荐）' },
  { value: 'aggressive', label: '激进 ±15%', description: '限幅 [0.85, 1.15]，更大的对齐能力但可能有轻微失真' },
  { value: 'extreme',    label: '极端 ±30%', description: '限幅 [0.70, 1.30]，明显的快/慢但仍可辨识，适合实验性数据' },
  { value: 'unlimited',  label: '无限制 ±50%', description: '限幅 [0.50, 2.00]，仅 MiniMax API 硬边界，边缘明显失真' },
]

const TTS_OPTIONS = [
  { value: 'minimax', label: 'MiniMax Speech 2.8', description: '成熟稳定，¥0.20/千字，RPM 20' },
  { value: 'mimo', label: 'MiMo-V2-TTS（小米）', description: '限时免费，RPM 100，自然语言风格控制' },
  { value: 'volcengine', label: '豆包语音合成 2.0（V3）', description: '火山引擎 V3 流式接口，300+音色，¥3/万字符，自动情感表达' },
]

const EXPRESS_TTS_OPTIONS = [
  { value: 'cosyvoice', label: 'CosyVoice', description: '阿里通义，快速合成，适合快捷版' },
  { value: 'mimo', label: 'MiMo-V2-TTS（小米）', description: '限时免费，RPM 100，自然语言风格控制' },
  { value: 'volcengine', label: '豆包语音合成 1.0', description: '火山引擎 V3 流式接口，100+音色，¥5/万字符' },
]

const STUDIO_TTS_OPTIONS = [
  { value: 'minimax', label: 'MiniMax Speech 2.8', description: '成熟稳定，¥0.20/千字，支持音色克隆' },
  { value: 'cosyvoice', label: 'CosyVoice（阿里百炼）', description: '阿里百炼 v3-flash，~60中文音色，¥2/万字符' },
  { value: 'volcengine', label: '豆包语音合成 2.0（V3）', description: '火山引擎 V3 流式接口，300+音色，¥3/万字符，自动情感表达' },
]

const COSYVOICE_ENDPOINT_OPTIONS = [
  { value: 'international', label: '国际端点', description: '新加坡节点，延迟低（1-2s），支持 10 个核心音色' },
  { value: 'mainland', label: '国内端点', description: '中国大陆节点，延迟较高（3-7s），支持全部 59 个音色' },
]

// REVIEW_OPTIONS and TRANSLATION_OPTIONS removed — model selection moved to /admin/prompts

export default function AdminSettingsPage() {
  const [settings, setSettings] = useState<AdminSettings>(DEFAULT_SETTINGS)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    fetch('/api/admin/settings', { credentials: 'include' })
      .then(async (resp) => {
        if (resp.status === 403) {
          setForbidden(true)
          return
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const data = await resp.json()
        // Gateway returns { settings: {...} } envelope
        const payload = data?.settings ?? data ?? {}
        setSettings({ ...DEFAULT_SETTINGS, ...payload })
      })
      .catch((err) => setError(`加载设置失败: ${err.message}`))
      .finally(() => setIsLoading(false))
  }, [])

  const handleSave = async () => {
    setIsSaving(true)
    setError(null)
    try {
      const resp = await fetch('/api/admin/settings', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: '保存失败' }))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }
      toast.success('设置已保存')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '保存失败'
      setError(msg)
      toast.error(msg)
    } finally {
      setIsSaving(false)
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    )
  }

  if (forbidden) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <div className="rounded-2xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 p-6 text-center">
          <p className="text-lg font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="text-sm text-muted-foreground mt-2">您没有权限查看此页面。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold font-heading text-foreground">系统设置</h1>
        <p className="text-sm text-muted-foreground mt-1">
          管理翻译流程的全局配置
        </p>
      </div>

      {error ? (
        <div className="rounded-xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 p-4 text-sm text-[color:var(--cinnabar)]">
          {error}
        </div>
      ) : null}

      {/* TTS Provider */}
      <SettingSection
        title="语音合成（TTS）引擎"
        description="选择配音阶段使用的 TTS 服务"
      >
        <RadioGroup
          options={TTS_OPTIONS}
          value={settings.tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, tts_provider: v }))}
          name="tts_provider"
        />
      </SettingSection>

      {/* Express TTS Provider */}
      <SettingSection
        title="快捷版 TTS 引擎"
        description="快捷版（express）任务使用的语音合成服务"
      >
        <RadioGroup
          options={EXPRESS_TTS_OPTIONS}
          value={settings.express_tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, express_tts_provider: v }))}
          name="express_tts_provider"
        />
      </SettingSection>

      {/* Studio TTS Provider */}
      <SettingSection
        title="工作台版 TTS 引擎"
        description="工作台版（Studio）任务使用的语音合成服务"
      >
        <RadioGroup
          options={STUDIO_TTS_OPTIONS}
          value={settings.studio_tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, studio_tts_provider: v }))}
          name="studio_tts_provider"
        />
      </SettingSection>

      {/* Model management — moved to prompts page */}
      <SettingSection
        title="大模型配置"
        description="审校模型和翻译模型现已移至「提示词管理」页面，支持工作台版/快捷版独立配置"
      >
        <div className="rounded-xl border border-border bg-muted/30 p-4">
          <a
            href="/admin/prompts"
            className="text-sm text-primary hover:underline"
          >
            前往「提示词 & 模型管理」→
          </a>
        </div>
      </SettingSection>

      {/* Skip translation config */}
      <SettingSection
        title="流程简化"
        description="普通用户的视频翻译流程设置"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.skip_translation_config_for_users}
            onChange={(e) => setSettings((s) => ({ ...s, skip_translation_config_for_users: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">普通用户跳过翻译配置阶段</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，普通用户不会看到翻译配置审核步骤，直接使用上面设置的默认翻译模型。
            </p>
          </div>
        </label>
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.skip_all_reviews_for_free_users}
            onChange={(e) => setSettings((s) => ({ ...s, skip_all_reviews_for_free_users: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">免费用户跳过所有审核</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，免费用户的任务将自动跳过所有人工审核步骤（说话人、翻译、语音），全流程自动完成。
            </p>
          </div>
        </label>
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.enable_pre_tts_rewrite}
            onChange={(e) => setSettings((s) => ({ ...s, enable_pre_tts_rewrite: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">Pre-TTS 预重写</p>
            <p className="text-xs text-muted-foreground mt-1">
              TTS 生成前预估时长并重写不匹配的译文。关闭后由 TTS 后的对齐阶段统一处理，适合语速差异大的场景。
            </p>
          </div>
        </label>

        {/* Phase 2 Task 1 — translation-duration-alignment */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.tts_speed_adjustment_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, tts_speed_adjustment_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              TTS 语速微调
              <span className="ml-2 inline-block rounded bg-[color:var(--ochre)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--ochre)]">
                Phase 2 · MiniMax
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，MiniMax TTS 每段会根据预估 vs 目标时长自动调整 voice_setting.speed（限幅内），消化小幅误差、减少 S5 rewrite。
              CosyVoice / VolcEngine 暂未接入，保持 1.0。
            </p>
            {settings.tts_speed_adjustment_enabled && (
              <div className="mt-2 flex gap-2">
                {TTS_SPEED_MODE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={(e) => { e.preventDefault(); setSettings((s) => ({ ...s, tts_speed_mode: opt.value })) }}
                    className={`rounded-lg border px-2.5 py-1 text-xs transition ${
                      settings.tts_speed_mode === opt.value
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border bg-muted/30 text-muted-foreground hover:bg-muted/50'
                    }`}
                    title={opt.description}
                  >
                    {opt.label}
                  </button>
                ))}
                <span className="text-[11px] text-muted-foreground self-center ml-1">
                  {TTS_SPEED_MODE_OPTIONS.find((o) => o.value === settings.tts_speed_mode)?.description || ''}
                </span>
              </div>
            )}
          </div>
        </label>

        {/* Phase 2 force-DSP override */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.force_dsp_alignment}
            onChange={(e) => setSettings((s) => ({ ...s, force_dsp_alignment: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              强制 DSP 对齐
              <span className="ml-2 inline-block rounded bg-[color:var(--ochre)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--ochre)]">
                Phase 2 · 极端兜底
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，<b>S5 对齐阶段跳过 rewrite</b>，所有 TTS 段直接 DSP 拉伸/压缩到原英文时长。
              一定能对齐，但语速差异大的段落会有 atempo 拉伸失真。适合“宁可有点怪也要严格同步”的内容
              （字幕同步/口型对齐），或者上游翻译字数控制不可靠时的兜底方案。
              <br />
              <span className="text-[color:var(--ochre)]">注意：开启后 rewrite 完全不生效，需复核率会变成 0%（因为强制对齐了），但音质需主观判断。</span>
            </p>
          </div>
        </label>

        {/* Phase 2 Task 2 — voice match speed dimension (灰度) */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.voice_match_speed_dimension_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, voice_match_speed_dimension_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              音色匹配启用语速维度
              <span className="ml-2 inline-block rounded px-1.5 py-0.5 text-[10px] bg-[color:var(--cinnabar)]/15 text-[color:var(--cinnabar)]">
                Phase 2 Task 2 · 灰度
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，音色匹配 reranker 加入 W_SPEED 自适应权重 (0.05–0.30)：根据 speaker 目标 cps 与库基线 (4.20) 偏离度，
              把语速接近的音色推到推荐列表前列。<b>极端语速 speaker</b>（Munger 慢节奏 / 快 podcast）效果显著，
              普通 speaker 行为不变。关闭时回退到原 8 维度评分。
              <br />
              <span className="text-[color:var(--ochre)]">默认关闭：建议先观察 metrics（speed_param_distribution + first_pass_error_pct）一段真实数据再启用。</span>
            </p>
          </div>
        </label>
      </SettingSection>

      {/* Smart kill switch — Task #23, P2 launch blocker #1.
          The master ON/OFF for the entire Smart pipeline. When OFF,
          every user (including admin) has smart removed from their
          allowed_service_modes — creation API returns smart_disabled.
          Separated from the per-strategy section below since this is
          a different concern (whole-feature toggle vs voice strategy). */}
      <SettingSection
        title="智能版总开关"
        description="智能版（Smart Auto Pipeline）的运行时总开关，与环境变量 AVT_ENABLE_SMART_MODE 双层 AND。任一关闭 → 所有用户（含管理员）无法创建智能版任务。建议保持开启，需要紧急关停时切换。"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.smart_mode_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, smart_mode_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              开启智能版（Smart Mode）
              <span className="ml-2 inline-block rounded bg-[color:var(--cinnabar)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--cinnabar)]">
                Kill switch · 默认关闭
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后（需同时设置 <code className="font-mono">AVT_ENABLE_SMART_MODE=true</code>），
              智能版会出现在用户的 <code className="font-mono">allowed_service_modes</code> 列表里，
              所有创建智能版任务的 API 调用通过。
              <strong className="text-[color:var(--cinnabar)]">保存后对新建请求立即生效</strong>（gateway
              每次 API 调用重读 admin_settings；无 mtime poll 延迟），
              所有人（包括管理员）创建智能版任务会返回 <code className="font-mono">smart_disabled</code> 403。
              已在跑的任务不会被中断。
            </p>
          </div>
        </label>
      </SettingSection>

      {/* Phase 3 (plan 2026-05-17): Smart 个人音色策略 */}
      <SettingSection
        title="智能版个人音色策略"
        description="控制智能版自动决策时如何使用用户的个人音色库（克隆音色）"
      >
        {/* Toggle 1: smart_reuse_user_voice_enabled */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.smart_reuse_user_voice_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, smart_reuse_user_voice_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">允许智能版复用已有个人音色</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后（默认），智能版会先查询用户的个人音色库，命中同源强匹配时直接复用，不调用克隆 provider、不扣克隆点数。
              关闭后，智能版不查询个人音色，只走新克隆或官方音色。
            </p>
          </div>
        </label>

        {/* Toggle 2: smart_auto_clone_enabled */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.smart_auto_clone_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, smart_auto_clone_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">允许智能版自动新克隆音色</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后（默认），智能版在用户同意（smart_consent.auto_voice_clone=True）的前提下，可自动为主说话人克隆新音色。
              关闭后只禁止新克隆，强匹配复用仍然生效——例如 MiniMax 账户余额接近上限时建议关闭。
            </p>
          </div>
        </label>

        {/* Toggle 3 (P5 follow-up, 2026-05-24):
            smart_auto_reuse_on_possible_user_voice_match. Wins over Toggle 4
            below when both are enabled — this is the "stop pausing" fix. */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.smart_auto_reuse_on_possible_user_voice_match}
            onChange={(e) => setSettings((s) => ({ ...s, smart_auto_reuse_on_possible_user_voice_match: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              弱匹配自动复用
              <span className="ml-2 inline-block rounded bg-primary/20 px-1.5 py-0.5 text-[10px] text-primary">
                Phase 5 · 默认开启
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后（默认），智能版任务遇到“可能匹配”的个人音色时，
              <strong>自动选 score 最高的那个直接复用</strong>，不调用克隆 provider、不打断流程。
              用户事后不满意可在编辑页改回去。优先级高于下方“弱匹配确认模式” —— 两者都开时以本项为准。
              关闭后回退到 Phase 4 行为（看下方开关决定暂停或忽略）。
            </p>
          </div>
        </label>

        {/* Toggle 4: smart_pause_on_possible_user_voice_match */}
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.smart_pause_on_possible_user_voice_match}
            onChange={(e) => setSettings((s) => ({ ...s, smart_pause_on_possible_user_voice_match: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              弱匹配确认模式
              <span className="ml-2 inline-block rounded bg-[color:var(--ochre)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--ochre)]">
                Phase 4 · 默认关闭
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，智能版任务遇到“可能匹配”的个人音色（同视频同名但 speaker_id 不一致、或跨视频同名）时，
              会暂停到音色审核页面，等用户确认是否复用。
              <strong className="text-[color:var(--ochre)]">提醒：开启后所有提交智能版的用户会在提交页看到警示</strong>，
              他们的任务可能不会全自动跑完。默认关闭以避免破坏“智能版=全自动”的产品预期。
              <br />
              <span className="text-[11px] text-muted-foreground/80">
                注：当上方“弱匹配自动复用”同时开启时，本项被忽略。
              </span>
            </p>
          </div>
        </label>

        {/* Number input: smart_user_voice_clone_cap */}
        <label className="flex flex-col gap-2 rounded-xl border border-border bg-muted/30 p-4">
          <div>
            <p className="text-sm font-medium text-foreground">个人音色库每用户上限</p>
            <p className="text-xs text-muted-foreground mt-1">
              单个用户在 MiniMax 账户里最多保留多少个克隆音色。智能版自动克隆前会查 user_voices 表中该用户的非过期行数，
              距离上限 ≤ 3（安全水位线）时不再自动新克隆，引导用户先清理音色库。
              默认 30，建议 5-200 之间。
            </p>
          </div>
          <input
            type="number"
            min={1}
            max={500}
            step={1}
            value={settings.smart_user_voice_clone_cap}
            onChange={(e) => {
              const v = parseInt(e.target.value, 10)
              if (Number.isFinite(v) && v >= 1 && v <= 500) {
                setSettings((s) => ({ ...s, smart_user_voice_clone_cap: v }))
              }
            }}
            className="w-32 rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
          />
        </label>
      </SettingSection>

      {/* Phase D — Whisper 字幕时间对齐 */}
      <SettingSection
        title="Whisper 字幕时间对齐"
        description="使用 faster-whisper 重新对齐字幕显示时间（不动文本，只动 cue 起止毫秒），让字幕与配音音频严格同步。需要服务端启用了 AVT_WHISPER_ALIGN_ENABLED=1 环境变量才能真正生效。"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.whisper_alignment_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, whisper_alignment_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              启用 Whisper 字幕时间对齐
              <span className="ml-2 inline-block rounded px-1.5 py-0.5 text-[10px] bg-[color:var(--ochre)]/20 text-[color:var(--ochre)]">
                Phase D · 受控启用
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              关闭时使用原有的「按字数比例」分配字幕时间（默认）。开启后将按下方触发策略调用 Whisper 字符级时间戳，
              替换 cue 的起止时间，但<b>字幕文字本身不变</b>。
              <br />
              <span className="text-muted-foreground/80">即使本开关打开，若 ops 没有设置 AVT_WHISPER_ALIGN_ENABLED=1，运行时仍走 fallback。</span>
            </p>
          </div>
        </label>

        {settings.whisper_alignment_enabled && (
          <>
            <div className="rounded-xl border border-border bg-muted/30 p-4">
              <p className="text-sm font-medium text-foreground mb-2">触发时机</p>
              <p className="text-xs text-muted-foreground mb-3">
                决定 Whisper 在哪些环节自动跑。<b>「仅在交付时」</b> 是推荐默认：用户没勾字幕的视频不浪费算力；
                第一次交付字幕的等待 ~10 分钟（小模型，38 分钟音频），结果会缓存，再次交付秒级。
              </p>
              <RadioGroup
                options={WHISPER_TRIGGER_OPTIONS}
                value={settings.whisper_alignment_trigger}
                onChange={(v) => setSettings((s) => ({ ...s, whisper_alignment_trigger: v }))}
                name="whisper_alignment_trigger"
              />
            </div>

            <div className="rounded-xl border border-border bg-muted/30 p-4">
              <p className="text-sm font-medium text-foreground mb-2">模型尺寸</p>
              <p className="text-xs text-muted-foreground mb-3">
                faster-whisper 的模型档位。线上默认 <code className="text-foreground">small</code>（~3× 实时，CN ASR 精度足够）；
                短视频或需要绝对精度可考虑 <code className="text-foreground">medium</code>，但 RAM/时间成本翻倍。
                切换模型会触发<b>缓存重建</b>（不同模型的 cache key 不同）。
              </p>
              <RadioGroup
                options={WHISPER_MODEL_OPTIONS}
                value={settings.whisper_alignment_model}
                onChange={(v) => setSettings((s) => ({ ...s, whisper_alignment_model: v }))}
                name="whisper_alignment_model"
              />
            </div>

            <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
              <input
                type="checkbox"
                checked={settings.whisper_alignment_skip_cache}
                onChange={(e) => setSettings((s) => ({ ...s, whisper_alignment_skip_cache: e.target.checked }))}
                className="h-4 w-4 rounded border-border"
              />
              <div>
                <p className="text-sm font-medium text-foreground">强制跳过缓存（每次重新转录）</p>
                <p className="text-xs text-muted-foreground mt-1">
                  默认关闭：相同 WAV 内容只跑一次，结果存在 <code className="text-foreground">{'{wav}.whisper_<model>_zh.json'}</code> 旁。
                  <b>开启会显著拖慢每次交付</b>（相当于回到首次的 ~10 分钟）；只在排障 / 验证模型升级 / 怀疑缓存被误用时短期开启。
                  开启时仍会重写新的缓存，关闭后立即生效。
                </p>
              </div>
            </label>
          </>
        )}
      </SettingSection>

      {/* Free user limits */}
      <SettingSection title="免费用户限制" description="控制免费用户的使用配额。">
        <div className="rounded-xl border border-border bg-muted/30 p-4 space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">最大视频时长</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={1}
                max={180}
                step={1}
                value={settings.free_user_max_duration_minutes}
                onChange={(e) => setSettings((s) => ({ ...s, free_user_max_duration_minutes: Number(e.target.value) || 10 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-sm text-muted-foreground">分钟</span>
          </div>
          <p className="text-xs text-muted-foreground">
            超过此时长的视频将被拒绝处理。付费用户不受此限制。
          </p>
        </div>
      </SettingSection>

      {/* Translation Char Range */}
      <SettingSection
        title="翻译字数范围"
        description="控制翻译阶段 min_chars / max_chars 的计算范围。probe 校准后 target_chars 已精准，此范围影响 LLM 翻译的字数容差和重试判断。"
      >
        <div className="rounded-xl border border-border bg-muted/30 p-4 space-y-4">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">下限系数</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={0.5}
                max={1.0}
                step={0.05}
                value={settings.translation_char_range_min_factor}
                onChange={(e) => setSettings((s) => ({ ...s, translation_char_range_min_factor: Number(e.target.value) || 0.85 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-xs text-muted-foreground">min_chars = target_chars × {settings.translation_char_range_min_factor}</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">上限系数</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={1.0}
                max={2.0}
                step={0.05}
                value={settings.translation_char_range_max_factor}
                onChange={(e) => setSettings((s) => ({ ...s, translation_char_range_max_factor: Number(e.target.value) || 1.15 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-xs text-muted-foreground">max_chars = target_chars × {settings.translation_char_range_max_factor}</span>
          </div>
          <p className="text-xs text-muted-foreground">
            范围越窄（如 0.90-1.10），翻译字数越精准但重试次数可能增加；范围越宽（如 0.80-1.20），翻译更宽松但配音时长匹配度降低。
          </p>
        </div>
      </SettingSection>

      {/* CosyVoice Endpoint Settings */}
      <SettingSection
        title="CosyVoice 端点配置"
        description="控制 CosyVoice 语音合成的服务端点。运行时端点影响实际配音生成，离线端点用于音色分析和建库。"
      >
        <div className="space-y-4">
          <div>
            <p className="text-sm font-medium text-foreground mb-2">运行时端点（Runtime）</p>
            <p className="text-xs text-muted-foreground mb-3">
              用于 express / CosyVoice 实际生产调用。默认国际端点，延迟低但音色覆盖有限。
            </p>
            <RadioGroup
              options={COSYVOICE_ENDPOINT_OPTIONS}
              value={settings.cosyvoice_runtime_endpoint_mode}
              onChange={(v) => setSettings((s) => ({ ...s, cosyvoice_runtime_endpoint_mode: v }))}
              name="cosyvoice_runtime_endpoint"
            />
          </div>
          <div className="border-t border-border pt-4">
            <p className="text-sm font-medium text-foreground mb-2">离线端点（Offline）</p>
            <p className="text-xs text-muted-foreground mb-3">
              用于 calibration / profiling / 离线建库。默认国内端点，音色覆盖完整。
            </p>
            <RadioGroup
              options={COSYVOICE_ENDPOINT_OPTIONS}
              value={settings.cosyvoice_offline_endpoint_mode}
              onChange={(v) => setSettings((s) => ({ ...s, cosyvoice_offline_endpoint_mode: v }))}
              name="cosyvoice_offline_endpoint"
            />
          </div>
        </div>
      </SettingSection>

      {/* Phase 4.2 D.1 (plan 2026-05-19): CosyVoice 克隆 General Availability gate.
          ⚠️ 付费 API 硬约束：CosyVoice clone 是按调用计费的外部 API（DashScope 阿里云）。
          默认关闭 → 只有 admin / allowlist 用户能触发克隆；打开后所有已登录用户都能看到
          克隆入口（仍需用户显式点击触发，不会自动调用）。
          Gateway 后端用 Pydantic StrictBool 字段防止 admin UI 误传字符串 "1"/"on"/"yes"
          被当成 true。保存仍是整体 JSON.stringify(settings)，不会丢字段。*/}
      <SettingSection
        title="CosyVoice 克隆全用户开放"
        description="控制是否对所有已登录用户开放 CosyVoice 音色克隆入口。关闭时仅 admin 和 allowlist 用户可见；打开后任何登录用户都可显式点击克隆按钮（仍需用户主动触发，不会被自动调用）。"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.cosyvoice_clone_general_availability_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, cosyvoice_clone_general_availability_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              CosyVoice 克隆全用户开放
              <span className="ml-2 inline-block rounded bg-[color:var(--cinnabar)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--cinnabar)]">
                付费 API · 默认关闭
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              <strong className="text-[color:var(--cinnabar)]">⚠️ 打开后所有已登录用户都可触发付费 API</strong>
              （CosyVoice 克隆调用 DashScope 阿里云，按次计费）。
              关闭时仅 <code className="font-mono">admin</code> + allowlist 用户能看到克隆按钮。
              保存后 gateway 每次调用重读 admin_settings 立即生效，已在跑的克隆任务不会被中断。
              此开关只控制<strong>授权可见性</strong>，前端按钮还会再 AND <code className="font-mono">provider.supportsClone</code>。
            </p>
          </div>
        </label>
      </SettingSection>

      {/* Phase 4.3a (plan 2026-05-28): Express 快捷版自动 CosyVoice 克隆 canary 主开关。
          ⚠️ 付费 API 硬约束：触发后会调武汉 worker → DashScope CosyVoice，按次计费。
          默认关闭 → 即使用户在 allowlist 也不会触发；打开后**仅 allowlist + admin**
          能在 Express 任务里触发自动克隆（spec §2 Layer 1 + Layer 3 双门控）。
          其它 7 个 hidden 字段（allowlist / 阈值 / cap / target_model）通过 full-body
          save 透传，不在本 toggle UI 里渲染入口。 */}
      <SettingSection
        title="Express 快捷版自动克隆 (canary)"
        description="Phase 4.3a 灰度功能：在 Express 任务里自动从主说话人挑 10-20s 样本，克隆 cosyvoice-v3.5-flash 临时音色注入到 TTS 路径。默认关闭；打开后仅 allowlist 用户和 admin 能触发（仍需用户在提交页显式勾选 consent，绝不静默调用）。"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.express_cosyvoice_auto_clone_enabled}
            onChange={(e) => setSettings((s) => ({ ...s, express_cosyvoice_auto_clone_enabled: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">
              启用 Express 自动克隆
              <span className="ml-2 inline-block rounded bg-[color:var(--cinnabar)]/20 px-1.5 py-0.5 text-[10px] text-[color:var(--cinnabar)]">
                付费 API · canary · 默认关闭
              </span>
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              <strong className="text-[color:var(--cinnabar)]">⚠️ Phase 4.3a 灰度路径，仅 allowlist 用户能触发。</strong>
              触发条件 5 层 AND：admin 开关 + worker env + allowlist + 用户 consent +
              成本闸（每用户每日 5 次 / 当前活跃临时音色 3 个）。
              其它 7 个字段（allowlist / 阈值 / cap）通过 admin_settings.json 持久化，本页保持后端 Pydantic 默认值，
              如需修改请直接编辑 <code className="font-mono">admin_settings.json</code> 或后续 Phase 4.3b 升级 UI。
              失败任意一层 = 走 CosyVoice 预设音色（与当前 Express 行为一致）。
            </p>
          </div>
        </label>
      </SettingSection>

      {/* Save button */}
      <div className="flex gap-3 pt-4 border-t border-border">
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={isSaving}
          onClick={() => { void handleSave() }}
          type="button"
        >
          {isSaving ? '保存中…' : '保存设置'}
        </button>
        <button
          className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/50"
          // PR #13 Codex P2 fix（discussion_r3308167865，2026-05-27 二轮复审）：
          // D.1 把全部 6 个 ``cosyvoice_clone_*`` 字段塞进 page state，但**只渲染
          // GA toggle**。如果"恢复默认"直接 setSettings(DEFAULT_SETTINGS)，会把
          // 当前生产环境**正在生效**的 worker_enabled / allowlist /
          // max_voices_per_user / max_concurrent_jobs / default_target_model
          // 全部覆盖为默认值；下次点"保存设置"时 full-body POST 会把这些值
          // 写回后端 —— 等于"点恢复默认 + 保存"会静默关克隆 worker / 擦 beta。
          //
          // 修复方案：reset 透传**5 个 hidden 字段**，**不**透传 visible GA toggle
          // —— GA 是用户能看到的可见控件，"恢复默认"语义就是让它回 fail-safe
          // 默认 ``false``。如果连 GA 都透传，"恢复默认"对 admin 视觉上无效，
          // 也无法用它紧急关闭误打开的 GA。
          //
          // 透传白名单（5 个 hidden）：
          //   - cosyvoice_clone_worker_enabled
          //   - cosyvoice_clone_default_target_model
          //   - cosyvoice_clone_user_allowlist
          //   - cosyvoice_clone_max_voices_per_user
          //   - cosyvoice_clone_max_concurrent_jobs
          // 重置白名单（1 个 visible，回 DEFAULT_SETTINGS 的 false）：
          //   - cosyvoice_clone_general_availability_enabled
          onClick={() => setSettings((s) => ({
            ...DEFAULT_SETTINGS,
            // 5 个 Phase 4.2 hidden 字段从 current state 透传（不被 reset 覆盖）
            cosyvoice_clone_worker_enabled: s.cosyvoice_clone_worker_enabled,
            cosyvoice_clone_default_target_model: s.cosyvoice_clone_default_target_model,
            cosyvoice_clone_user_allowlist: s.cosyvoice_clone_user_allowlist,
            cosyvoice_clone_max_voices_per_user: s.cosyvoice_clone_max_voices_per_user,
            cosyvoice_clone_max_concurrent_jobs: s.cosyvoice_clone_max_concurrent_jobs,
            // visible Phase 4.2 GA toggle 显式回 DEFAULT（== false），不透传 s.* —— 这是
            // 整个 reset 按钮"对用户可见的恢复行为"，让 admin 能在 UI 上看到
            // GA 真的复位了，也保证 fail-safe-off。
            cosyvoice_clone_general_availability_enabled:
              DEFAULT_SETTINGS.cosyvoice_clone_general_availability_enabled,
            // --- Phase 4.3a Express 自动 clone reset 规则（spec §8.2 P2-3）---
            // 同 Phase 4.2 D.1 模式：可见 toggle (enabled) 显式回 DEFAULT (false)，
            // 让 admin 能在 UI 上看到复位生效；其它 7 个 hidden 字段透传 current
            // state，避免点恢复默认 + 保存时静默把 allowlist / 阈值 / cap 全部
            // 擦掉。
            express_cosyvoice_auto_clone_enabled:
              DEFAULT_SETTINGS.express_cosyvoice_auto_clone_enabled,
            express_cosyvoice_auto_clone_user_allowlist:
              s.express_cosyvoice_auto_clone_user_allowlist,
            express_cosyvoice_auto_clone_main_speaker_min_ratio:
              s.express_cosyvoice_auto_clone_main_speaker_min_ratio,
            express_cosyvoice_auto_clone_main_speaker_min_lines:
              s.express_cosyvoice_auto_clone_main_speaker_min_lines,
            express_cosyvoice_auto_clone_sample_max_seconds:
              s.express_cosyvoice_auto_clone_sample_max_seconds,
            express_cosyvoice_auto_clone_target_model:
              s.express_cosyvoice_auto_clone_target_model,
            express_cosyvoice_auto_clone_per_user_daily_cap:
              s.express_cosyvoice_auto_clone_per_user_daily_cap,
            express_cosyvoice_auto_clone_per_user_active_temp_cap:
              s.express_cosyvoice_auto_clone_per_user_active_temp_cap,
          }))}
          type="button"
        >
          恢复默认
        </button>
      </div>
    </div>
  )
}

function SettingSection({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-6">
      <h2 className="text-lg font-semibold text-foreground">{title}</h2>
      <p className="text-sm text-muted-foreground mt-1 mb-4">{description}</p>
      {children}
    </section>
  )
}

function RadioGroup({
  options,
  value,
  onChange,
  name,
}: {
  options: { value: string; label: string; description: string }[]
  value: string
  onChange: (v: string) => void
  name?: string
}) {
  const groupName = name ?? options.map((o) => o.value).join('-')
  return (
    <div className="space-y-2">
      {options.map((opt) => (
        <label
          key={opt.value}
          className={`flex items-start gap-3 rounded-xl border p-4 cursor-pointer transition ${
            value === opt.value
              ? 'border-primary/50 bg-primary/5'
              : 'border-border bg-muted/30 hover:bg-muted/50'
          }`}
        >
          <input
            type="radio"
            name={groupName}
            checked={value === opt.value}
            onChange={() => onChange(opt.value)}
            className="mt-0.5 h-4 w-4 border-border"
          />
          <div>
            <p className={`text-sm font-medium ${value === opt.value ? 'text-primary' : 'text-foreground'}`}>
              {opt.label}
            </p>
            <p className="text-xs text-muted-foreground mt-0.5">{opt.description}</p>
          </div>
        </label>
      ))}
    </div>
  )
}
