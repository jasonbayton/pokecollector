import { execSync } from 'child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const __dirname = dirname(fileURLToPath(import.meta.url))
const packageJson = JSON.parse(readFileSync(resolve(__dirname, 'package.json'), 'utf-8'))

// VERSION belongs to upstream and this fork deliberately does not bump it, so
// it reads 1.39.2 whichever fork release is built. The annotated tag is what
// identifies the build, and it is what the About panel should show.
function readForkRelease() {
  try {
    const described = execSync('git describe --tags --dirty', {
      cwd: resolve(__dirname, '..'),
      stdio: ['ignore', 'pipe', 'ignore'],
    }).toString().trim()
    return described || ''
  } catch {
    return ''
  }
}

function readAppVersion() {
  const forkRelease = readForkRelease()
  if (forkRelease) return forkRelease
  try {
    return readFileSync(resolve(__dirname, '..', 'VERSION'), 'utf-8').trim()
  } catch {
    return packageJson.version
  }
}

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(readAppVersion()),
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
