import { useEffect, useEffectEvent, useRef } from 'react'

type UsePollingTaskOptions = {
  enabled?: boolean
  immediate?: boolean
  intervalMs?: number
  pauseWhenHidden?: boolean
  refreshOnVisible?: boolean
  skipIfRunning?: boolean
}

function isPageHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden
}

function isPromiseLike(value: unknown): value is Promise<unknown> {
  return Boolean(value && typeof (value as Promise<unknown>).then === 'function')
}

export function usePollingTask(
  task: () => Promise<void> | void,
  options: UsePollingTaskOptions = {},
) {
  const {
    enabled = true,
    immediate = true,
    intervalMs = 4000,
    pauseWhenHidden = true,
    refreshOnVisible = true,
    skipIfRunning = true,
  } = options

  const inFlightRef = useRef(false)

  const runTask = useEffectEvent(() => {
    if (!enabled) return
    if (pauseWhenHidden && isPageHidden()) return
    if (skipIfRunning && inFlightRef.current) return

    if (skipIfRunning) {
      inFlightRef.current = true
    }

    let result: Promise<void> | void
    try {
      result = task()
    } catch (error) {
      if (skipIfRunning) {
        inFlightRef.current = false
      }
      throw error
    }

    if (!skipIfRunning) return

    if (isPromiseLike(result)) {
      void result.then(
        () => {
          inFlightRef.current = false
        },
        () => {
          inFlightRef.current = false
        },
      )
      return
    }

    inFlightRef.current = false
  })

  useEffect(() => {
    if (!enabled) {
      return undefined
    }

    if (immediate) {
      runTask()
    }

    const timer = window.setInterval(() => {
      runTask()
    }, intervalMs)

    const onVisibilityChange = () => {
      if (!document.hidden) {
        runTask()
      }
    }
    if (pauseWhenHidden && refreshOnVisible && typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange)
    }

    return () => {
      window.clearInterval(timer)
      if (pauseWhenHidden && refreshOnVisible && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibilityChange)
      }
    }
  }, [enabled, immediate, intervalMs, pauseWhenHidden, refreshOnVisible])
}
