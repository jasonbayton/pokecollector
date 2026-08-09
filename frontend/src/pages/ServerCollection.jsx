import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Users, Search, LayoutGrid, List } from 'lucide-react'

import { getServerCollection } from '../api/client'
import { CardModal } from '../components/CardItem'
import { CardDisplay, CardRow } from '../components/card-system'
import { useSettings } from '../contexts/SettingsContext'
import { resolveCardImageUrl } from '../utils/imageUrl'

/** Who holds this card, shown under the artwork. The whole point of the view. */
function OwnerSummary({ owners }) {
  return (
    <span className="block truncate text-[11px] text-blue">
      {owners.map((o) => `${o.username} ×${o.quantity}`).join(', ')}
    </span>
  )
}

/**
 * Every contributed collection, merged. Deliberately read-only: this answers
 * "does anyone already have this card" and nothing here should invite an edit,
 * because the cards belong to other people.
 */
export default function ServerCollection() {
  const { t, formatPrice } = useSettings()
  const [search, setSearch] = useState('')
  const [view, setView] = useState('grid')
  const [selectedCard, setSelectedCard] = useState(null)
  const [ownerFilter, setOwnerFilter] = useState('')

  const { data, isLoading, isError } = useQuery({
    queryKey: ['server-collection'],
    queryFn: () => getServerCollection(),
  })

  const entries = data?.data || []
  const contributors = data?.contributors || []

  const visible = useMemo(() => {
    const term = search.trim().toLowerCase()
    return entries.filter((entry) => {
      if (ownerFilter && !entry.owners.some((o) => o.username === ownerFilter)) return false
      if (!term) return true
      const card = entry.card || {}
      return [card.name, card.set_name, card.set_abbreviation, card.number]
        .some((field) => String(field || '').toLowerCase().includes(term))
    })
  }, [entries, search, ownerFilter])

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

            <div className="flex items-center gap-2">
              <div className="relative flex-1">
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
                onClick={() => setView(view === 'grid' ? 'list' : 'grid')}
                className="btn-ghost px-3"
                title={t('serverCollection.toggleView')}
              >
                {view === 'grid' ? <List size={16} /> : <LayoutGrid size={16} />}
              </button>
            </div>
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
                  badges={entry.owners.map((o) => ({ label: `${o.username} \u00d7${o.quantity}`, variant: 'purple' }))}
                  value={entry.total_value > 0 ? formatPrice(entry.total_value) : '-'}
                  valueSecondary={`\u00d7${entry.quantity}`}
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
