import { apiClient } from '@/lib/api/client'
import {
  toJobLogEntries,
  toJobSummary,
  toProjectResultSummary,
  toResultDownloadItems,
} from '@/lib/api/mappers'
import type {
  ApiJobArtifactsResponse,
  ApiJobListResponse,
  ApiJobLogsResponse,
  ApiJobRecord,
  ApiJobResultSummary,
} from '@/types/api'
import {
  type CreateTranslationJobInput,
  type JobLogEntry,
  type JobSummary,
  type ProjectDetailResource,
  type ProjectResultSummary,
  type ResultDownloadItem,
} from '@/types/jobs'

export async function listJobs(): Promise<JobSummary[]> {
  const payload = await apiClient.get<ApiJobListResponse>('/jobs')
  return payload.jobs.map(toJobSummary)
}

export async function getJob(jobId: string): Promise<JobSummary> {
  const payload = await apiClient.get<ApiJobRecord>(`/jobs/${jobId}`)
  return toJobSummary(payload)
}

export async function submitTranslationJob(
  input: CreateTranslationJobInput,
): Promise<JobSummary> {
  // When a local upload is in play, pass the original filename alongside
  // the sanitised upload path. The gateway's display_name orchestrator
  // truncates it into a friendly task title (plan §6.2 branch 3).
  const sourceType = input.sourceType ?? 'youtube_url'
  const source: Record<string, string> = {
    type: sourceType,
    value: sourceType === 'local_video' ? (input.localFilePath ?? '') : input.youtubeUrl,
  }
  if (sourceType === 'local_video' && input.localFileName) {
    source.filename = input.localFileName
  }

  const payload = await apiClient.post<ApiJobRecord>('/jobs', {
    body: {
      job_type: 'localize_video',
      output_target: 'editor',
      source,
      speakers: input.speakers,
      voice_a: input.voiceA,
      voice_b: input.voiceB,
      transcription_method: input.transcriptionMethod ?? 'assemblyai',
      service_mode: input.service_mode ?? 'express',
    },
  })

  return toJobSummary(payload)
}

export async function continueJob(jobId: string): Promise<JobSummary> {
  const payload = await apiClient.post<ApiJobRecord>(`/jobs/${jobId}/continue`, {
    body: {},
  })
  return toJobSummary(payload)
}

/**
 * Rename a job's user-visible title. Hits the gateway's rename endpoint
 * (``PATCH /gateway/jobs/{id}``) which handles ownership + collision
 * resolution (plan §6.5 / D16). Empty / whitespace-only values are
 * rejected on the server with 400 — callers should validate client-side
 * before calling.
 *
 * ⚠ This endpoint lives at the raw ``/gateway/*`` path, not under the
 * client's default ``/job-api`` base. We use ``fetch`` directly here so
 * the apiClient's prefix assumption doesn't get in the way.
 */
export async function renameJob(jobId: string, displayName: string): Promise<JobSummary> {
  const response = await fetch(`/gateway/jobs/${jobId}`, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ display_name: displayName }),
  })
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}))
    const message = errorBody?.message ?? errorBody?.detail ?? `重命名失败（HTTP ${response.status}）`
    throw new Error(message)
  }
  const payload = (await response.json()) as ApiJobRecord
  return toJobSummary(payload)
}

export async function getJobLogs(jobId: string): Promise<JobLogEntry[]> {
  const payload = await apiClient.get<ApiJobLogsResponse>(`/jobs/${jobId}/logs`)
  return toJobLogEntries(payload.events)
}

export async function getProjectResultSummary(
  jobId: string,
): Promise<ProjectResultSummary> {
  const payload = await apiClient.get<ApiJobResultSummary>(
    `/jobs/${jobId}/result-summary`,
  )
  return toProjectResultSummary(payload)
}

export async function getProjectArtifacts(
  jobId: string,
): Promise<ResultDownloadItem[]> {
  const payload = await apiClient.get<ApiJobArtifactsResponse>(
    `/jobs/${jobId}/artifacts`,
  )
  return toResultDownloadItems(payload)
}

export async function getProjectDetail(
  jobId: string,
): Promise<ProjectDetailResource> {
  const [job, result] = await Promise.all([
    getJob(jobId),
    getProjectResultSummary(jobId),
  ])
  let downloads: ResultDownloadItem[] = []
  try {
    downloads = await getProjectArtifacts(jobId)
  } catch {
    downloads = []
  }

  return {
    downloads,
    job,
    result,
  }
}
