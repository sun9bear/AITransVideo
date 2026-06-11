import { useEffect, useRef, type RefObject } from 'react'

/**
 * Mount-state guard for long-running async loops (task-status polling etc.).
 *
 * Pattern:
 *   const isMountedRef = useIsMountedRef()
 *   while (...) {
 *     await sleep(POLL_INTERVAL_MS)
 *     if (!isMountedRef.current) return  // stop tracking after unmount
 *     ...
 *   }
 *
 * 之所以用 ref 而不是 AbortController：StrictMode 的模拟卸载会把
 * AbortController 永久置为 aborted，而 ref 在重挂载的 effect 里能复位。
 * 代价是循环最多多睡一个 interval 才退出，对秒级轮询无感。
 */
export function useIsMountedRef(): RefObject<boolean> {
  const ref = useRef(true)
  useEffect(() => {
    ref.current = true
    return () => {
      ref.current = false
    }
  }, [])
  return ref
}

/** Plain awaitable delay; pair with useIsMountedRef in polling loops. */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
