"use client"

import { useEffect, useState, useCallback, useMemo } from 'react'
import type {
  VoiceCatalogItem,
  VoiceCatalogListResponse,
  CreateVoiceRequest,
  UpdateVoiceRequest,
  ImportPreviewEntry,
} from '@/types/voiceCatalog'
import {
  listVoices,
  createVoice,
  updateVoice,
  deleteVoice,
  finalizeLabel,
  getLabelStatus,
  triggerTextLabeling,
  triggerAudioLabeling,
  submitLabelTask,
  pollLabelTask,
  batchFinalizeLabels,
  verifyVoice,
  verifyBatch,
  importVoices,
} from '@/lib/api/voiceCatalog'

// ---------------------------------------------------------------------------
// Provider sub-filter
// ---------------------------------------------------------------------------

interface ProviderFilter {
  value: string
  label: string
  apiProvider?: string
  apiResourceId?: string
}

const PROVIDER_FILTERS: ProviderFilter[] = [
  { value: '',                label: '全部 Provider' },
  { value: 'volcengine_1_0', label: '豆包 1.0',           apiProvider: 'volcengine', apiResourceId: 'seed-tts-1.0' },
  { value: 'volcengine_2_0', label: '豆包 2.0',           apiProvider: 'volcengine', apiResourceId: 'seed-tts-2.0' },
  { value: 'cosyvoice',      label: 'CosyVoice（全部）',  apiProvider: 'cosyvoice' },
  { value: 'minimax',        label: 'MiniMax',            apiProvider: 'minimax' },
  { value: 'mimo',           label: 'MiMo',               apiProvider: 'mimo' },
]

const GENDERS = [
  { value: '', label: '全部性别' },
  { value: 'male', label: '男' },
  { value: 'female', label: '女' },
  { value: 'child', label: '儿童' },
]

const LABEL_FILTERS = [
  { value: '', label: '全部标注' },
  { value: 'none', label: '未标注' },
  { value: 'text', label: '仅文本标注' },
  { value: 'audio_round1', label: '仅音频R1' },
  { value: 'audio_round2', label: '仅音频R2' },
  { value: 'audio_round3', label: '仅音频R3' },
  { value: 'final', label: '已 Final' },
]

// ---------------------------------------------------------------------------
// Badge components
// ---------------------------------------------------------------------------

function LabelBadge({ done }: { done: boolean }) {
  return done
    ? <span className="inline-block w-2 h-2 rounded-full bg-green-500" title="已完成" />
    : <span className="inline-block w-2 h-2 rounded-full bg-zinc-600" title="未完成" />
}

function VerifyBadge({ item }: { item: VoiceCatalogItem }) {
  // seed 继承信任：仅当从未人工验证过时显示 seed
  if (item.is_verified && item.is_seed && item.verify_attempts === 0) {
    return <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">seed</span>
  }
  // 人工验证通过（包括 seed 音色重新验证后）
  if (item.is_verified && item.verify_attempts > 0) {
    return <span className="text-xs px-1.5 py-0.5 rounded bg-green-500/20 text-green-400">已验证</span>
  }
  // 从未验证过的 seed 但 is_verified（理论上不会走到，保底）
  if (item.is_verified) {
    return <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">seed</span>
  }
  // 验证过但失败
  if (item.verify_attempts > 0) {
    return <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-400">失败</span>
  }
  return <span className="text-xs px-1.5 py-0.5 rounded bg-zinc-500/20 text-zinc-400">待验证</span>
}

function ResourceTag({ item }: { item: VoiceCatalogItem }) {
  const rid = (item.provider_config as Record<string, string>)?.resource_id
  if (!rid) return null
  const short = rid.replace('seed-tts-', '')
  return <span className="text-[10px] px-1 py-0.5 rounded bg-zinc-700 text-zinc-300">{short}</span>
}

