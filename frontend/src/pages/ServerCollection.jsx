import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Users, Search, LayoutGrid, List } from 'lucide-react'

import { getServerCollection } from '../api/client'
import { useSettings } from '../contexts/SettingsContext'
import { resolveCardImageUrl } from '../utils/imageUrl'
import CardItem from '../components/CardItem'

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
    return <div className="p-4 text-sm text-text-muted">{t('common.loading')}</div>
  }
  if (isError) {
    return <div className="p-4 text-sm text-brand-red">{t('common.error')}</div>
  }

  return (
    <div className="space-y-4 p-4">
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
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setSelectedCard(entry.card)}
                  className="card overflow-hidden p-2 text-left"
                >
                  <div className="relative">
                    <img
                      src={resolveCardImageUrl(entry.card)}
                      alt={entry.card.name}
                      loading="lazy"
                      className="w-full rounded-lg"
                    />
                    <span className="absolute left-1 top-1 rounded-full bg-bg-primary/90 px-2 py-0.5 text-[11px] font-bold">
                      ×{entry.quantity}
                    </span>
                  </div>
                  <p className="mt-2 truncate text-sm font-semibold text-text-primary">{entry.card.name}</p>
                  <p className="truncate text-[11px] text-text-muted">
                    {entry.card.set_abbreviation || entry.card.set_id} {entry.card.number}
                  </p>
                  <p className="mt-1 truncate text-[11px] text-blue">
                    {entry.owners.map((o) => `${o.username} ×${o.quantity}`).join(', ')}
                  </p>
                  <p className="text-[11px] font-semibold text-green">{formatPrice(entry.total_value)}</p>
                </button>
              ))}
            </div>
          ) : (
            <div className="card divide-y divide-border">
              {visible.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setSelectedCard(entry.card)}
                  className="flex w-full items-center gap-3 p-3 text-left"
                >
                  <img
                    src={resolveCardImageUrl(entry.card)}
                    alt={entry.card.name}
                    loading="lazy"
                    className="h-14 w-10 shrink-0 rounded object-cover"
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-text-primary">{entry.card.name}</p>
                    <p className="truncate text-[11px] text-text-muted">
                      {entry.card.set_name || entry.card.set_id} #{entry.card.number}
                    </p>
                  </div>
                  <div className="hidden min-w-0 flex-1 sm:block">
                    <p className="truncate text-[11px] text-blue">
                      {entry.owners.map((o) => `${o.username} ×${o.quantity}`).join(', ')}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="text-sm font-semibold">×{entry.quantity}</p>
                    <p className="text-[11px] text-green">{formatPrice(entry.total_value)}</p>
                  </div>
                </button>
              ))}
            </div>
          )}
        </>
      )}

      {selectedCard && (
        <CardItem card={selectedCard} isModal onClose={() => setSelectedCard(null)} />
      )}
    </div>
  )
}
