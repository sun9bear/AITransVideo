const defaultJobApiBaseUrl = '/job-api'
const defaultWebUiBaseUrl = '/web-ui-api'

export function resolveJobApiBaseUrl() {
  const configuredBaseUrl = import.meta.env.VITE_JOB_API_BASE_URL?.trim()

  if (!configuredBaseUrl) {
    return defaultJobApiBaseUrl
  }

  return configuredBaseUrl.replace(/\/+$/, '')
}

export function resolveWebUiBaseUrl() {
  const configuredBaseUrl = import.meta.env.VITE_WEB_UI_BASE_URL?.trim()

  if (!configuredBaseUrl) {
    return defaultWebUiBaseUrl
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
