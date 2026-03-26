"use client"

import { useState } from 'react'
import type { ResultDownloadItem } from '@/types/jobs'

type ResultDownloadListProps = {
  items: readonly ResultDownloadItem[]
}

const primaryDownloadKeys = [
  'publish.dubbed_video',
  'editor.dubbed_audio_complete',
  'editor.tts_segments_zip',
  'editor.subtitles',
] as const

const primaryDownloadKeySet = new Set<string>(primaryDownloadKeys)

const ZIP_KEYS = new Set(['editor.tts_segments_zip'])

export function ResultDownloadList({ items }: ResultDownloadListProps) {
  const orderedPrimaryItems = primaryDownloadKeys
    .map((key) => items.find((item) => item.key === key))
    .filter((item): item is ResultDownloadItem => Boolean(item))
  const secondaryItems = items.filter((item) => !primaryDownloadKeySet.has(item.key))

  return (
    <section className="rounded-2xl border border-border bg-card p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-foreground">结果下载</h2>
        <p className="text-sm text-muted-foreground">
          默认只展示用户最常用的结果文件，补充结果会放到次级区。
        </p>
      </div>

      <div className="mt-5 space-y-3">
        {orderedPrimaryItems.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-5 text-sm text-muted-foreground">
            当前还没有可展示的主结果。
          </div>
        ) : null}

        {orderedPrimaryItems.map((item) => (
          <DownloadRow key={item.key} item={item} isPrimary />
        ))}

        {secondaryItems.length > 0 ? (
          <details className="rounded-xl border border-border bg-card p-4">
            <summary className="cursor-pointer text-sm font-semibold text-muted-foreground hover:text-foreground/80 transition">
              查看补充结果
            </summary>
            <div className="mt-4 space-y-3">
              {secondaryItems.map((item) => (
                <DownloadRow key={item.key} item={item} isPrimary={false} />
              ))}
            </div>
          </details>
        ) : null}
      </div>
    </section>
  )
}

function DownloadRow({ item, isPrimary }: { item: ResultDownloadItem; isPrimary: boolean }) {
  const [downloading, setDownloading] = useState(false)
  const isZip = ZIP_KEYS.has(item.key)

  const handleClick = () => {
    if (!item.downloadUrl || downloading) return
    setDownloading(true)

    if (isZip) {
      // Async fetch + blob download for zip files
      fetch(item.downloadUrl, { credentials: 'include' })
        .then((resp) => {
          if (!resp.ok) throw new Error('下载失败')
          return resp.blob().then((blob) => ({ blob, headers: resp.headers }))
        })
        .then(({ blob, headers }) => {
          const disposition = headers.get('content-disposition') || ''
          const match = disposition.match(/filename\*?=(?:UTF-8'')?([^;\s]+)/)
          const filename = match ? decodeURIComponent(match[1]) : 'tts_segments.zip'
          const url = URL.createObjectURL(blob)
          const a = document.createElement('a')
          a.href = url
          a.download = filename
          a.click()
          URL.revokeObjectURL(url)
        })
        .catch(() => { window.open(item.downloadUrl!, '_blank') })
        .finally(() => { setDownloading(false) })
    } else {
      // Normal files: trigger <a> download, show hint for 3 seconds
      const a = document.createElement('a')
      a.href = item.downloadUrl
      a.download = ''
      a.click()
      setTimeout(() => { setDownloading(false) }, 3000)
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-muted/30 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-semibold text-foreground">{item.label}</p>
          {isPrimary ? (
            <span className="rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-semibold text-emerald-400">
              主结果
            </span>
          ) : null}
        </div>
        <p className="mt-2 text-sm text-muted-foreground">{item.description}</p>
        {downloading ? (
          <div className="mt-2 flex items-center gap-2">
            <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary border-t-transparent" aria-hidden="true" />
            <span className="text-xs text-primary/80">
              {isZip ? '资源打包中，即将开始下载…请勿重复点击…' : '即将开始下载…请勿重复点击…'}
            </span>
          </div>
        ) : null}
      </div>

      {item.available && item.downloadUrl ? (
        <button
          className="inline-flex shrink-0 rounded-full bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={downloading}
          onClick={handleClick}
          type="button"
        >
          {downloading ? (isZip ? '打包中…' : '下载中…') : '下载文件'}
        </button>
      ) : (
        <span className="inline-flex shrink-0 rounded-full bg-muted/40 px-4 py-2 text-sm font-semibold text-muted-foreground">
          暂不可下载
        </span>
      )}
    </div>
  )
}
