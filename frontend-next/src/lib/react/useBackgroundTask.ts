"use client"

import { useCallback, useEffect, useRef, useState } from "react"

// 2026-04-21: "expired" added for materials_pack zips that the gateway
// periodic cleanup has pruned (plan §7 24h retention). UI should treat
// expired like a soft-failure that requires a re-pack click, without
// surfacing it as an error.
export type TaskStatus =
  | "idle"
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "expired"

export interface TaskProgress {
  stage?: string
  percent?: number
  [k: string]: unknown
}

export interface BackgroundTaskState {
  taskId: string | null
  status: TaskStatus
  progress: TaskProgress | null
  result: Record<string, unknown> | null
  error: string | null
  /** Seconds since task first transitioned to running. Hint for stall UI. */
  runningForSec: number
}

export interface UseBackgroundTaskOptions {
  jobId: string
  taskType: "materials_pack" | "generate_video"
  /** SHA-256 of params (hex). Required for param-specific state restore. */
  paramsFingerprint?: string
  /** Milliseconds between polls. materials_pack: ~2500; generate_video: ~4000. */
  pollIntervalMs?: number
  /** Threshold to emit "possibly stalled" hint. Default 30min. */
  stallHintMs?: number
}

export interface UseBackgroundTaskReturn extends BackgroundTaskState {
  startTask: (params: Record<string, unknown>) => Promise<string | null>
  reset: () => void
  isActive: boolean
  /** Whether to show "task may be stalled" hint. */
  stalled: boolean
}

const DEFAULT_POLL_MS = 3000
const DEFAULT_STALL_MS = 30 * 60 * 1000
const POLL_BACKOFF_MAX = 3

function isPageHidden(): boolean {
  return typeof document !== "undefined" && document.hidden
}

type ApiTask = {
  task_id: string
  job_id?: string
  task_type?: string
  status: TaskStatus
  progress: TaskProgress | null
  result: Record<string, unknown> | null
  error: string | null
  params_fingerprint?: string
  created_at?: string | null
  updated_at?: string | null
}

async function fetchLatest(
  jobId: string,
  taskType: string,
  fingerprint: string | undefined,
): Promise<ApiTask | null> {
  const qs = new URLSearchParams({ type: taskType })
  if (fingerprint) qs.set("fingerprint", fingerprint)
  const res = await fetch(`/api/jobs/${jobId}/tasks/latest?${qs.toString()}`, {
    credentials: "include",
  })
  if (!res.ok) throw new Error(`latest HTTP ${res.status}`)
  const body = await res.json()
  return body ?? null
}

async function fetchTask(jobId: string, taskId: string): Promise<ApiTask> {
  const res = await fetch(`/api/jobs/${jobId}/tasks/${taskId}`, {
    credentials: "include",
  })
  if (!res.ok) throw new Error(`task HTTP ${res.status}`)
  return res.json()
}

