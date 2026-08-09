import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import toast from 'react-hot-toast'

import { getDeletedCollectionItems, restoreDeletedCollectionItem } from '../api/client'
import { useSettings } from '../contexts/SettingsContext'
import { invalidateCardState } from '../utils/queryInvalidation'
import Modal from './ui/Modal'
import { CardRow } from './card-system'
import { resolveCardImageUrl } from '../utils/imageUrl'

/**
 * Undo for cards removed by hand. Only the manual delete path fills this;
 * trades and sales are not accidents and have their own reversal.
 */
export default function RecentlyDeletedModal({ isOpen, onClose }) {
  const { t } = useSettings()
  const queryClient = useQueryClient()

  const { data: entries = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['collection', 'deleted'],
    queryFn: getDeletedCollectionItems,
    // Nothing is fetched until the drawer is actually opened.
    enabled: isOpen,
  })

  const restore = useMutation({
    mutationFn: (id) => restoreDeletedCollectionItem(id),
    onSuccess: (data) => {
      toast.success(data?.outcome === 'merged'
        ? t('collection.deleted.restoredMerged')
        : t('collection.deleted.restored'))
      // A restored card changes every cached card-tile view, not just this
      // list. The project already has a helper for that fan-out.
      invalidateCardState(queryClient)
      queryClient.invalidateQueries({ queryKey: ['collection', 'deleted'] })
    },
    onError: (err) => {
      const blocker = err?.response?.data?.detail
      const known = ['owner_missing', 'card_missing', 'invalid_quantity']
      toast.error(known.includes(blocker)
        ? t(`collection.deleted.blocker.${blocker}`)
        : t('collection.deleted.restoreFailed'))
    },
  })

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('collection.deleted.title')} size="lg">
      <p className="mb-3 text-sm text-text-secondary">{t('collection.deleted.subtitle')}</p>

      {isLoading && <p className="py-6 text-center text-sm text-text-muted">{t('common.loading')}</p>}

      {isError && (
        <div className="py-6 text-center">
          <p className="text-sm text-brand-red">{t('common.error')}</p>
          <button type="button" onClick={() => refetch()} className="btn-ghost mt-2 text-sm">
            {t('common.retry')}
          </button>
        </div>
      )}

      {!isLoading && !isError && entries.length === 0 && (
        <p className="py-6 text-center text-sm text-text-muted">{t('collection.deleted.empty')}</p>
      )}

      <div className="space-y-2">
        {entries.map((entry) => {
          const card = {
            id: entry.card_id,
            card_id: entry.card_id,
            name: entry.card_name,
            number: entry.number,
            set_id: entry.set_id,
            images_small: entry.images_small,
          }
          const badges = [
            entry.variant ? { label: entry.variant, variant: 'purple' } : null,
            entry.condition ? { label: entry.condition, variant: 'gold' } : null,
            entry.owner ? { label: entry.owner, variant: 'blue' } : null,
          ].filter(Boolean)

          return (
            <div key={entry.id} className="flex items-center gap-2">
              <div className="min-w-0 flex-1">
                <CardRow
                  card={card}
                  image={resolveCardImageUrl(card)}
                  name={entry.card_name || entry.card_id}
                  setNumber={[entry.set_id, entry.number].filter(Boolean).join(' ').toUpperCase()}
                  badges={badges}
                  value={`×${entry.quantity}`}
                  valueSecondary={`${t('collection.deleted.by')} ${entry.deleted_by || '?'}`}
                  variantEffectSource={entry.variant}
                />
              </div>
              <button
                type="button"
                disabled={!entry.restorable || restore.isPending}
                onClick={() => restore.mutate(entry.id)}
                title={entry.restorable
                  ? t('collection.deleted.restore')
                  : t(`collection.deleted.blocker.${entry.restore_blocker}`)}
                className="btn-ghost shrink-0 px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-40"
              >
                <RotateCcw size={14} />
                <span className="hidden sm:inline">{t('collection.deleted.restore')}</span>
              </button>
            </div>
          )
        })}
      </div>
    </Modal>
  )
}
