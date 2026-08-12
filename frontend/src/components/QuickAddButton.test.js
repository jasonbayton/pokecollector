import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  QUICK_ADD_CUSTOM,
  QUICK_ADD_QUEUE,
  QUICK_ADD_SCAN,
  QUICK_ADD_SEARCH,
} from '../contexts/ScannerContext'
import QuickAddButton, { QuickAddMenu } from './QuickAddButton'

const { scannerValue } = vi.hoisted(() => ({
  scannerValue: {
    runQuickAdd: vi.fn(),
    scanAttention: 0,
    scansActive: false,
  },
}))

// Only the hook is replaced. The action constants stay real, so a renamed
// action breaks these tests instead of quietly matching a stale string.
vi.mock('../contexts/ScannerContext', async (importOriginal) => ({
  ...(await importOriginal()),
  useScanner: () => scannerValue,
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

const renderMenu = (props = {}) => renderToStaticMarkup(createElement(QuickAddMenu, {
  open: false,
  onToggle: () => {},
  onSelect: () => {},
  ...props,
}))

// Calling the component directly returns the element tree with the handlers it
// built, which markup cannot carry. Legal because the mocked settings hook is a
// plain function, so no React hook is used outside a render.
const menuTree = (props = {}) => [...walk(QuickAddMenu({
  open: true,
  onToggle: () => {},
  onSelect: () => {},
  ...props,
}))]

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
  scannerValue.runQuickAdd.mockReset()
  scannerValue.scanAttention = 0
  scannerValue.scansActive = false
})

describe('QuickAddMenu', () => {
  it('sits in the bottom-right corner, clear of the bottom-left home button', () => {
    const markup = renderMenu()

    expect(markup).toContain('right:max(1rem, env(safe-area-inset-right))')
    expect(markup).toContain('bottom:max(1.5rem, env(safe-area-inset-bottom))')
    expect(markup).not.toContain('left:')
    expect(markup).not.toContain('left-')
  })

  it('draws nothing across the screen while closed', () => {
    // A permanent full-screen layer would sit over the home button and the page.
    const markup = renderMenu()

    expect(markup).not.toContain('inset-0')
    expect(markup).toContain('aria-expanded="false"')
    expect(markup).not.toContain('role="menu"')
  })

  it('stays under the dialog layer', () => {
    // Dialogs own z-50. A floating button drawn over an open sheet reads as a
    // rendering fault, which is why the public home button was lowered to 40.
    const markup = renderMenu()

    expect(markup).toContain('z-40')
    expect(markup).not.toContain('z-50')
  })

  it('offers the four quick-add destinations under existing labels', () => {
    const markup = renderMenu({ open: true })

    expect(markup).toContain('role="menu"')
    expect(markup).toContain('scanner.title')
    expect(markup).toContain('nav.cardSearch')
    expect(markup).toContain('cardSearch.createCustomCard')
    expect(markup).toContain('scanner.queueTitle')
  })

  it('reports which destination was chosen', () => {
    const onSelect = vi.fn()
    const items = menuTree({ onSelect }).filter(node => node.props?.role === 'menuitem')

    expect(items).toHaveLength(4)
    items.forEach(item => item.props.onClick())

    expect(onSelect.mock.calls.map(([action]) => action)).toEqual([
      QUICK_ADD_SCAN,
      QUICK_ADD_SEARCH,
      QUICK_ADD_CUSTOM,
      QUICK_ADD_QUEUE,
    ])
  })

  it('shows the number of scans waiting for a decision', () => {
    const markup = renderMenu({ attention: 7 })

    expect(markup).toContain('>7</span>')
    expect(markup).toContain('title="scanner.needReview"')
    expect(markup).not.toContain('animate-pulse')
  })

  it('caps the badge rather than stretching the button', () => {
    expect(renderMenu({ attention: 150 })).toContain('>99+</span>')
  })

  it('pulses while a job is still running with nothing to review yet', () => {
    const markup = renderMenu({ attention: 0, active: true })

    expect(markup).toContain('animate-pulse')
    expect(markup).toContain('title="scanner.processing"')
  })

  it('shows neither once the items are resolved and no job is running', () => {
    // The negative control for the badge: a queue that has been dealt with must
    // not leave a permanent decoration on a control the user sees everywhere.
    const markup = renderMenu({ attention: 0, active: false })

    expect(markup).not.toContain('animate-pulse')
    expect(markup).not.toContain('bg-yellow')
    expect(markup).not.toContain('title="scanner.needReview"')
  })
})

describe('QuickAddButton', () => {
  it('renders the control closed, with the queue state the provider published', () => {
    scannerValue.scanAttention = 4

    const markup = renderToStaticMarkup(createElement(
      MemoryRouter,
      { initialEntries: ['/collection'] },
      createElement(QuickAddButton),
    ))

    expect(markup).toContain('aria-label="quickAdd.title"')
    expect(markup).toContain('aria-expanded="false"')
    expect(markup).toContain('>4</span>')
  })
})
