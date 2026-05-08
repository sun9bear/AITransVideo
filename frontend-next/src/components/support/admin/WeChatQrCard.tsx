"use client"

import { useEffect, useRef, useState } from "react"

import {
  adminDeleteWechatQr,
  adminGetWechatQrInfo,
  adminUploadWechatQr,
  type WeChatQrInfo,
} from "@/lib/api/support"

/**
 * Admin upload UI for the WeChat customer-service QR.
 *
 * The image is shown to users in SupportWidget when no admin is online
 * AND the user has clicked 转人工. Constraints (defense against abuse):
 *   - Only PNG / JPEG accepted (server enforces; client also gates).
 *   - Max 1 MB.
 *   - Single QR per deployment; re-upload overwrites.
 */
export function WeChatQrCard() {
  const [info, setInfo] = useState<WeChatQrInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      setInfo(await adminGetWechatQrInfo())
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  const onPick = (file: File) => {
    setError(null)
    if (!["image/png", "image/jpeg", "image/jpg"].includes(file.type)) {
      setError(`不支持的文件类型：${file.type}。仅 PNG / JPEG。`)
      return
    }
    if (file.size > 1024 * 1024) {
      setError(`文件过大：${(file.size / 1024).toFixed(1)} KB > 1024 KB`)
      return
    }
    setUploading(true)
    adminUploadWechatQr(file)
      .then((next) => setInfo(next))
      .catch((err) => setError(err instanceof Error ? err.message : "上传失败"))
      .finally(() => setUploading(false))
  }

  const onDelete = async () => {
    if (!confirm("确定移除当前 WeChat 二维码？用户在运营离线时将看不到 QR 卡片。")) {
      return
    }
    setUploading(true)
    try {
      await adminDeleteWechatQr()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "移除失败")
    } finally {
      setUploading(false)
    }
  }

  return (
    <section className="space-y-4 rounded-xl border border-border bg-card p-4">
      <header>
        <h2 className="text-base font-semibold text-foreground">微信客服二维码</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          运营全部不在线时，用户点「转人工」会看到这张 QR 卡片，引导用户加微信继续沟通。
          PNG / JPEG，≤ 1 MB。
        </p>
      </header>

      {loading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : info?.has_qr && info.url ? (
        <div className="flex flex-wrap items-start gap-4">
          <div className="rounded border border-border bg-background p-2">
            <img
              src={info.url}
              alt="WeChat 客服二维码"
              className="h-48 w-48 object-contain"
            />
          </div>
          <div className="flex-1 space-y-2 text-sm">
            <p className="text-muted-foreground">
              已上传 ·{" "}
              {info.uploaded_at
                ? new Date(info.uploaded_at).toLocaleString("zh-CN")
                : "—"}
            </p>
            <p className="text-xs text-muted-foreground">
              {info.size_bytes ? `${(info.size_bytes / 1024).toFixed(1)} KB` : ""}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
                className="rounded border border-border bg-background px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
              >
                替换
              </button>
              <button
                type="button"
                disabled={uploading}
                onClick={() => void onDelete()}
                className="rounded border border-destructive/40 bg-destructive/10 px-3 py-1 text-xs text-destructive hover:bg-destructive/20 disabled:opacity-50"
              >
                移除
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-muted-foreground">还未上传二维码。</p>
          <button
            type="button"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
            className="rounded border border-border bg-background px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
          >
            {uploading ? "上传中…" : "上传二维码"}
          </button>
        </div>
      )}

      {error ? (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <input
        ref={fileInputRef}
        type="file"
        accept="image/png,image/jpeg"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onPick(f)
          // reset so picking the same file twice still triggers onChange
          e.target.value = ""
        }}
      />
    </section>
  )
}
