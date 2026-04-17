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
  const payload = await apiClient.post<ApiJobRecord>('/jobs', {
    body: {
      job_type: 'localize_video',
      output_target: 'publish',
      source: {
        type: input.sourceType ?? 'youtube_url',
        value: input.sourceType === 'local_video' ? (input.localFilePath ?? '') : input.youtubeUrl,
      },
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
