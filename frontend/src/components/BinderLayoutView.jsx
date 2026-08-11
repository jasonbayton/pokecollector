import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { clearBinderSlot, getBinderPage, moveBinderSlot, placeBinderSlot } from '../api/client'
import { useSettings } from '../contexts/SettingsContext'
import { resolveCardImageUrl } from '../utils/imageUrl'
import { clampPage, pageCells } from '../utils/binderSlots'

// One physical page of a mapped binder. Only the page being looked at is
// fetched, so a binder with hundreds of pages costs the same as one with two.
export default function BinderLayoutView({ binderId, binder, cards }) {
  const { t } = useSettings()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  // Tapping an occupied pocket selects it; tapping a second pocket moves or
  // swaps. Selecting an empty pocket while an unplaced card is chosen places it.
  const [selectedPocket, setSelectedPocket] = useState(null)
  const [pendingEntryId, setPendingEntryId] = useState(null)

  const { data: pageData, isLoading } = useQuery({
    queryKey: ['binder-layout', binderId, page],
    queryFn: () => getBinderPage(binderId, page),
    enabled: Boolean(binder?.grid_rows && binder?.grid_columns),
  })

  const cardsByEntry = useMemo(() => {
    const map = new Map()
    for (const card of cards || []) map.set(card.binder_card_id, card)
    return map
  }, [cards])

  const placedCounts = useMemo(() => {
    const counts = new Map()
    for (const cell of pageCells(pageData)) {
      if (cell.binderCardId != null) {
        counts.set(cell.binderCardId, (counts.get(cell.binderCardId) || 0) + 1)
      }
    }
    return counts
  }, [pageData])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['binder-layout', binderId] })
  }

  const onError = (error) => {
    toast.error(error?.response?.data?.detail || t('common.error'))
  }

  const placeMutation = useMutation({
    mutationFn: (data) => placeBinderSlot(binderId, data),
    onSuccess: () => { refresh(); setPendingEntryId(null) },
    onError,
  })

  const moveMutation = useMutation({
    mutationFn: (data) => moveBinderSlot(binderId, data),
    onSuccess: () => { refresh(); setSelectedPocket(null) },
    onError,
  })

  const clearMutation = useMutation({
    mutationFn: ({ page: p, pocket }) => clearBinderSlot(binderId, p, pocket),
    onSuccess: () => { refresh(); setSelectedPocket(null) },
    onError,
  })

  if (!binder?.grid_rows || !binder?.grid_columns) {
    return (
      <div className="rounded-2xl border border-border bg-bg-card p-6 text-center">
        <p className="text-sm text-text-muted">{t('binders.layout.notMapped')}</p>
      </div>
    )
  }

  const cells = pageCells(pageData)
  const pageCount = pageData?.page_count || 1

  const handleCellClick = (cell) => {
    if (cell.isEmpty) {
      if (pendingEntryId != null) {
        placeMutation.mutate({ binder_card_id: pendingEntryId, page, pocket: cell.pocket })
      } else if (selectedPocket != null) {
        moveMutation.mutate({
          from_page: page, from_pocket: selectedPocket, to_page: page, to_pocket: cell.pocket,
        })
      }
      return
    }
    if (selectedPocket === cell.pocket) {
      setSelectedPocket(null)
      return
    }
    if (selectedPocket != null) {
      moveMutation.mutate({
        from_page: page, from_pocket: selectedPocket, to_page: page, to_pocket: cell.pocket,
      })
      return
    }
    setSelectedPocket(cell.pocket)
  }

  // Cards with copies the layout does not yet account for, so they can be
  // dropped into a pocket.
  const unplaced = (cards || []).filter(card => {
    const placedHere = placedCounts.get(card.binder_card_id) || 0
    return (card.required_quantity || 1) > placedHere
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          className="btn-ghost px-2"
          onClick={() => setPage(current => clampPage(current - 1, pageCount))}
          disabled={page <= 1}
          aria-label={t('binders.layout.previousPage')}
        >
          <ChevronLeft size={16} />
        </button>
        <p className="text-sm text-text-secondary">
          {t('binders.layout.pageOf').replace('{page}', page).replace('{total}', pageCount)}
        </p>
        <button
          type="button"
          className="btn-ghost px-2"
          onClick={() => setPage(current => current + 1)}
          aria-label={t('binders.layout.nextPage')}
        >
          <ChevronRight size={16} />
        </button>
      </div>

      {isLoading ? (
        <p className="text-center text-sm text-text-muted">{t('common.loading')}</p>
      ) : (
        <div
          className="grid gap-2"
          style={{ gridTemplateColumns: `repeat(${binder.grid_columns}, minmax(0, 1fr))` }}
        >
          {cells.map(cell => {
            const card = cell.binderCardId != null ? cardsByEntry.get(cell.binderCardId) : null
            const isSelected = selectedPocket === cell.pocket
            return (
              <button
                key={cell.pocket}
                type="button"
                onClick={() => handleCellClick(cell)}
                title={card ? card.name : t('binders.layout.emptyPocket')}
                className={`relative aspect-[5/7] rounded-lg border transition-all ${
                  isSelected
                    ? 'border-yellow ring-2 ring-yellow/40'
                    : cell.isEmpty
                      ? 'border-dashed border-border bg-bg-primary hover:border-yellow/40'
                      : 'border-border bg-bg-elevated hover:border-yellow/40'
                }`}
              >
                {card ? (
                  <img
                    src={resolveCardImageUrl(card)}
                    alt={card.name}
                    className="h-full w-full rounded-lg object-cover"
                    loading="lazy"
                  />
                ) : (
                  <span className="text-[10px] text-text-muted">{cell.pocket}</span>
                )}
                {!cell.isEmpty && isSelected && (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(event) => {
                      event.stopPropagation()
                      clearMutation.mutate({ page, pocket: cell.pocket })
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.stopPropagation()
                        clearMutation.mutate({ page, pocket: cell.pocket })
                      }
                    }}
                    className="absolute right-1 top-1 rounded-full bg-bg-primary/90 p-1 text-text-muted hover:text-brand-red"
                    aria-label={t('binders.layout.clearPocket')}
                  >
                    <X size={12} />
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}

      {unplaced.length > 0 && (
        <div className="rounded-2xl border border-border bg-bg-card p-3">
          <p className="mb-2 text-xs uppercase tracking-[0.2em] text-text-muted">
            {t('binders.layout.unplaced')}
          </p>
          <div className="flex flex-wrap gap-2">
            {unplaced.map(card => (
              <button
                key={card.binder_card_id}
                type="button"
                onClick={() => setPendingEntryId(
                  pendingEntryId === card.binder_card_id ? null : card.binder_card_id,
                )}
                className={`rounded-lg border px-2 py-1 text-xs transition-all ${
                  pendingEntryId === card.binder_card_id
                    ? 'border-yellow text-text-primary'
                    : 'border-border text-text-secondary hover:border-yellow/40'
                }`}
              >
                {card.name}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-text-muted">{t('binders.layout.placeHint')}</p>
        </div>
      )}
    </div>
  )
}
