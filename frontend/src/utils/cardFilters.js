/**
 * Shared card category and subtype filter labels.
 *
 * These lived inside Collection. They are shared so that any page offering the
 * same filters selects the same cards: a card's subtype is spread across
 * `subtypes`, `trainer_type`, `energy_type` and `stage`, and a filter that
 * reads only the first silently misses matches.
 */

export const CARD_CATEGORY_OPTIONS = ['Pokémon', 'Trainer', 'Energy']
export const CARD_SUBTYPE_OPTIONS = ['Item', 'Supporter', 'Stadium', 'Pokémon Tool', 'EX', 'ex', 'GX', 'Stage 1', 'Stage 2', 'Basic']

export const normalizeCardFilterValue = (value) => String(value || '')
  .normalize('NFD')
  .replace(/[\u0300-\u036f]/g, '')
  .trim()
  .toLowerCase()

export const normalizeCardFilterLabelKey = (value) => String(value || '')
  .normalize('NFD')
  .replace(/[\u0300-\u036f]/g, '')
  .trim()

const CARD_FILTER_DISPLAY_LABELS = new Map(
  [...CARD_CATEGORY_OPTIONS, ...CARD_SUBTYPE_OPTIONS].map(label => [normalizeCardFilterLabelKey(label), label])
)

export const getPreferredCardFilterLabel = (value) => (
  CARD_FILTER_DISPLAY_LABELS.get(normalizeCardFilterLabelKey(value)) || String(value || '').trim()
)

export const getCardCategoryLabel = (card) => {
  const supertype = String(card?.supertype || '').trim()
  if (normalizeCardFilterValue(supertype) === 'pokemon') return 'Pokémon'
  if (supertype) return getPreferredCardFilterLabel(supertype)
  return ''
}

export const getCardSubtypeLabels = (card) => {
  const labels = new Set()
  ;(card?.subtypes || []).forEach(subtype => {
    if (subtype) labels.add(getPreferredCardFilterLabel(subtype))
  })
  ;[card?.trainer_type, card?.energy_type, card?.stage].forEach(subtype => {
    if (subtype) labels.add(getPreferredCardFilterLabel(subtype))
  })
  return [...labels].filter(Boolean)
}

export const sortCardFilterLabels = (preferredOrder, labels) => {
  const preferredIndex = new Map(preferredOrder.map((label, index) => [normalizeCardFilterLabelKey(label), index]))
  return [...labels].sort((a, b) => {
    const indexA = preferredIndex.get(normalizeCardFilterLabelKey(a))
    const indexB = preferredIndex.get(normalizeCardFilterLabelKey(b))
    if (indexA !== undefined || indexB !== undefined) {
      return (indexA ?? Number.MAX_SAFE_INTEGER) - (indexB ?? Number.MAX_SAFE_INTEGER)
    }
    return a.localeCompare(b)
  })
}
