import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import CardImageDialog from './CardImageDialog'

// Modal portals into document.body and reads window.matchMedia, neither of
// which exists under the repository's node test environment. Replacing it with
// a plain wrapper keeps the dialog's own markup renderable while still proving
// which title it asks for.
vi.mock('./ui/Modal', () => ({
  default: ({ title, children }) => createElement('div', { 'data-title': title }, children),
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

const render = props => renderToStaticMarkup(createElement(CardImageDialog, props))

describe('CardImageDialog', () => {
  it('requests the large print, not the thumbnail it was handed', () => {
    // The whole point of the dialog. A public payload only carries the small
    // proxy URL, so asking for that again would just upscale a grid tile.
    const markup = render({
      card: { id: 'base1-64_en', name: 'Starmie' },
      image: '/api/images/card/base1-64_en/small',
    })

    expect(markup).toContain('src="/api/images/card/base1-64_en/large"')
    expect(markup).not.toContain('/small')
  })

  it('falls back to the supplied artwork when a card has no usable id', () => {
    const markup = render({
      card: { id: 4211, name: 'Mystery' },
      image: '/api/images/card/whatever/small',
    })

    expect(markup).toContain('src="/api/images/card/whatever/small"')
  })

  it('titles the dialog with the card and captions it with set and rarity', () => {
    const markup = render({
      card: { id: 'base1-64_en', name: 'Starmie', set_id: 'Base Set', number: '64', rarity: 'Rare' },
    })

    expect(markup).toContain('data-title="Starmie"')
    expect(markup).toContain('BASE SET 64 · Rare')
  })

  it('sizes the artwork from its width so the ratio survives a clamped phone', () => {
    // A fixed height plus max-w-full silently stops matching the card once the
    // width clamps, and then overflows the bottom sheet.
    const markup = render({ card: { id: 'base1-64_en', name: 'Starmie' } })

    expect(markup).toContain('aspect-[2.5/3.5]')
    expect(markup).toContain('w-full')
    expect(markup).not.toMatch(/\bh-\[/)
  })

  it('renders nothing without a card', () => {
    expect(render({ card: null })).toBe('')
  })
})
