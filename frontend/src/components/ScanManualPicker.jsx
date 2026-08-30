import { useState } from 'react'
import { Search } from 'lucide-react'
import { searchCards } from '../api/client'
import Modal from './ui/Modal'
import TcgdexLanguageSelect from './TcgdexLanguageSelect'
import { manualSearchParams, toManualScanMatch } from './scanManualPickerHelpers'

export default function ScanManualPicker({ defaultLang = 'en', onSelect, onClose, t }) {
  const [query, setQuery] = useState('')
  const [lang, setLang] = useState(defaultLang)
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const [searched, setSearched] = useState(false)

  const search = async event => {
    event.preventDefault()
    if (!query.trim()) return
    setLoading(true)
    setError(false)
    try {
      const response = await searchCards(manualSearchParams(query, lang))
      setResults(response.data?.data || [])
      setSearched(true)
    } catch {
      setError(true)
      setResults([])
      setSearched(true)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal isOpen onClose={onClose} title={t('scanner.manualPickTitle')} size="lg">
      <div className="space-y-4 p-4">
        <p className="text-sm text-text-muted">{t('scanner.manualPickHint')}</p>
        <form onSubmit={search} className="flex gap-2">
          <input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder={t('scanner.manualPickPlaceholder')}
            className="input min-w-0 flex-1"
          />
          <button type="submit" disabled={loading || !query.trim()} className="btn-primary px-3">
            <Search size={16} /> {t('common.search')}
          </button>
        </form>
        <div>
          <label className="mb-1.5 block text-xs font-medium text-text-muted">{t('scanner.manualPickLanguage')}</label>
          <TcgdexLanguageSelect
            value={lang}
            onChange={next => { setLang(next); setResults([]); setSearched(false) }}
            className="select w-full"
          />
        </div>
        {error && <p role="alert" className="text-sm text-brand-red">{t('scanner.manualPickSearchFailed')}</p>}
        {!loading && !error && searched && results.length === 0 && (
          <p className="text-sm text-text-muted">{t('common.noResults')}</p>
        )}
        {loading && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        <div className="max-h-72 space-y-2 overflow-y-auto">
          {results.map(card => (
            <button key={card.id} type="button" onClick={() => onSelect(toManualScanMatch(card))}
              className="w-full rounded-xl border border-border bg-bg-card p-3 text-left hover:border-brand-red/60">
              <p className="font-semibold text-text-primary">{card.name}</p>
              <p className="text-xs text-text-muted">{`${(card.set_abbreviation || '').toUpperCase()} ${card.number || ''}`.trim()}</p>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  )
}