async function createTask(
  jobId: string,
  taskType: string,
  params: Record<string, unknown>,
): Promise<ApiTask> {
  const res = await fetch(`/api/jobs/${jobId}/tasks`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_type: taskType, params }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const msg = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`
    throw new Error(msg)
  }
  const body = await res.json()
  return body.task as ApiTask
}

/**
 * Hook for user-triggered background tasks (materials_pack / generate_video).
 *
 * On mount: fetches latest active task for this (jobId, taskType, fingerprint)
 * and restores state. When a task is in pending/running, polls with exponential
 * backoff on transient errors; stops on terminal states.
 */
export function useBackgroundTask(
  opts: UseBackgroundTaskOptions,
): UseBackgroundTaskReturn {
  const {
    jobId,
    taskType,
    paramsFingerprint,
    pollIntervalMs = DEFAULT_POLL_MS,
    stallHintMs = DEFAULT_STALL_MS,
  } = opts

  const [state, setState] = useState<BackgroundTaskState>({
    taskId: null,
    status: "idle",
    progress: null,
    result: null,
    error: null,
    runningForSec: 0,
  })

  const pollTimer = useRef<number | null>(null)
  const backoff = useRef(0)
  const runningSince = useRef<number | null>(null)
  const activeTaskId = useRef<string | null>(null)
  const pollInFlight = useRef(false)

  const clearTimer = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const applyTask = useCallback((task: ApiTask | null) => {
    if (!task) {
      activeTaskId.current = null
      setState((prev) => ({ ...prev, status: "idle" }))
      return
    }
    if (task.status === "pending" || task.status === "running") {
      activeTaskId.current = task.task_id
    } else if (activeTaskId.current === task.task_id) {
      activeTaskId.current = null
    }
    const now = Date.now()
    if (task.status === "running" && runningSince.current === null) {
      runningSince.current = now
    }
    if (task.status !== "running") {
      runningSince.current = null
    }
    const runningForSec =
      runningSince.current !== null ? Math.floor((now - runningSince.current) / 1000) : 0

    setState({
      taskId: task.task_id,
      status: task.status,
      progress: task.progress ?? null,
      result: task.result ?? null,
      error: task.error ?? null,
      runningForSec,
    })
  }, [])

  const scheduleNext = useCallback(
    (callback: () => void) => {
      clearTimer()
      if (isPageHidden()) return
      const delay = pollIntervalMs * Math.pow(2, Math.min(backoff.current, POLL_BACKOFF_MAX))
      pollTimer.current = window.setTimeout(callback, delay)
    },
    [clearTimer, pollIntervalMs],
  )

  const poll = useCallback(
    async (taskId: string) => {
      activeTaskId.current = taskId
      if (isPageHidden()) {
        clearTimer()
        return
      }
      if (pollInFlight.current) return
      pollInFlight.current = true
      try {
        const task = await fetchTask(jobId, taskId)
        backoff.current = 0
        applyTask(task)
        if (task.status === "pending" || task.status === "running") {
          scheduleNext(() => void poll(taskId))
        } else {
          activeTaskId.current = null
          clearTimer()
        }
      } catch (err) {
        backoff.current += 1
        if (backoff.current > POLL_BACKOFF_MAX) {
          activeTaskId.current = null
          clearTimer()
          setState((prev) => ({
            ...prev,
            status: "failed",
            error: err instanceof Error ? err.message : "轮询失败",
          }))
          return
        }
        scheduleNext(() => void poll(taskId))
      } finally {
        pollInFlight.current = false
      }
    },
    [jobId, applyTask, scheduleNext, clearTimer],
  )

  useEffect(() => {
    if (typeof document === "undefined") return
    const onVisibilityChange = () => {
      if (!document.hidden && activeTaskId.current) {
        void poll(activeTaskId.current)
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange)
    return () => document.removeEventListener("visibilitychange", onVisibilityChange)
  }, [poll])

  // Mount: restore state from the latest task for this (jobId, taskType,
  // fingerprint). The server returns the most recent task regardless of
  // status — so users returning to the page after a completion see
  // "素材包可下载" immediately instead of the initial "素材包" button.
  //
  // Failed tasks are NOT auto-restored: we leave the hook idle so the user
  // can start a fresh attempt. If they want to inspect the error they can
  // re-click and get the same failure again (tasks are idempotent by
  // fingerprint + the partial unique index allows re-attempts once a prior
  // one is in a terminal state).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const latest = await fetchLatest(jobId, taskType, paramsFingerprint)
        if (cancelled || !latest) return
        if (latest.status === "pending" || latest.status === "running") {
          activeTaskId.current = latest.task_id
          applyTask(latest)
          scheduleNext(() => void poll(latest.task_id))
        } else if (latest.status === "completed") {
          applyTask(latest)
        }
        // failed → leave idle; user starts fresh on next action
      } catch {
        // non-fatal; user just won't see recovered state
      }
    })()
    return () => {
      cancelled = true
      clearTimer()
    }
    // Fingerprint changes = different task identity; re-run
  }, [jobId, taskType, paramsFingerprint, applyTask, poll, scheduleNext, clearTimer])

  const startTask = useCallback(
    async (params: Record<string, unknown>): Promise<string | null> => {
      clearTimer()
      backoff.current = 0
      runningSince.current = null
      activeTaskId.current = null
      setState({
        taskId: null,
        status: "pending",
        progress: null,
        result: null,
        error: null,
        runningForSec: 0,
      })
      try {
        const task = await createTask(jobId, taskType, params)
        applyTask(task)
        if (task.status === "pending" || task.status === "running") {
          activeTaskId.current = task.task_id
          scheduleNext(() => void poll(task.task_id))
        }
        return task.task_id
      } catch (err) {
        setState({
          taskId: null,
          status: "failed",
          progress: null,
          result: null,
          error: err instanceof Error ? err.message : "创建任务失败",
          runningForSec: 0,
        })
        return null
      }
    },
    [jobId, taskType, applyTask, poll, scheduleNext, clearTimer],
  )

  const reset = useCallback(() => {
    clearTimer()
    backoff.current = 0
    runningSince.current = null
    activeTaskId.current = null
    setState({
      taskId: null,
      status: "idle",
      progress: null,
      result: null,
      error: null,
      runningForSec: 0,
    })
  }, [clearTimer])

  const isActive = state.status === "pending" || state.status === "running"
  const stalled = state.runningForSec * 1000 > stallHintMs

  return { ...state, startTask, reset, isActive, stalled }
}
