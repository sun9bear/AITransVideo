export function resolveAppBasePath() {
  const configuredBasePath = import.meta.env.VITE_APP_BASE_PATH?.trim()

  if (!configuredBasePath || configuredBasePath === '/') {
    return '/'
  }

  const normalizedBasePath = configuredBasePath.startsWith('/')
    ? configuredBasePath
    : `/${configuredBasePath}`

  return normalizedBasePath.replace(/\/+$/, '') || '/'
}
