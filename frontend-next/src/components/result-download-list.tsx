import type { ResultDownloadItem } from '@/types/jobs'

type ResultDownloadListProps = {
  items: readonly ResultDownloadItem[]
}

const primaryDownloadKeys = [
  'publish.dubbed_video',
  'editor.dubbed_audio_complete',
  'editor.subtitles',
] as const

const primaryDownloadKeySet = new Set<string>(primaryDownloadKeys)

export function ResultDownloadList({ items }: ResultDownloadListProps) {
  const orderedPrimaryItems = primaryDownloadKeys
    .map((key) => items.find((item) => item.key === key))
    .filter((item): item is ResultDownloadItem => Boolean(item))
  const secondaryItems = items.filter((item) => !primaryDownloadKeySet.has(item.key))

  return (
    <section className="surface-card p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-ink-950">结果下载</h2>
        <p className="muted-copy">
          默认只展示用户最常用的结果文件，补充结果会放到次级区。
        </p>
      </div>

      <div className="mt-5 space-y-3">
        {orderedPrimaryItems.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-ink-950/12 bg-sand-50/80 px-4 py-5 text-sm text-ink-900/65">
            当前还没有可展示的主结果。
          </div>
        ) : null}

        {orderedPrimaryItems.map((item) => (
          <div
            key={item.key}
            className="flex flex-col gap-3 rounded-2xl border border-ink-950/8 bg-sand-50/70 px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
          >
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-semibold text-ink-950">{item.label}</p>
                <span className="rounded-full bg-mint-500/12 px-3 py-1 text-xs font-semibold text-mint-700">
                  主结果
                </span>
              </div>
              <p className="mt-2 text-sm text-ink-900/65">{item.description}</p>
            </div>

            {item.available && item.downloadUrl ? (
              <a
                className="inline-flex rounded-full bg-mint-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-mint-700"
                download
                href={item.downloadUrl}
              >
                下载文件
              </a>
            ) : (
              <span className="inline-flex rounded-full bg-ink-950/8 px-4 py-2 text-sm font-semibold text-ink-900/55">
                暂不可下载
              </span>
            )}
          </div>
        ))}

        {secondaryItems.length > 0 ? (
          <details className="rounded-3xl border border-ink-950/8 bg-white/75 p-4">
            <summary className="cursor-pointer text-sm font-semibold text-ink-950">
              查看补充结果
            </summary>

            <div className="mt-4 space-y-3">
              {secondaryItems.map((item) => (
                <div
                  key={item.key}
                  className="flex flex-col gap-3 rounded-2xl border border-ink-950/8 bg-sand-50/70 px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div>
                    <p className="text-sm font-semibold text-ink-950">{item.label}</p>
                    <p className="mt-2 text-sm text-ink-900/65">{item.description}</p>
                  </div>

                  {item.available && item.downloadUrl ? (
                    <a
                      className="inline-flex rounded-full bg-mint-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-mint-700"
                      download
                      href={item.downloadUrl}
                    >
                      下载文件
                    </a>
                  ) : (
                    <span className="inline-flex rounded-full bg-ink-950/8 px-4 py-2 text-sm font-semibold text-ink-900/55">
                      暂不可下载
                    </span>
                  )}
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </div>
    </section>
  )
}
