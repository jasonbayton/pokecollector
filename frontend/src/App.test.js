import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

const { authValue, entries, getScanJobs } = vi.hoisted(() => ({
  authValue: { user: { username: 'ash' }, loading: false, multiUser: true },
  entries: { value: ['/collection'] },
  getScanJobs: vi.fn(),
}))

// BrowserRouter needs a DOM history. MemoryRouter is the same Router with the
// entries handed to it, which also lets each test say where the user is.
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    BrowserRouter: ({ children }) => createElement(
      actual.MemoryRouter,
      { initialEntries: entries.value },
      children,
    ),
  }
})

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: { jobs: [] } }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

vi.mock('./contexts/AuthContext', () => ({
  AuthProvider: ({ children }) => children,
  useAuth: () => authValue,
}))

vi.mock('./contexts/SettingsContext', () => ({
  SettingsProvider: ({ children }) => children,
  useSettings: () => ({ t: key => key }),
}))

vi.mock('./contexts/ConfirmDialogContext', () => ({
  ConfirmDialogProvider: ({ children }) => children,
  useConfirmDialog: () => vi.fn(),
}))

vi.mock('./api/client', () => ({
  forceChangePassword: vi.fn(),
  getScanJobs,
}))

vi.mock('./hooks/useListScrollRestoration', () => ({
  useReleaseManualHistoryScrollRestoration: () => {},
}))

vi.mock('./components/AppNav', () => ({ default: () => null }))

const render = pathname => {
  entries.value = [pathname]
  return renderToStaticMarkup(createElement(App))
}

beforeEach(() => {
  authValue.user = { username: 'ash' }
  authValue.loading = false
  authValue.multiUser = true
  entries.value = ['/collection']
})

describe('App', () => {
  it.each(['/collection', '/', '/sets'])('mounts the scanner provider around %s, so quick add works there', pathname => {
    // Without the provider every one of these pages throws on the first render
    // of the quick-add control - "useScanner must be used within
    // ScannerProvider" - and the whole app renders nothing at all.
    const markup = render(pathname)

    expect(markup).toContain('aria-label="quickAdd.title"')
  })

  it('does not mount it for the login screen', () => {
    // It polls the signed-in user's scan queue. There is no signed-in user here.
    const markup = render('/login')

    expect(markup).not.toContain('aria-label="quickAdd.title"')
  })

  it('does not mount it for the public share pages', () => {
    const markup = render('/u/ash/collection')

    expect(markup).not.toContain('aria-label="quickAdd.title"')
  })

  it('does not mount it before the user is known', () => {
    authValue.user = null
    authValue.loading = true

    expect(render('/collection')).not.toContain('aria-label="quickAdd.title"')
  })
})
