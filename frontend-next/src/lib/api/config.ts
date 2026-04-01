const defaultJobApiBaseUrl = '/job-api'

export function resolveJobApiBaseUrl() {
  const configuredBaseUrl = process.env.NEXT_PUBLIC_JOB_API_BASE_URL?.trim()

  if (!configuredBaseUrl) {
    return defaultJobApiBaseUrl
  }

  return configuredBaseUrl.replace(/\/+$/, '')
}

export function buildBackendUrl(baseUrl: string, path: string) {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  const normalizedBaseUrl = baseUrl.trim().replace(/\/+$/, '')

  if (!normalizedBaseUrl) {
    return normalizedPath
  }

  if (
    normalizedBaseUrl.startsWith('http://') ||
    normalizedBaseUrl.startsWith('https://')
  ) {
    return new URL(normalizedPath, `${normalizedBaseUrl}/`).toString()
  }

  const relativeBase = normalizedBaseUrl.startsWith('/')
    ? normalizedBaseUrl
    : `/${normalizedBaseUrl}`

  return `${relativeBase}${normalizedPath}`
}
