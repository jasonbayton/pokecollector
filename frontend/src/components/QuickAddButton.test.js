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
import QuickAddButton, { QuickAddMenu, escapeDismisses } from './QuickAddButton'

const { scannerValue } = vi.hoisted(() => ({
  scannerValue: {
    runQuickAdd: vi.fn(),
    scanAttention: 0,
    scansActive: false,
    // The menu's open state lives in the provider so the card search can
    // suspend its arrow keys while the menu covers it.
    quickAddMenuOpen: false,
    setQuickAddMenuOpen: vi.fn(),
  },
}))

// Only the hook is replaced. The action constants and the route policy stay
// real, so a renamed action or a changed rule breaks these tests instead of
// quietly matching a stale string.
vi.mock('../contexts/ScannerContext', async (importOriginal) => ({
  ...(await importOriginal()),
  useScanner: () => scannerValue,
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

// z-40 is the nav layer (AppNav's sticky strip and the floating home button),
// z-50 the dialog layer (Modal and Sheet).
const NAV_LAYER = 40
const DIALOG_LAYER = 50

// Reads the z value out of a class string, whether written z-40 or z-[46].
const zOf = (markup, prefix) => {
  const at = markup.indexOf(prefix)
  if (at < 0) return -1
  const rest = markup.slice(at + prefix.length)
  const match = rest.match(/^\[?(\d+)\]?/)
  return match ? Number(match[1]) : -1
}

const renderMenu = (props = {}) => renderToStaticMarkup(createElement(QuickAddMenu, {
  open: false,
  onToggle: () => {},
  onClose: () => {},
  onSelect: () => {},
  ...props,
}))

// Calling the component directly returns the element tree with the handlers it
// built, which markup cannot carry. Legal because the mocked settings hook is a
// plain function, so no React hook is used outside a render.
const menuTree = (props = {}) => [...walk(QuickAddMenu({
  open: true,
  onToggle: () => {},
  onClose: () => {},
  onSelect: () => {},
  ...props,
}))]

const renderControl = pathname => renderToStaticMarkup(createElement(
  MemoryRouter,
  { initialEntries: [pathname] },
  createElement(QuickAddButton),
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

  it('stays under the dialog layer while closed', () => {
    // Dialogs own z-50. A floating button drawn over an open sheet reads as a
    // rendering fault, which is why the public home button was lowered too.
    const markup = renderMenu()

    expect(zOf(markup, 'fixed z-')).toBeLessThan(50)
    expect(markup).not.toContain('z-50')
  })

  it('stacks the open menu over the page, over its backdrop, and still under dialogs', () => {
    // The state that actually matters, and the one the closed render cannot
    // speak for: the backdrop has to cover the page but not the menu, and the
    // whole control has to stay below the dialog layer.
    const markup = renderMenu({ open: true })
    const backdrop = zOf(markup, 'fixed inset-0 z-')
    const control = zOf(markup, 'fixed z-')

    // Asserted as an ordering rather than as literal class names, so
    // renumbering the scale does not break a test about layering.
    // NAV_LAYER is what the sticky header strip and the home button use; the
    // backdrop must cover them, or they stay lit and clickable through the dim.
    expect(backdrop).toBeGreaterThan(NAV_LAYER)
    expect(control).toBeGreaterThan(backdrop)
    expect(control).toBeLessThan(DIALOG_LAYER)
    expect(markup).toContain('role="menu"')
    expect(markup).toContain('aria-expanded="true"')
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

  it('dismisses rather than toggles when the backdrop is clicked', () => {
    // The click that reaches the backdrop has already blurred the menu, and the
    // blur closes it. A toggle here would reopen what the blur just closed.
    const onClose = vi.fn()
    const onToggle = vi.fn()
    const backdrop = menuTree({ onClose, onToggle })
      .find(node => node.props?.['aria-label'] === 'common.close')

    expect(backdrop).toBeDefined()
    backdrop.props.onClick()

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('closes when focus leaves the control, and stays open while it moves inside it', () => {
    // A menu is not a dialog: tab out of it and it goes away, rather than
    // trapping the user in four items.
    const onClose = vi.fn()
    const container = menuTree({ onClose }).find(node => typeof node.props?.onBlur === 'function')
    const inside = { id: 'menu-item' }
    const currentTarget = { contains: node => node === inside }

    container.props.onBlur({ currentTarget, relatedTarget: inside })
    expect(onClose).not.toHaveBeenCalled()

    container.props.onBlur({ currentTarget, relatedTarget: { id: 'somewhere-else' } })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the number of scans waiting for a decision', () => {
    const markup = renderMenu({ attention: 7 })

    expect(markup).toContain('>7</span>')
    // The queue writes this key after a count, and it reads as a fragment
    // without one. The tooltip carries the same count.
    expect(markup).toContain('title="7 scanner.needReview"')
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
    expect(markup).not.toContain('scanner.needReview')
  })
})

describe('QuickAddButton', () => {
  it('renders the control closed, with the queue state the provider published', () => {
    scannerValue.scanAttention = 4

    const markup = renderControl('/collection')

    expect(markup).toContain('aria-label="quickAdd.title"')
    expect(markup).toContain('aria-expanded="false"')
    expect(markup).toContain('>4</span>')
  })

  it('draws nothing at all on the scan queue', () => {
    // The queue is a route that renders itself as a modal over the page. The
    // control would sit under its backdrop: visible, unclickable, and clicking
    // it would dismiss the queue and land the user on /search.
    scannerValue.scanAttention = 4

    expect(renderControl('/scans')).toBe('')
    expect(renderControl('/scans/12')).toBe('')
  })
})

describe('escape dismissal', () => {
  const press = key => {
    const close = vi.fn()
    const event = { key, preventDefault: vi.fn() }
    escapeDismisses(close)(event)
    return { close, event }
  }

  it('closes the menu and puts focus back on the button that opened it', () => {
    const { close, event } = press('Escape')

    expect(close).toHaveBeenCalledWith({ restoreFocus: true })
    expect(event.preventDefault).toHaveBeenCalledTimes(1)
  })

  it('leaves every other key to the page', () => {
    // The listener is on the document, so swallowing anything else would take
    // keys away from whatever the user is actually typing into.
    for (const key of ['Enter', 'ArrowLeft', 'a', 'Tab']) {
      const { close, event } = press(key)
      expect(close, `${key} should not dismiss`).not.toHaveBeenCalled()
      expect(event.preventDefault, `${key} should not be swallowed`).not.toHaveBeenCalled()
    }
  })
})
