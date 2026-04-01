import path from 'node:path'
import { fileURLToPath } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const appBasePath = normalizeBasePath(env.VITE_APP_BASE_PATH)

  return {
    base: appBasePath,
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(fileURLToPath(new URL('.', import.meta.url)), './src'),
      },
    },
    server: {
      host: '127.0.0.1',
      port: 4173,
      // ARCHIVED: This Vite frontend is deprecated. Use frontend-next/ instead.
      // The /web-ui-api proxy (8876) has been removed. Only /job-api remains for reference.
      proxy: {
        '/job-api': {
          target: 'http://127.0.0.1:8877',
          changeOrigin: true,
          rewrite: (requestPath) => requestPath.replace(/^\/job-api/, ''),
        },
      },
    },
  }
})

function normalizeBasePath(value: string | undefined) {
  const normalizedValue = (value ?? '/').trim()

  if (!normalizedValue || normalizedValue === '/') {
    return '/'
  }

  return normalizedValue.startsWith('/')
    ? normalizedValue.replace(/\/?$/, '/')
    : `/${normalizedValue.replace(/\/?$/, '/')}`
}
