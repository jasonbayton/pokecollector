import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'react-hot-toast'
import App from './App.jsx'
import './components/card-system/tokens.css'
import './components/card-system/styles.css'
import './index.css'

// Apply saved theme before first paint to prevent flash
const savedTheme = localStorage.getItem('theme')
if (savedTheme && savedTheme !== 'default') {
  document.documentElement.setAttribute('data-theme', savedTheme)
}

// A deploy replaces every hashed chunk and deletes the previous ones. A tab
// that is still running the older build asks for a chunk that has gone, the
// lazy route fails to load, and the screen stays blank. Reloading picks up the
// current build. The timestamp guard means a chunk that is genuinely missing
// costs one reload rather than an endless loop.
window.addEventListener('vite:preloadError', (event) => {
  const KEY = 'pc:chunk-reload-at'
  const last = Number(sessionStorage.getItem(KEY) || 0)
  if (Date.now() - last < 10000) return
  sessionStorage.setItem(KEY, String(Date.now()))
  event.preventDefault()
  window.location.reload()
})

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30000,
      refetchOnWindowFocus: false,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      <Toaster
        position="top-center"
        toastOptions={{
          style: {
            background: '#1a1a2e',
            color: '#fff',
            border: '1px solid #2a2a4a',
          },
          success: {
            iconTheme: { primary: '#10b981', secondary: '#fff' },
          },
          error: {
            iconTheme: { primary: '#EE1515', secondary: '#fff' },
          },
        }}
      />
    </QueryClientProvider>
  </React.StrictMode>
)
