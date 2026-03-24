import { buildBackendUrl, resolveWebUiBaseUrl } from '@/lib/api/config'
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
  const projectDir = input.projectDir?.trim() ?? ''

  if (!projectDir) {
    if (!jobId) {
      return null
    }
  }

  if (!DOWNLOADABLE_ARTIFACT_KEYS.includes(downloadKey)) {
    return null
  }

  const query = new URLSearchParams({ key: downloadKey })

  if (jobId) {
    query.set('job_id', jobId)
  }

  if (projectDir) {
    // Backward compatibility for legacy web-ui download resolver.
    query.set('project_dir', projectDir)
  }

  return buildBackendUrl(
    resolveWebUiBaseUrl(),
    `/api/result-download?${query.toString()}`,
  )
}
