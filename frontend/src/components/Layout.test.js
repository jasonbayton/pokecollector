import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import Layout from './Layout'

const { navigate, scannerValue } = vi.hoisted(() => ({
  navigate: vi.fn(),
  scannerValue: { runQuickAdd: vi.fn(), scanAttention: 0, scansActive: false },
}))

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  useNavigate: () => navigate,
}))

vi.mock('../contexts/ScannerContext', async (importOriginal) => ({
  ...(await importOriginal()),
  useScanner: () => scannerValue,
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { username: 'ash' }, logout: vi.fn(), multiUser: true }),
}))

vi.mock('../hooks/useListScrollRestoration', () => ({
  useReleaseManualHistoryScrollRestoration: () => {},
}))

// AppNav is left in place but wrapped, so the test can reach the click handler
// the pokeball was built with. Markup alone cannot carry a handler.
let appNavTree
vi.mock('./AppNav', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    default: props => {
      appNavTree = actual.default(props)
      return appNavTree
    },
  }
})

const render = pathname => renderToStaticMarkup(createElement(
  MemoryRouter,
  { initialEntries: [pathname] },
  createElement(Layout),
))

function* walk(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child)
    return
  }
  if (!node || typeof node !== 'object') return
  yield node
  yield* walk(node.props?.children)
}

beforeEach(() => {
  appNavTree = null
  navigate.mockReset()
})

describe('Layout', () => {
  it.each(['/collection', '/sets', '/'])('renders the quick-add control on %s', pathname => {
    const markup = render(pathname)

    expect(markup).toContain('aria-label="quickAdd.title"')
  })

  it('drops the whole nav bar on the home route', () => {
    // This is why the control cannot live in AppNav: the home screen has none.
    const home = render('/')
    const collection = render('/collection')

    expect(home).not.toContain('aria-label="home.navigation"')
    expect(collection).toContain('aria-label="home.navigation"')
  })

  it('leaves the bottom-left pokeball working and uncovered', () => {
    const markup = render('/collection')
    const pokeball = [...walk(appNavTree)]
      .find(node => node.props?.['aria-label'] === 'home.navigation')

    expect(pokeball).toBeDefined()
    pokeball.props.onClick()
    expect(navigate).toHaveBeenCalledWith('/')

    // Opposite corners, and nothing full-screen is drawn while the quick-add
    // menu is closed.
    expect(pokeball.props.className).toContain('left-4')
    expect(markup).toContain('right:max(1rem, env(safe-area-inset-right))')
    expect(markup).not.toContain('inset-0')
  })
})
