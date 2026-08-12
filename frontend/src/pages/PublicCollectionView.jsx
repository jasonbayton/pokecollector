import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { HelpCircle } from 'lucide-react'
import clsx from 'clsx'
import { getPublicCollection, getPublicProfile } from '../api/publicClient'
import { formatEur } from '../utils/formatEur'
import { useSettings } from '../contexts/SettingsContext'
import CardImageDialog from '../components/CardImageDialog'
import { CardDisplay, CardLegend } from '../components/card-system'

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
  const [badgeLegendOpen, setBadgeLegendOpen] = useState(false)
  const [zoomedCard, setZoomedCard] = useState(null)
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

  const cardsLabel = collection.card_count === 1 ? t('serverCollection.card') : t('serverCollection.cards')

  return (
    <main className="min-h-screen bg-bg-primary px-4 py-8 text-text-primary">
      <div className="mx-auto max-w-6xl">
        <Link to={`/u/${handle}`} className="text-sm text-text-secondary hover:text-text-primary">
          &larr; {profile?.trainer_name || `@${handle}`}
        </Link>

        <div className="mb-4 mt-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold text-text-primary">{t('publicProfiles.viewCollection')}</h1>
            <p className="mt-1 text-sm text-text-secondary">
              {collection.unique_card_count} {t('serverCollection.uniqueCards')} · {collection.card_count} {cardsLabel}
              {collection.total_value != null ? ` · ${formatEur(collection.total_value)}` : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setBadgeLegendOpen(open => !open)}
            className={clsx('btn-ghost px-3 py-2 text-sm', badgeLegendOpen && 'border-brand-red/30 bg-brand-red/10 text-brand-red')}
            aria-expanded={badgeLegendOpen}
            aria-controls="public-collection-badge-legend"
          >
            <HelpCircle size={15} />
            <span>{t('setDetail.badgeLegend')}</span>
          </button>
        </div>

        {badgeLegendOpen && (
          <div id="public-collection-badge-legend" className="card mb-4 p-3">
            <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-text-muted">
              {t('setDetail.badgeLegend')}
            </p>
            <CardLegend collapsible={false} showWishlist={false} />
          </div>
        )}

        {collection.cards.length === 0 ? (
          <div className="rounded-2xl border border-border bg-bg-secondary p-8 text-center text-text-secondary">
            {t('common.noResults')}
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8">
            {collection.cards.map((card) => {
              const displayCard = { ...card, set_id: card.set_name }
              return (
                <CardDisplay
                  key={`${card.id}-${card.variant || 'normal'}`}
                  card={displayCard}
                  image={card.image}
                  price={card.market_value != null ? formatEur(card.market_value) : null}
                  variantEffectSource={card.variant}
                  onClick={() => setZoomedCard(displayCard)}
                  stateIndicatorProps={{
                    // getCardState reads owned_variants; a bare quantity renders nothing.
                    card: { owned_variants: [{ variant: card.variant || 'Normal', quantity: card.quantity }] },
                    showWishlist: false,
                    alwaysShowQuantity: true,
                  }}
                />
              )
            })}
          </div>
        )}
      </div>

      {zoomedCard && (
        <CardImageDialog
          card={zoomedCard}
          image={zoomedCard.image}
          onClose={() => setZoomedCard(null)}
        />
      )}
    </main>
  )
}