function FinalLabelTags({ item }: { item: VoiceCatalogItem }) {
  const fl = item.final_label
  if (!fl) return <span className="text-[10px] text-zinc-600">-</span>
  const tags: string[] = []
  if (fl.age_group) tags.push(fl.age_group)
  if (fl.persona_style) tags.push(fl.persona_style)
  if (fl.energy_level) tags.push(fl.energy_level)
  if (fl.maturity) tags.push(fl.maturity)
  if (fl.delivery_style) tags.push(fl.delivery_style)
  if (fl.pitch_level) tags.push(`pitch:${fl.pitch_level}`)
  if (fl.texture_tags?.length) tags.push(fl.texture_tags.join('/'))
  if (!tags.length) return <span className="text-[10px] text-zinc-600">-</span>
  return (
    <div className="flex flex-wrap gap-0.5">
      {tags.map(t => (
        <span key={t} className="text-[10px] px-1 py-0 rounded bg-purple-500/15 text-purple-300">{t}</span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Modal wrapper
// ---------------------------------------------------------------------------

function Modal({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: React.ReactNode
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-background border border-border rounded-xl shadow-xl w-full max-w-lg max-h-[80vh] overflow-y-auto p-6"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-foreground">{title}</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground text-xl">&times;</button>
        </div>
        {children}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add Voice modal
// ---------------------------------------------------------------------------

function AddVoiceModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: () => void
}) {
  const [form, setForm] = useState<CreateVoiceRequest>({
    voice_id: '',
    provider: 'volcengine',
    display_name: '',
    gender: 'female',
    language: 'zh',
  })
  const [resourceId, setResourceId] = useState('seed-tts-1.0')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleSave = async () => {
    setError('')
    setSaving(true)
    try {
      const req: CreateVoiceRequest = {
        ...form,
        provider_config: form.provider === 'volcengine'
          ? { resource_id: resourceId }
          : {},
      }
      await createVoice(req)
      onCreated()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="新增音色">
      <div className="space-y-3">
        <label className="block text-sm">
          <span className="text-muted-foreground">voice_id *</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.voice_id} onChange={e => setForm(f => ({ ...f, voice_id: e.target.value }))} />
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">显示名称 *</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.display_name} onChange={e => setForm(f => ({ ...f, display_name: e.target.value }))} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-sm">
            <span className="text-muted-foreground">Provider</span>
            <select className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              value={form.provider} onChange={e => setForm(f => ({ ...f, provider: e.target.value }))}>
              <option value="volcengine">volcengine</option>
              <option value="cosyvoice">cosyvoice</option>
              <option value="minimax">minimax</option>
              <option value="mimo">mimo</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">性别</span>
            <select className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              value={form.gender || ''} onChange={e => setForm(f => ({ ...f, gender: e.target.value || null }))}>
              <option value="female">女</option>
              <option value="male">男</option>
              <option value="child">儿童</option>
            </select>
          </label>
        </div>
        {form.provider === 'volcengine' && (
          <label className="block text-sm">
            <span className="text-muted-foreground">resource_id</span>
            <select className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              value={resourceId} onChange={e => setResourceId(e.target.value)}>
              <option value="seed-tts-1.0">seed-tts-1.0</option>
              <option value="seed-tts-2.0">seed-tts-2.0</option>
            </select>
          </label>
        )}
        <label className="block text-sm">
          <span className="text-muted-foreground">场景</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.scene || ''} onChange={e => setForm(f => ({ ...f, scene: e.target.value || null }))} />
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">备注</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.notes || ''} onChange={e => setForm(f => ({ ...f, notes: e.target.value || null }))} />
        </label>
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button className="secondary-button text-sm" onClick={onClose}>取消</button>
          <button className="primary-button text-sm" onClick={handleSave} disabled={saving || !form.voice_id || !form.display_name}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Edit Voice modal
// ---------------------------------------------------------------------------

function EditVoiceModal({
  open,
  onClose,
  voice,
  onUpdated,
}: {
  open: boolean
  onClose: () => void
  voice: VoiceCatalogItem | null
  onUpdated: () => void
}) {
  const [form, setForm] = useState<UpdateVoiceRequest>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (voice) {
      setForm({
        display_name: voice.display_name,
        gender: voice.gender,
        scene: voice.scene,
        matchable: voice.matchable,
        notes: voice.notes,
      })
    }
  }, [voice])

  if (!voice) return null

  const handleSave = async () => {
    setError('')
    setSaving(true)
    try {
      await updateVoice(voice.voice_id, form)
      onUpdated()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`编辑 ${voice.voice_id}`}>
      <div className="space-y-3">
        <label className="block text-sm">
          <span className="text-muted-foreground">显示名称</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.display_name || ''} onChange={e => setForm(f => ({ ...f, display_name: e.target.value }))} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-sm">
            <span className="text-muted-foreground">性别</span>
            <select className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              value={form.gender || ''} onChange={e => setForm(f => ({ ...f, gender: e.target.value || null }))}>
              <option value="">-</option>
              <option value="female">女</option>
              <option value="male">男</option>
              <option value="child">儿童</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">场景</span>
            <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
              value={form.scene || ''} onChange={e => setForm(f => ({ ...f, scene: e.target.value || null }))} />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.matchable ?? true}
            onChange={e => setForm(f => ({ ...f, matchable: e.target.checked }))} />
          <span className="text-muted-foreground">可匹配</span>
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">备注</span>
          <input className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
            value={form.notes || ''} onChange={e => setForm(f => ({ ...f, notes: e.target.value || null }))} />
        </label>
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2 pt-2">
          <button className="secondary-button text-sm" onClick={onClose}>取消</button>
          <button className="primary-button text-sm" onClick={handleSave} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Import modal
// ---------------------------------------------------------------------------

function ImportModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: () => void
}) {
  const [text, setText] = useState('')
  const [provider, setProvider] = useState('volcengine')
  const [preview, setPreview] = useState<ImportPreviewEntry[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<{ created: string[]; skipped: string[]; errors: Array<{ voice_id: string; error: string }> } | null>(null)
  const [error, setError] = useState('')

  const handlePreview = async () => {
    setError('')
    setLoading(true)
    try {
      const resp = await importVoices({ text, provider, dry_run: true })
      if ('entries' in resp) {
        setPreview(resp.entries)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const handleConfirm = async () => {
    setError('')
    setLoading(true)
    try {
      const resp = await importVoices({ text, provider, dry_run: false })
      if ('created' in resp) {
        setResult(resp)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const handleClose = () => {
    if (result) onDone()
    setPreview(null)
    setResult(null)
    setText('')
    setError('')
    onClose()
  }

  return (
    <Modal open={open} onClose={handleClose} title="批量导入音色">
      <div className="space-y-3">
        {!result ? (
          <>
            <label className="block text-sm">
              <span className="text-muted-foreground">Provider</span>
              <select className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
                value={provider} onChange={e => setProvider(e.target.value)}>
                <option value="volcengine">volcengine</option>
                <option value="cosyvoice">cosyvoice</option>
                <option value="minimax">minimax</option>
                <option value="mimo">mimo</option>
              </select>
            </label>
            <label className="block text-sm">
              <span className="text-muted-foreground">CSV/Tab 文本（voice_id, display_name, gender, scene, resource_id）</span>
              <textarea className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-mono h-32"
                value={text} onChange={e => setText(e.target.value)}
                placeholder="voice_id, display_name, gender, scene, resource_id&#10;zh_male_test, 测试音色, male, 通用, seed-tts-1.0" />
            </label>

            {preview && (
              <div className="border border-border rounded-lg p-3 text-sm max-h-48 overflow-y-auto">
                <p className="font-medium mb-2">预览（{preview.length} 条）</p>
                {preview.map(e => (
                  <div key={e.voice_id} className="flex justify-between py-0.5">
                    <span className="font-mono text-xs">{e.voice_id}</span>
                    <span className={e.status === 'will_create' ? 'text-green-400 text-xs' : 'text-zinc-500 text-xs'}>
                      {e.status === 'will_create' ? '新增' : '跳过（已存在）'}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {error && <p className="text-sm text-red-400">{error}</p>}

            <div className="flex justify-end gap-2 pt-2">
              <button className="secondary-button text-sm" onClick={handleClose}>取消</button>
              {!preview ? (
                <button className="primary-button text-sm" onClick={handlePreview}
                  disabled={loading || !text.trim()}>
                  {loading ? '解析中...' : '预览'}
                </button>
              ) : (
                <button className="primary-button text-sm" onClick={handleConfirm}
                  disabled={loading || preview.every(e => e.status === 'skip_duplicate')}>
                  {loading ? '导入中...' : '确认导入'}
                </button>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="space-y-2 text-sm">
              <p className="text-green-400">已创建：{result.created.length} 条</p>
              {result.skipped.length > 0 && <p className="text-zinc-400">跳过（重复）：{result.skipped.length} 条</p>}
              {result.errors.length > 0 && (
                <div className="text-red-400">
                  <p>错误：{result.errors.length} 条</p>
                  {result.errors.map(e => <p key={e.voice_id} className="text-xs ml-2">{e.voice_id}: {e.error}</p>)}
                </div>
              )}
            </div>
            <div className="flex justify-end pt-2">
              <button className="primary-button text-sm" onClick={handleClose}>关闭</button>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function VoiceCatalogPage() {
  const [items, setItems] = useState<VoiceCatalogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [providerKey, setProviderKey] = useState('')
  const [gender, setGender] = useState('')
  const [labelFilter, setLabelFilter] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Modals
  const [showAdd, setShowAdd] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [editVoice, setEditVoice] = useState<VoiceCatalogItem | null>(null)

  // Batch selection
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // Per-row verify loading
  const [verifying, setVerifying] = useState<Set<string>>(new Set())

  // Label status
  const [labelStatus, setLabelStatus] = useState<{ total_voices: number; label_counts: Record<string, number> } | null>(null)

  const selectedFilter = useMemo(
    () => PROVIDER_FILTERS.find(f => f.value === providerKey) || PROVIDER_FILTERS[0],
    [providerKey],
  )

  const load = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const resp: VoiceCatalogListResponse = await listVoices({
        provider: selectedFilter.apiProvider || undefined,
        resourceId: selectedFilter.apiResourceId || undefined,
        gender: gender || undefined,
        labelFilter: labelFilter || undefined,
        page,
        pageSize,
      })
      setItems(resp.items)
      setTotal(resp.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setIsLoading(false)
    }
  }, [selectedFilter, gender, labelFilter, page, pageSize])

  useEffect(() => { load() }, [load])

  // Load label status on mount
  useEffect(() => {
    getLabelStatus().then(setLabelStatus).catch(() => {})
  }, [])

  // Clear selection on page/filter change
  useEffect(() => { setSelected(new Set()) }, [providerKey, gender, labelFilter, page])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const handleDelete = async (voiceId: string) => {
    if (!confirm(`确认归档音色 ${voiceId}？（软删除）`)) return
    try {
      await deleteVoice(voiceId)
      load()
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    }
  }

  const handleVerify = async (voiceId: string) => {
    setVerifying(v => new Set(v).add(voiceId))
    try {
      const resp = await verifyVoice(voiceId)
      // Update the item in place
      setItems(prev => prev.map(item =>
        item.voice_id === voiceId ? resp.voice : item
      ))
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    } finally {
      setVerifying(v => { const n = new Set(v); n.delete(voiceId); return n })
    }
  }

  const handleBatchVerify = async () => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    if (!confirm(`验证选中的 ${ids.length} 个音色？`)) return

    setVerifying(new Set(ids))
    try {
      await verifyBatch(ids)
      load()
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    } finally {
      setVerifying(new Set())
      setSelected(new Set())
    }
  }

  const [labeling, setLabeling] = useState(false)
  const [labelProgress, setLabelProgress] = useState('')

  const handleBatchLabel = async (type: 'text' | 'round1' | 'round2' | 'round3') => {
    const ids = Array.from(selected)
    if (ids.length === 0) return

    const supported = new Set(['volcengine', 'cosyvoice'])
    const unsupported = items.filter(i => selected.has(i.voice_id) && !supported.has(i.provider))
    if (unsupported.length > 0) {
      alert(`标注仅支持 volcengine/cosyvoice，已选中 ${unsupported.length} 个不支持的音色`)
      return
    }

    const isAudio = type !== 'text'
    const labelName = isAudio ? `音频 ${type.toUpperCase()}` : '文本标注'
    if (!confirm(`对选中的 ${ids.length} 个音色执行${labelName}？`)) return

    setLabeling(true)
    setSelected(new Set())

    try {
      // Submit async task
      const taskType = isAudio ? 'trigger-audio' : 'trigger-text'
      const { task_id } = await submitLabelTask(ids, taskType as 'trigger-text' | 'trigger-audio', isAudio ? type : undefined)

      // Poll progress
      let done = false
      while (!done) {
        await new Promise(r => setTimeout(r, 3000))
        const task = await pollLabelTask(task_id)
        const p = task.progress
        setLabelProgress(`${labelName} ${p.completed}/${p.total}（批次 ${p.current_batch}）...`)

        if (task.status === 'completed') {
          done = true
          const r = task.result
          const written = r?.written?.length ?? 0
          const errors = r?.errors?.length ?? 0
          alert(`${labelName}完成：${written} 成功${errors ? `，${errors} 失败` : ''}`)
        } else if (task.status === 'failed') {
          done = true
          alert(`${labelName}失败：${task.error || '未知错误'}`)
        }
      }
      load()
      getLabelStatus().then(setLabelStatus).catch(() => {})
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    } finally {
      setLabeling(false)
      setLabelProgress('')
    }
  }

  const toggleSelect = (voiceId: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(voiceId)) next.delete(voiceId)
      else next.add(voiceId)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selected.size === items.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(items.map(i => i.voice_id)))
    }
  }

  const handleBatchFinalize = async () => {
    const ids = Array.from(selected)
    if (!ids.length) return
    if (!confirm(`为选中的 ${ids.length} 个音色生成 Final 标签？`)) return
    try {
      const resp = await batchFinalizeLabels(ids)
      alert(`Final 标签：${resp.succeeded.length} 成功${resp.failed.length ? `，${resp.failed.length} 失败` : ''}`)
      load()
      getLabelStatus().then(setLabelStatus).catch(() => {})
      setSelected(new Set())
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    }
  }

  const handleFinalize = async (voiceId: string) => {
    try {
      await finalizeLabel(voiceId)
      alert(`final 标签已生成: ${voiceId}`)
      load()
      getLabelStatus().then(setLabelStatus).catch(() => {})
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">音色库管理</h1>
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">共 {total} 个音色</span>
          <button className="secondary-button text-xs" onClick={() => setShowImport(true)}>
            批量导入
          </button>
          <button className="primary-button text-xs" onClick={() => setShowAdd(true)}>
            新增音色
          </button>
        </div>
      </div>

      {/* Label status bar */}
      {labelStatus && (
        <div className="flex gap-4 text-xs text-muted-foreground bg-muted/30 rounded-lg px-4 py-2">
          <span>标注进度：</span>
          <span>文本 <b className="text-foreground">{labelStatus.label_counts.text || 0}</b>/{labelStatus.total_voices}</span>
          <span>音频R1 <b className="text-foreground">{labelStatus.label_counts.audio_round1 || 0}</b></span>
          <span>R2 <b className="text-foreground">{labelStatus.label_counts.audio_round2 || 0}</b></span>
          <span>R3 <b className="text-foreground">{labelStatus.label_counts.audio_round3 || 0}</b></span>
          <span>Final <b className="text-foreground">{labelStatus.label_counts.final || 0}</b>/{labelStatus.total_voices}</span>
        </div>
      )}

      {/* Filters + batch actions */}
      <div className="flex gap-3 flex-wrap items-center">
        <select
          className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
          value={providerKey}
          onChange={(e) => { setProviderKey(e.target.value); setPage(1) }}
        >
          {PROVIDER_FILTERS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
        <select
          className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
          value={gender}
          onChange={(e) => { setGender(e.target.value); setPage(1) }}
        >
          {GENDERS.map(g => <option key={g.value} value={g.value}>{g.label}</option>)}
        </select>
        <select
          className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm"
          value={labelFilter}
          onChange={(e) => { setLabelFilter(e.target.value); setPage(1) }}
        >
          {LABEL_FILTERS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
        </select>
        {labeling && (
          <span className="text-xs text-yellow-400 animate-pulse">{labelProgress || '标注中...'}</span>
        )}
        {!labeling && selected.size > 0 && (
          <>
            <button className="secondary-button text-xs" onClick={handleBatchVerify}>
              验证选中（{selected.size}）
            </button>
            <button className="secondary-button text-xs" onClick={() => handleBatchLabel('text')}>
              文本标注（{selected.size}）
            </button>
            <button className="secondary-button text-xs" onClick={() => handleBatchLabel('round1')}>
              音频R1（{selected.size}）
            </button>
            <button className="secondary-button text-xs" onClick={() => handleBatchLabel('round2')}>
              音频R2（{selected.size}）
            </button>
            <button className="secondary-button text-xs" onClick={() => handleBatchLabel('round3')}>
              音频R3（{selected.size}）
            </button>
            <button className="primary-button text-xs" onClick={handleBatchFinalize}>
              生成 Final（{selected.size}）
            </button>
          </>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-4 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-2 py-2 text-center w-8">
                <input type="checkbox" checked={items.length > 0 && selected.size === items.length}
                  onChange={toggleSelectAll} />
              </th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">voice_id</th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">名称</th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">Provider</th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">性别</th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">场景</th>
              <th className="px-3 py-2 text-center font-medium text-muted-foreground">验证</th>
              <th className="px-3 py-2 text-center font-medium text-muted-foreground" title="text / audio1 / audio2 / audio3 / final">标注</th>
              <th className="px-3 py-2 text-left font-medium text-muted-foreground">Final 标签</th>
              <th className="px-3 py-2 text-center font-medium text-muted-foreground">可匹配</th>
              <th className="px-3 py-2 text-center font-medium text-muted-foreground">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {isLoading ? (
              <tr><td colSpan={11} className="px-3 py-8 text-center text-muted-foreground">加载中...</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={11} className="px-3 py-8 text-center text-muted-foreground">暂无数据</td></tr>
            ) : items.map((item) => (
              <tr key={item.voice_id} className="hover:bg-muted/30">
                <td className="px-2 py-2 text-center">
                  <input type="checkbox" checked={selected.has(item.voice_id)}
                    onChange={() => toggleSelect(item.voice_id)} />
                </td>
                <td className="px-3 py-2 font-mono text-xs text-foreground/80 max-w-[240px] truncate" title={item.voice_id}>
                  {item.voice_id}
                </td>
                <td className="px-3 py-2 text-foreground">{item.display_name}</td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-1.5">
                    <span className="text-muted-foreground">{item.provider}</span>
                    <ResourceTag item={item} />
                  </div>
                </td>
                <td className="px-3 py-2 text-muted-foreground">{item.gender || '-'}</td>
                <td className="px-3 py-2 text-muted-foreground text-xs">{item.scene || '-'}</td>
                <td className="px-3 py-2 text-center"><VerifyBadge item={item} /></td>
                <td className="px-3 py-2 text-center">
                  <div className="flex gap-1 justify-center" title="text / audio1 / audio2 / audio3 / final">
                    <LabelBadge done={item.label_status.text} />
                    <LabelBadge done={item.label_status.audio_round1} />
                    <LabelBadge done={item.label_status.audio_round2} />
                    <LabelBadge done={item.label_status.audio_round3} />
                    <LabelBadge done={item.label_status.final} />
                  </div>
                </td>
                <td className="px-3 py-2 max-w-[200px]">
                  <FinalLabelTags item={item} />
                </td>
                <td className="px-3 py-2 text-center">
                  {item.matchable
                    ? <span className="text-green-400">是</span>
                    : <span className="text-zinc-500">否</span>}
                </td>
                <td className="px-3 py-2 text-center">
                  <div className="flex gap-1 justify-center">
                    <button
                      className="text-xs px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30"
                      disabled={verifying.has(item.voice_id)}
                      onClick={() => handleVerify(item.voice_id)}
                      title="验证"
                    >
                      {verifying.has(item.voice_id) ? '...' : '验证'}
                    </button>
                    <button
                      className="text-xs px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 hover:bg-purple-500/30"
                      onClick={() => handleFinalize(item.voice_id)}
                      title="生成 Final 标签"
                    >
                      Final
                    </button>
                    <button
                      className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30"
                      onClick={() => setEditVoice(item)}
                      title="编辑"
                    >
                      编辑
                    </button>
                    <button
                      className="text-xs px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30"
                      onClick={() => handleDelete(item.voice_id)}
                      title="归档"
                    >
                      归档
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button
            className="secondary-button text-xs"
            disabled={page <= 1}
            onClick={() => setPage(p => Math.max(1, p - 1))}
          >
            上一页
          </button>
          <span className="text-sm text-muted-foreground">
            {page} / {totalPages}
          </span>
          <button
            className="secondary-button text-xs"
            disabled={page >= totalPages}
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
          >
            下一页
          </button>
        </div>
      )}

      {/* Modals */}
      <AddVoiceModal open={showAdd} onClose={() => setShowAdd(false)} onCreated={load} />
      <EditVoiceModal open={!!editVoice} onClose={() => setEditVoice(null)} voice={editVoice} onUpdated={load} />
      <ImportModal open={showImport} onClose={() => setShowImport(false)} onDone={load} />
    </div>
  )
}
