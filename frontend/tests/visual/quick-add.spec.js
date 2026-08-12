import { expect, test } from '@playwright/test'

// The quick-add control is keyboard, pointer and layering behaviour, none of
// which a server-rendered unit test can see. These drive the real app with the
// backend stubbed, the way the card-page tests do.

const USER = {
  id: 1,
  username: 'Quick Adder',
  role: 'admin',
  is_active: true,
  must_change_password: false,
}

const JOB = {
  id: 1,
  total: 2,
  processed: 2,
  pending: 0,
  processing: 0,
  retrying: 0,
  failed: 0,
  active: 0,
  attention: 2,
  failed_attention: 0,
  expires_at: '2026-12-01T12:00:00',
  items: [],
}

const searchResult = index => ({
  id: `quick-add-card-${index}`,
  card_id: `quick-add-card-${index}`,
  name: `Catalogue card ${index}`,
  number: String(index),
  set_id: 'quick-add-set_en',
  set_name: 'Quick Add Set',
  rarity: 'Rare',
  supertype: 'Pokemon',
  types: ['Fire'],
  price_market: 3,
  price_trend: 3,
  variants_normal: true,
  lang: 'en',
})

async function installApiFixtures(page, writes) {
  await page.addInitScript(user => {
    localStorage.setItem('token', 'quick-add-token')
    localStorage.setItem('user', JSON.stringify(user))
    localStorage.setItem('app_language', 'en')
  }, USER)

  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    // The Vite source path /src/api/client.js also matches the broad glob.
    if (!path.startsWith('/api/')) {
      await route.continue()
      return
    }

    if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`)

    if (request.method() === 'POST' && path === '/api/cards/custom') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'quick-add-custom-1',
          card_id: 'quick-add-custom-1',
          name: 'Hand drawn Charizard',
          lang: 'en',
          variants_normal: true,
        }),
      })
      return
    }

    const responses = {
      '/api/auth/mode': { multi_user: true },
      '/api/auth/me': USER,
      '/api/settings/': {
        language: 'en',
        price_primary: 'trend',
        price_display: '["trend","avg","avg1","avg7","avg30","low"]',
        tcgdex_sync_languages: 'en',
        currency: 'EUR',
      },
      '/api/settings/exchange-rate': { rate: 1 },
      '/api/settings/tcgdex-filter-languages': ['en'],
      '/api/collection/': [],
      '/api/wishlist/': [],
      '/api/sets/': [],
      '/api/cards/custom': [],
      '/api/cards/search': { data: [searchResult(1), searchResult(2)], total_count: 120 },
      '/api/cards/recognize/jobs': { jobs: [JOB] },
      '/api/dashboard/': { total_value: 0, total_cost: 0, recent_additions: [], top_cards: [] },
      '/api/analytics/investment-tracker': [],
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(responses[path] ?? {}),
    })
  })
}

const quickAdd = page => page.getByRole('button', { name: 'Quick add', exact: true })

let writes

test.beforeEach(async ({ page }) => {
  writes = []
  await installApiFixtures(page, writes)
})

test('quick add opens the scanner from a page that is not the card search', async ({ page }) => {
  await page.goto('/collection')
  await quickAdd(page).click()
  await page.getByRole('menuitem', { name: 'Scan card' }).click()

  await expect(page.getByRole('button', { name: 'Take photo' })).toBeVisible()
  // The scanner opens over the page rather than moving the user to one.
  expect(new URL(page.url()).pathname).toBe('/collection')
})

test('a card created from quick add is offered to the collection, not just the catalogue', async ({ page }) => {
  await page.goto('/collection')
  await quickAdd(page).click()
  await page.getByRole('menuitem', { name: 'Create card manually' }).click()

  await page.getByPlaceholder('e.g. Charizard ex').fill('Hand drawn Charizard')
  await page.getByRole('button', { name: 'Create card & add' }).click()

  await page.getByRole('button', { name: 'Add to Collection' }).click()
  await expect.poll(() => writes).toContain('POST /api/collection/')
})

test('the scan queue is left to draw its own screen', async ({ page }) => {
  // The queue is a route rendered as a modal. A control drawn under its
  // backdrop is unusable, and clicking it dismisses the queue.
  await page.goto('/scans')
  await expect(page.getByRole('heading', { name: 'Scans' })).toBeVisible()

  await expect(quickAdd(page)).toHaveCount(0)
  expect(new URL(page.url()).pathname).toBe('/scans')
})

test('arrow keys stop turning the page while quick add owns the screen', async ({ page }) => {
  await page.goto('/search?q=quick&page=2')
  await expect(page.getByText('120')).toBeVisible()

  // Control: with nothing open, the keys page the results.
  await page.locator('body').press('ArrowRight')
  await expect.poll(() => new URL(page.url()).searchParams.get('page')).toBe('3')

  await quickAdd(page).click()
  await page.getByRole('menuitem', { name: 'Create card manually' }).click()
  await expect(page.getByPlaceholder('e.g. Charizard ex')).toBeVisible()

  await page.locator('body').press('ArrowRight')
  await page.waitForTimeout(200)
  expect(new URL(page.url()).searchParams.get('page')).toBe('3')
})

test('escape closes the menu wherever the user is looking, and hands focus back', async ({ page }) => {
  await page.goto('/collection')
  await quickAdd(page).click()
  await expect(page.getByRole('menu')).toBeVisible()

  // Focus starts inside the menu, so the keyboard user is already there.
  expect(await page.evaluate(() => document.activeElement?.getAttribute('role'))).toBe('menuitem')

  await page.evaluate(() => document.body.focus())
  await page.keyboard.press('Escape')

  await expect(page.getByRole('menu')).toHaveCount(0)
  expect(await page.evaluate(() => document.activeElement?.getAttribute('aria-label'))).toBe('Quick add')
})

test('the open menu covers the page and leaves the home button reachable again once closed', async ({ page }) => {
  await page.goto('/collection')
  await quickAdd(page).click()

  const menuItem = page.getByRole('menuitem', { name: 'Scan card' })
  const box = await menuItem.boundingBox()
  const topmost = await page.evaluate(([x, y]) => {
    const element = document.elementFromPoint(x, y)
    return Boolean(element?.closest('[role="menu"]'))
  }, [box.x + box.width / 2, box.y + box.height / 2])

  // Nothing is drawn over the menu it just opened.
  expect(topmost).toBe(true)

  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Navigation' })).toBeVisible()
})

test('the home screen leaves the scan count on the control that owns it', async ({ page }) => {
  await page.goto('/')

  const tile = page.getByRole('button', { name: 'Card Search' })
  await expect(tile).toBeVisible()
  await expect(tile.locator('.bg-yellow')).toHaveCount(0)

  // The count itself is not lost: it rides on the quick-add control, which is
  // on the home screen too.
  await expect(quickAdd(page).locator('.bg-yellow')).toHaveText('2')
})
