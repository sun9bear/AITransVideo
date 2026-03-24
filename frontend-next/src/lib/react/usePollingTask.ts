import { useEffect, useEffectEvent } from 'react'

type UsePollingTaskOptions = {
  enabled?: boolean
  immediate?: boolean
  intervalMs?: number
}

export function usePollingTask(
  task: () => Promise<void> | void,
  options: UsePollingTaskOptions = {},
) {
  const {
    enabled = true,
    immediate = true,
    intervalMs = 4000,
  } = options
  const runTask = useEffectEvent(() => {
    void task()
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

    return () => {
      window.clearInterval(timer)
    }
  }, [enabled, immediate, intervalMs])
}
