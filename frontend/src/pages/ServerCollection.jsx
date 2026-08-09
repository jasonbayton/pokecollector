import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Users, Search, LayoutGrid, List, Filter, X } from 'lucide-react'

import { getServerCollection } from '../api/client'
import { CardModal } from '../components/CardItem'
import { CardDisplay, CardRow } from '../components/card-system'
import TcgdexLanguageSelect from '../components/TcgdexLanguageSelect'
import { useSettings } from '../contexts/SettingsContext'
import { getCardCategoryLabel, getCardSubtypeLabels, normalizeCardFilterLabelKey } from '../utils/cardFilters'
import { resolveCardImageUrl } from '../utils/imageUrl'
import { getEffectiveCardPrice } from '../utils/prices'

/** Who holds this card, shown under the artwork. The whole point of the view. */
function OwnerSummary({ owners }) {
  return (
    <span className="block truncate text-[11px] text-blue">
      {owners.map((o) => `${o.username} ×${o.quantity}`).join(', ')}
    </span>
  )
}

/**
 * The same basis Collection filters on: the configured price field, via the
 * shared helper that skips non-positive values and handles reverse-holo
 * pricing. A row can span several printings, so the dearest one anyone holds
 * is what the range is matched against.
 */
export function entryPrice(entry, priceField) {
  const card = entry.card || {}
  const variants = entry.variants || []
  if (!variants.length) return getEffectiveCardPrice(card, null, priceField)
  return Math.max(...variants.map((variant) => getEffectiveCardPrice(card, variant, priceField)))
}

/**
 * Every contributed collection, merged. Deliberately read-only: this answers
 * "does anyone already have this card" and nothing here should invite an edit,
 * because the cards belong to other people.
 *
 * The filters mirror Collection's card-level ones. Condition and variant are
 * deliberately absent: they describe a single copy, and one row here can span
 * several people's copies in different conditions.
 */
