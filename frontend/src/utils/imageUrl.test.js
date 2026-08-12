import { describe, expect, it } from 'vitest'

import { productImageUrl, resolveCardImageUrl } from './imageUrl'

describe('productImageUrl', () => {
  it('uses the opaque product image proxy for configured images', () => {
    expect(productImageUrl({
      image_url: 'https://images.example.test/box.webp',
      image_proxy_url: '/api/images/product/42?token=signed',
    })).toBe('/api/images/product/42?token=signed')
  })

  it('uses the existing card back without a configured image', () => {
    expect(productImageUrl({ id: 42, image_url: null, image_proxy_url: null })).toBe('/cardback.jpg')
    expect(productImageUrl(null)).toBe('/cardback.jpg')
  })
})

describe('resolveCardImageUrl', () => {
  it('asks the proxy for the size it was given', () => {
    // The enlarged card view depends on this: a public payload only ever
    // carries the small artwork, so asking for large is what stops the dialog
    // upscaling a grid thumbnail.
    expect(resolveCardImageUrl({ id: 'base1-64_en' })).toBe('/api/images/card/base1-64_en/small')
    expect(resolveCardImageUrl({ id: 'base1-64_en' }, 'large')).toBe('/api/images/card/base1-64_en/large')
  })

  it('encodes ids that are not URL safe', () => {
    // The backend percent-encodes the same id when it builds the public small
    // URL, and test_public_binders.py covers a card id carrying a space and a
    // hash. The two have to agree or the enlarged view 404s.
    expect(resolveCardImageUrl({ id: 'custom card#1' }, 'large'))
      .toBe('/api/images/card/custom%20card%231/large')
  })

  it('prefers card_id when the row also carries a numeric collection item id', () => {
    expect(resolveCardImageUrl({ id: 4211, card_id: 'sv1-1_de' }, 'large'))
      .toBe('/api/images/card/sv1-1_de/large')
  })

  it('falls back to the supplied artwork when there is no usable card id', () => {
    expect(resolveCardImageUrl({ id: 4211, image: 'https://assets.example/base1/64' }, 'large'))
      .toBe('https://assets.example/base1/64/high.webp')
    expect(resolveCardImageUrl({ id: 4211, images_large: 'https://assets.example/big.webp' }, 'large'))
      .toBe('https://assets.example/big.webp')
  })

  it('is null when a card carries no artwork at all', () => {
    expect(resolveCardImageUrl({ id: 4211 }, 'large')).toBe(null)
    expect(resolveCardImageUrl(null, 'large')).toBe(null)
  })
})
