import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { getPublicCollection, getPublicProfile } from '../api/publicClient'
import { formatEur } from '../utils/formatEur'
import { useSettings } from '../contexts/SettingsContext'
import { CardDisplay } from '../components/card-system'

/**
 * A trainer's whole collection, for anyone with the link. Read-only by
 * construction: this module imports no mutating call, and the payload it
 * renders carries no purchase price, condition or grade.
 */
export default function PublicCollectionView() {
  const { handle } = useParams()
  const [collection, setCollection] = useState(null)
  const [profile, setProfile] = useState(null)
  const [error, setError] = useState(null)
  const { t } = useSettings()

  useEffect(() => {
    let cancelled = false
    setCollection(null)
    setError(null)
    Promise.all([getPublicCollection(handle), getPublicProfile(handle).catch(() => null)])
      .then(([data, prof]) => {
        if (cancelled) return
        setCollection(data)
        setProfile(prof)
      })
      .catch(() => { if (!cancelled) setError(true) })
    return () => { cancelled = true }
  }, [handle])

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center text-text-secondary">
        {t('publicProfiles.collectionUnavailable')}
      </div>
    )
  }
  if (!collection) {
    return <div className="min-h-screen flex items-center justify-center text-text-secondary">{t('common.loading')}</div>
  }

  return (
    <main className="min-h-screen bg-bg-primary px-4 py-8 text-text-primary">
      <div className="mx-auto max-w-6xl">
        <Link to={`/u/${handle}`} className="text-sm text-text-secondary hover:text-text-primary">
          &larr; {profile?.trainer_name || `@${handle}`}
        </Link>

        <div className="mb-6 mt-3">
          <h1 className="text-xl font-bold text-text-primary">{t('publicProfiles.viewCollection')}</h1>
          <p className="mt-1 text-sm text-text-secondary">
            {collection.unique_card_count} {t('serverCollection.uniqueCards')} · {collection.card_count} {t('serverCollection.cards')}
            {collection.total_value != null ? ` · ${formatEur(collection.total_value)}` : ''}
          </p>
        </div>

        {collection.cards.length === 0 ? (
          <div className="rounded-2xl border border-border bg-bg-secondary p-8 text-center text-text-secondary">
            {t('common.noResults')}
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8">
            {collection.cards.map((card) => (
              <CardDisplay
                key={`${card.id}-${card.variant || 'normal'}`}
                card={card}
                image={card.image}
                price={card.market_value != null ? formatEur(card.market_value) : null}
                variantEffectSource={card.variant}
                stateIndicatorProps={{ card: { quantity: card.quantity }, alwaysShowQuantity: true }}
              />
            ))}
          </div>
        )}
      </div>
    </main>
  )
}
