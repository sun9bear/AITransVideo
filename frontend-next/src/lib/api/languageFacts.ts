/**
 * Language direction facts (PR-A part 2 §5/§7). The Gateway endpoint
 * `/api/language-facts` returns the directions the CURRENT user may pick
 * (entitlement-filtered): every user sees the GA default (英文 → 中文); the
 * 内测 direction 中文 → 英文 appears only for admin-enabled allowlisted users.
 */

export interface LanguagePairFact {
  pair_key: string
  source_language: string
  target_language: string
  label: string
  is_default: boolean
  /** Create-path hard gate: false → the direction is shown as 即将上线 and the
   *  option is disabled; the backend 409s `language_pair_not_yet_available`. */
  pipeline_ready: boolean
  workflow_capabilities: string[]
}

/** The zero-regression GA default — also the fail-closed fallback. */
export const GA_DEFAULT_LANGUAGE_FACT: LanguagePairFact = {
  pair_key: 'en->zh-CN',
  source_language: 'en',
  target_language: 'zh-CN',
  label: '英文 → 中文',
  is_default: true,
  pipeline_ready: true,
  workflow_capabilities: ['transcribe', 'translate', 'tts', 'subtitles', 'jianying'],
}

/**
 * Fetch the language directions the current user may pick. **Fail-closed**: any
 * non-2xx / network error / empty response resolves to the GA default pair only,
 * so the form always has at least the zero-regression direction and never
 * blocks on this fetch. Lives on the Gateway (not Job API) → direct fetch.
 */
export async function getLanguageFacts(): Promise<LanguagePairFact[]> {
  try {
    const resp = await fetch('/api/language-facts', { credentials: 'include' })
    if (!resp.ok) return [GA_DEFAULT_LANGUAGE_FACT]
    const data = (await resp.json()) as { language_pairs?: LanguagePairFact[] }
    const pairs = Array.isArray(data?.language_pairs) ? data.language_pairs : []
    return pairs.length > 0 ? pairs : [GA_DEFAULT_LANGUAGE_FACT]
  } catch {
    return [GA_DEFAULT_LANGUAGE_FACT]
  }
}
