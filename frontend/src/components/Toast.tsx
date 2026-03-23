import { useEffect, useState } from 'react'

interface ToastProps {
  message: string | null
  type?: 'error' | 'success' | 'info'
  duration?: number
  onClose?: () => void
}

export function Toast({ message, type = 'error', duration = 4000, onClose }: ToastProps) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (message) {
      setVisible(true)
      const timer = setTimeout(() => {
        setVisible(false)
        onClose?.()
      }, duration)
      return () => clearTimeout(timer)
    }
    setVisible(false)
  }, [message, duration, onClose])

  if (!visible || !message) return null

  const bgColor = type === 'error'
    ? 'bg-coral-600'
    : type === 'success'
      ? 'bg-emerald-600'
      : 'bg-ink-800'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center pointer-events-none">
      <div
        className={`${bgColor} text-white px-6 py-4 rounded-2xl shadow-2xl max-w-md text-center text-sm font-medium pointer-events-auto cursor-pointer animate-[fadeIn_0.2s_ease-out]`}
        onClick={() => {
          setVisible(false)
          onClose?.()
        }}
      >
        {message}
      </div>
    </div>
  )
}
