import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Minus, Plus, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { rapidSetEntry } from '../api/client'
import { cardNumberMatches } from '../utils/cardNumbers'
import { CARD_VARIANTS } from '../utils/cardVariants'
import { COLLECTION_CONDITIONS } from '../utils/collectionOptions'
import { invalidateCardState } from '../utils/queryInvalidation'
import { addCopy, applyRowChange, cachedLanguagesForCard, cachedLanguagesInSet, variantChoices } from '../utils/rapidEntryRows'
import TcgdexLanguageSelect from './TcgdexLanguageSelect'
import QuantityInput from './ui/QuantityInput'

export default function RapidSetEntry({ set, cards, queryClient, t, onClose }) {
  const [defaults, setDefaults] = useState({ condition: 'Mint', variant: '', lang: set.lang || 'en' })
  const [number, setNumber] = useState('')
  const [rows, setRows] = useState([])
  const [summary, setSummary] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef(null)
  const nextRowId = useRef(0)
  const cardsByNumber = useMemo(() => cards.filter(card => card.number), [cards])
  const match = useMemo(
    () => cardsByNumber.find(card => cardNumberMatches(card.number, number)),
    [cardsByNumber, number],
  )
  const cachedLanguages = useMemo(
    () => (match ? cachedLanguagesForCard(match, cards) : cachedLanguagesInSet(cards)),
    [cards, match],
  )

  useEffect(() => {
    if (!cachedLanguages.length || cachedLanguages.some(language => language.code === defaults.lang)) return
    setDefaults(value => ({ ...value, lang: cachedLanguages[0].code }))
  }, [cachedLanguages, defaults.lang])

  const addNumber = () => {
    if (!match) return
    // Rows carry a stable id of their own. Editing a row's condition then
    // changes only that row, where keying on the card would have merged it
    // into whichever other row now shared its identity.
    // The effect above keeps this true for normal interaction. Keep the same
    // guard here so a rapid Enter key cannot add a row with an invalid language
    // between a new number match and React applying the state adjustment.
    const lang = cachedLanguages.some(language => language.code === defaults.lang)
      ? defaults.lang
      : cachedLanguages[0]?.code || match.lang
    setRows(current => addCopy(current, match, { ...defaults, lang }, (nextRowId.current += 1)))
    setNumber('')
    inputRef.current?.focus()
  }

  const updateRow = (rowId, change) => {
    setRows(current => applyRowChange(current, rowId, change))
  }

  const changeQuantity = (rowId, delta) => {
    setRows(current => current.flatMap(row => {
      if (row.id !== rowId) return [row]
      const quantity = row.quantity + delta
      return quantity > 0 ? [{ ...row, quantity }] : []
    }))
  }

  const commit = async () => {
    if (!rows.length || submitting) return
    setSubmitting(true)
    try {
      const result = await rapidSetEntry({
        set_id: set.id,
        items: rows.map(({ card, quantity, condition, variant, lang }) => ({
          card_id: card.id, quantity, condition, variant, lang,
        })),
      })
      invalidateCardState(queryClient, { setId: set.id })
      setSummary(result)
      setRows([])
      toast.success(t('rapidEntry.committed'))
    } catch (error) {
      const detail = error?.response?.data?.detail
      toast.error(detail?.message || detail || t('rapidEntry.commitFailed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="card space-y-4 border-brand-red/40">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-text-primary">{t('rapidEntry.title')}</h2>
          <p className="text-sm text-text-secondary">{t('rapidEntry.subtitle')}</p>
        </div>
        <button type="button" onClick={onClose} className="btn-ghost px-2" aria-label={t('common.close')}><X size={18} /></button>
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <label className="text-xs text-text-muted">{t('card.condition')}
          <select value={defaults.condition} onChange={event => setDefaults(value => ({ ...value, condition: event.target.value }))} className="select mt-1 w-full">
            {COLLECTION_CONDITIONS.map(condition => <option key={condition}>{condition}</option>)}
          </select>
        </label>
        <label className="text-xs text-text-muted">{t('card.variant')}
          <select value={defaults.variant} onChange={event => setDefaults(value => ({ ...value, variant: event.target.value }))} className="select mt-1 w-full">
            <option value="">{t('rapidEntry.variantAutomatic')}</option>
            {CARD_VARIANTS.map(variant => <option key={variant}>{variant}</option>)}
          </select>
        </label>
        <label className="text-xs text-text-muted">{t('rapidEntry.language')}
          <TcgdexLanguageSelect value={defaults.lang} onChange={lang => setDefaults(value => ({ ...value, lang }))} languages={cachedLanguages} className="select mt-1 w-full" />
        </label>
      </div>

      <div>
        <label htmlFor="rapid-entry-number" className="text-xs text-text-muted">{t('rapidEntry.collectorNumber')}</label>
        <div className="mt-1 flex gap-2">
          <input
            ref={inputRef}
            id="rapid-entry-number"
            value={number}
            onChange={event => setNumber(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addNumber() } }}
            className="input flex-1 font-mono"
            autoComplete="off"
            inputMode="text"
            placeholder={t('rapidEntry.collectorNumberPlaceholder')}
          />
          <button type="button" onClick={addNumber} disabled={!match} className="btn-primary disabled:opacity-50"><Plus size={16} /> {t('rapidEntry.add')}</button>
        </div>
        {number && !match && <p className="mt-1 text-xs text-brand-red">{t('rapidEntry.numberNotFound')}</p>}
        {match && <p className="mt-1 text-xs text-green"><Check size={12} className="inline" /> {match.number} - {match.name}</p>}
      </div>

      {rows.length > 0 && <div className="divide-y divide-border rounded-xl border border-border">
        {rows.map(row => (
          <div key={row.id} className="p-3">
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1"><span className="font-mono text-sm text-brand-red">{row.card.number}</span> <span className="text-sm text-text-primary">{row.card.name}</span></div>
              <button type="button" onClick={() => changeQuantity(row.id, -1)} className="btn-ghost px-2" aria-label={t('rapidEntry.removeOne')}><Minus size={15} /></button>
              <span className="w-6 text-center font-bold tabular-nums">{row.quantity}</span>
              <button type="button" onClick={() => changeQuantity(row.id, 1)} className="btn-ghost px-2" aria-label={t('rapidEntry.addOne')}><Plus size={15} /></button>
              <button type="button" onClick={() => updateRow(row.id, { expanded: !row.expanded })} className="btn-ghost px-2" aria-label={t('rapidEntry.rowOptions')}><ChevronDown size={15} /></button>
            </div>
            {row.expanded && <div className="mt-3 grid gap-2 sm:grid-cols-4">
              <label className="text-xs text-text-muted">{t('common.quantity')}<QuantityInput value={row.quantity} onChange={quantity => updateRow(row.id, { quantity })} className="input mt-1 w-full" /></label>
              <label className="text-xs text-text-muted">{t('card.condition')}<select value={row.condition} onChange={event => updateRow(row.id, { condition: event.target.value })} className="select mt-1 w-full">{COLLECTION_CONDITIONS.map(condition => <option key={condition}>{condition}</option>)}</select></label>
              <label className="text-xs text-text-muted">{t('card.variant')}<select value={row.variant} onChange={event => updateRow(row.id, { variant: event.target.value })} className="select mt-1 w-full">{variantChoices(row.card, CARD_VARIANTS).map(variant => <option key={variant}>{variant}</option>)}</select></label>
              <label className="text-xs text-text-muted">{t('rapidEntry.language')}<TcgdexLanguageSelect value={row.lang} onChange={lang => updateRow(row.id, { lang })} languages={cachedLanguagesForCard(row.card, cards)} className="select mt-1 w-full" /></label>
            </div>}
          </div>
        ))}
      </div>}

      {summary && <p className="rounded-lg bg-green/10 p-3 text-sm text-green">{t('rapidEntry.summary').replace('{quantity}', summary.quantity).replace('{added}', summary.added).replace('{updated}', summary.updated)}</p>}
      <button type="button" onClick={commit} disabled={!rows.length || submitting} className="btn-primary w-full disabled:opacity-50">{submitting ? t('rapidEntry.committing') : t('rapidEntry.commit').replace('{quantity}', rows.reduce((total, row) => total + row.quantity, 0))}</button>
    </div>
  )
}