export default function ServerCollection() {
  const { t, formatPrice, pricePrimaryField } = useSettings()
  const [search, setSearch] = useState('')
  const [view, setView] = useState('grid')
  const [selectedCard, setSelectedCard] = useState(null)
  const [ownerFilter, setOwnerFilter] = useState('')

  const [showFilters, setShowFilters] = useState(false)
  const [filterSet, setFilterSet] = useState('')
  const [filterRarity, setFilterRarity] = useState('')
  const [filterType, setFilterType] = useState('')
  const [filterCategory, setFilterCategory] = useState('')
  const [filterSubtype, setFilterSubtype] = useState('')
  const [filterLang, setFilterLang] = useState('')
  const [filterMinPrice, setFilterMinPrice] = useState('')
  const [filterMaxPrice, setFilterMaxPrice] = useState('')
  const [filterDuplicates, setFilterDuplicates] = useState(false)

  const { data, isLoading, isError } = useQuery({
    // Valued with the same price field the filter uses, or the totals on screen
    // would disagree with the range that selected them.
    queryKey: ['server-collection', pricePrimaryField],
    queryFn: () => getServerCollection({ price_field: pricePrimaryField }),
  })

  const entries = data?.data || []
  const contributors = data?.contributors || []

  // Options come from what is actually shared, so no single filter offers a
  // choice nothing matches. Combinations can still come back empty.
  const options = useMemo(() => {
    const sets = new Map()
    const rarities = new Set()
    const types = new Set()
    const categories = new Set()
    const subtypes = new Set()
    const languages = new Set()
    for (const entry of entries) {
      const card = entry.card || {}
      if (card.set_id) sets.set(card.set_id, card.set_ref?.name || card.set_id)
      if (card.rarity) rarities.add(card.rarity)
      for (const type of card.types || []) types.add(type)
      const category = getCardCategoryLabel(card)
      if (category) categories.add(category)
      for (const subtype of getCardSubtypeLabels(card)) subtypes.add(subtype)
      if (card.lang) languages.add(card.lang)
    }
    const sorted = (set) => [...set].sort((a, b) => a.localeCompare(b))
    return {
      sets: [...sets.entries()].sort((a, b) => a[1].localeCompare(b[1])),
      rarities: sorted(rarities),
      types: sorted(types),
      categories: sorted(categories),
      subtypes: sorted(subtypes),
      // TcgdexLanguageSelect renders option.code, so codes alone render blank.
      languages: sorted(languages).map((code) => ({ code })),
    }
  }, [entries])

  const hasActiveFilters = Boolean(
    filterSet || filterRarity || filterType || filterCategory || filterSubtype
    || filterLang || filterMinPrice || filterMaxPrice || filterDuplicates
  )

  const resetFilters = () => {
    setFilterSet('')
    setFilterRarity('')
    setFilterType('')
    setFilterCategory('')
    setFilterSubtype('')
    setFilterLang('')
    setFilterMinPrice('')
    setFilterMaxPrice('')
    setFilterDuplicates(false)
  }

  const visible = useMemo(() => {
    const term = search.trim().toLowerCase()
    return entries.filter((entry) => {
      if (ownerFilter && !entry.owners.some((o) => o.username === ownerFilter)) return false

      const card = entry.card || {}
      if (filterSet && card.set_id !== filterSet) return false
      if (filterRarity && card.rarity !== filterRarity) return false
      if (filterType && !(card.types || []).includes(filterType)) return false
      if (filterCategory && getCardCategoryLabel(card) !== filterCategory) return false
      if (filterSubtype && !getCardSubtypeLabels(card)
        .map(normalizeCardFilterLabelKey)
        .includes(normalizeCardFilterLabelKey(filterSubtype))) return false
      if (filterLang && card.lang !== filterLang) return false

      const price = entryPrice(entry, pricePrimaryField)
      if (filterMinPrice && price < parseFloat(filterMinPrice)) return false
      if (filterMaxPrice && price > parseFloat(filterMaxPrice)) return false
      // "Duplicates" across the whole server: more than one copy exists,
      // whether that is one person holding two or two people holding one each.
      if (filterDuplicates && entry.quantity < 2) return false

      if (!term) return true
      return [card.name, card.set_ref?.name, card.set_ref?.abbreviation, card.number]
        .some((field) => String(field || '').toLowerCase().includes(term))
    })
  }, [
    entries, search, ownerFilter, filterSet, filterRarity, filterType, filterCategory,
    filterSubtype, filterLang, filterMinPrice, filterMaxPrice, filterDuplicates,
    pricePrimaryField,
  ])

  if (isLoading) {
    return <div className="py-4 text-sm text-text-muted">{t('common.loading')}</div>
  }
  if (isError) {
    return <div className="py-4 text-sm text-brand-red">{t('common.error')}</div>
  }

  return (
    <div className="space-y-4 pb-2">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-bold text-text-primary">
          <Users size={20} className="text-blue" />
          {t('serverCollection.title')}
        </h1>
        <p className="mt-1 text-sm text-text-muted">
          {data.unique_cards} {t('serverCollection.uniqueCards')} · {data.total_cards} {t('serverCollection.cards')} · {formatPrice(data.total_value)}
        </p>
      </div>

      {contributors.length === 0 ? (
        <div className="card p-6 text-center">
          <p className="text-sm text-text-primary">{t('serverCollection.nobodySharing')}</p>
          <p className="mt-1 text-xs text-text-muted">{t('serverCollection.nobodySharingHint')}</p>
        </div>
      ) : (
        <>
          <div className="card space-y-3 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[11px] uppercase tracking-[0.2em] text-text-muted">
                {t('serverCollection.contributors')}
              </span>
              <button
                type="button"
                onClick={() => setOwnerFilter('')}
                className={ownerFilter === '' ? 'btn-primary-sm' : 'btn-ghost px-3 py-1 text-xs'}
              >
                {t('common.all')}
              </button>
              {contributors.map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => setOwnerFilter(ownerFilter === name ? '' : name)}
                  className={ownerFilter === name ? 'btn-primary-sm' : 'btn-ghost px-3 py-1 text-xs'}
                >
                  {name}
                </button>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[12rem] flex-1">
                <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('serverCollection.searchPlaceholder')}
                  className="input w-full pl-9 text-sm"
                />
              </div>

              <button
                type="button"
                onClick={() => setShowFilters((f) => !f)}
                className={`btn-ghost text-sm py-1.5 ${showFilters || hasActiveFilters ? 'border-brand-red/30 text-brand-red' : ''}`}
              >
                <Filter size={14} /> {t('common.filter')}
                {hasActiveFilters && (
                  <span className="ml-1 flex h-4 w-4 items-center justify-center rounded-full bg-brand-red text-xs leading-none text-white">!</span>
                )}
              </button>

              {hasActiveFilters && (
                <button type="button" onClick={resetFilters} className="btn-ghost text-sm py-1.5">
                  <X size={14} /> {t('collection.clearFilters')}
                </button>
              )}

              <button
                type="button"
                onClick={() => setView(view === 'grid' ? 'list' : 'grid')}
                className="btn-ghost px-3"
                title={t('serverCollection.toggleView')}
              >
                {view === 'grid' ? <List size={16} /> : <LayoutGrid size={16} />}
              </button>
            </div>

            {showFilters && (
              <div className="grid grid-cols-2 gap-3 border-t border-border pt-3 sm:grid-cols-3">
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterSet')}</label>
                  <select className="select py-1.5 text-sm" value={filterSet} onChange={(e) => setFilterSet(e.target.value)}>
                    <option value="">{t('collection.allSets')}</option>
                    {options.sets.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('common.rarity')}</label>
                  <select className="select py-1.5 text-sm" value={filterRarity} onChange={(e) => setFilterRarity(e.target.value)}>
                    <option value="">{t('common.allRarities')}</option>
                    {options.rarities.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterEnergyType')}</label>
                  <select className="select py-1.5 text-sm" value={filterType} onChange={(e) => setFilterType(e.target.value)}>
                    <option value="">{t('collection.allEnergyTypes')}</option>
                    {options.types.map((tp) => <option key={tp} value={tp}>{tp}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterCardCategory')}</label>
                  <select className="select py-1.5 text-sm" value={filterCategory} onChange={(e) => setFilterCategory(e.target.value)}>
                    <option value="">{t('common.all')}</option>
                    {options.categories.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterSubtype')}</label>
                  <select className="select py-1.5 text-sm" value={filterSubtype} onChange={(e) => setFilterSubtype(e.target.value)}>
                    <option value="">{t('common.all')}</option>
                    {options.subtypes.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('lang.filter')}</label>
                  <TcgdexLanguageSelect
                    value={filterLang || 'all'}
                    includeAll
                    allLabel={t('lang.all')}
                    compact
                    languages={options.languages}
                    onChange={(value) => setFilterLang(value === 'all' ? '' : value)}
                    className="select py-1.5 text-sm"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterMinPrice')}</label>
                  <input
                    type="number" min="0" step="0.01" placeholder="0"
                    value={filterMinPrice}
                    onChange={(e) => setFilterMinPrice(e.target.value)}
                    className="input py-1.5 text-sm"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-text-muted">{t('collection.filterMaxPrice')}</label>
                  <input
                    type="number" min="0" step="0.01" placeholder="∞"
                    value={filterMaxPrice}
                    onChange={(e) => setFilterMaxPrice(e.target.value)}
                    className="input py-1.5 text-sm"
                  />
                </div>
                <div className="col-span-2 flex items-center gap-2 sm:col-span-1">
                  <label className="flex cursor-pointer items-center gap-2">
                    <input
                      type="checkbox"
                      checked={filterDuplicates}
                      onChange={(e) => setFilterDuplicates(e.target.checked)}
                      className="h-4 w-4 accent-brand-red"
                    />
                    <span className="text-xs text-text-secondary">{t('collection.filterDuplicates')}</span>
                  </label>
                </div>
              </div>
            )}
          </div>

          {visible.length === 0 ? (
            <p className="p-4 text-center text-sm text-text-muted">{t('common.noResults')}</p>
          ) : view === 'grid' ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {visible.map((entry) => (
                <CardDisplay
                  key={entry.id}
                  card={entry.card}
                  image={resolveCardImageUrl(entry.card)}
                  price={entry.total_value > 0 ? formatPrice(entry.total_value) : null}
                  stateIndicatorProps={{ card: { quantity: entry.quantity }, alwaysShowQuantity: true }}
                  variantEffectSource={entry.variants || []}
                  captionAccessory={<OwnerSummary owners={entry.owners} />}
                  onClick={() => setSelectedCard(entry)}
                />
              ))}
            </div>
          ) : (
            <div className="space-y-2">
              {visible.map((entry) => (
                <CardRow
                  key={entry.id}
                  card={entry.card}
                  image={resolveCardImageUrl(entry.card)}
                  name={entry.card.name}
                  setNumber={[entry.card.set_ref?.abbreviation || entry.card.set_id, entry.card.number]
                    .filter(Boolean).join(' ').toUpperCase()}
                  badges={entry.owners.map((o) => ({ label: `${o.username} ×${o.quantity}`, variant: 'purple' }))}
                  value={entry.total_value > 0 ? formatPrice(entry.total_value) : '-'}
                  valueSecondary={`×${entry.quantity}`}
                  variantEffectSource={entry.variants || []}
                  onClick={() => setSelectedCard(entry)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {selectedCard && (
        <CardModal
          card={selectedCard.card}
          readOnly
          onClose={() => setSelectedCard(null)}
        />
      )}
    </div>
  )
}
