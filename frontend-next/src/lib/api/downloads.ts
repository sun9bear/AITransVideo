import { buildBackendUrl, resolveJobApiBaseUrl } from '@/lib/api/config'
import { DOWNLOADABLE_ARTIFACT_KEYS, type DownloadableArtifactKey } from '@/types/jobs'

type BuildResultDownloadUrlInput = {
  downloadKey: DownloadableArtifactKey
  jobId?: string | null
  projectDir?: string | null
}

export function buildResultDownloadUrl(
  input: BuildResultDownloadUrlInput,
) {
  const downloadKey = input.downloadKey
  const jobId = input.jobId?.trim() ?? ''

  if (!jobId) {
    return null
  }

  if (!DOWNLOADABLE_ARTIFACT_KEYS.includes(downloadKey)) {
    return null
  }

  // TTS segments zip has its own endpoint
  if (downloadKey === 'editor.tts_segments_zip') {
    return buildBackendUrl(
      resolveJobApiBaseUrl(),
      `/jobs/${jobId}/tts-segments-zip`,
    )
  }

  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/download/${downloadKey}`,
  )
}
