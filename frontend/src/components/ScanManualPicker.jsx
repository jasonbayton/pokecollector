import { useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { searchCards } from '../api/client'
import Modal from './ui/Modal'
import TcgdexLanguageSelect from './TcgdexLanguageSelect'
import { manualSearchParams, toManualScanRows } from './scanManualPickerHelpers'
import { resolveCardImageUrl } from '../utils/imageUrl'

export default function ScanManualPicker({ defaultLang = 'en', onSelect, onClose, t }) {
  const [query, setQuery] = useState('')
  const [lang, setLang] = useState(defaultLang)
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const [searched, setSearched] = useState(false)
  const requestToken = useRef(0)

  const search = async event => {
    event.preventDefault()
    if (!query.trim()) return
    const token = (requestToken.current += 1)
    setLoading(true)
    setError(false)
    try {
      const response = await searchCards(manualSearchParams(query, lang))
      // Stale guard: a slower earlier search, or one in a language the user
      // has since changed away from, must not repopulate the list behind a
      // newer one and offer the wrong printings.
      if (token !== requestToken.current) return
      setResults(toManualScanRows(response))
      setSearched(true)
    } catch {
      if (token !== requestToken.current) return
      setError(true)
      setResults([])
      setSearched(true)
    } finally {
      if (token === requestToken.current) setLoading(false)
    }
  }

  return (
    <Modal isOpen onClose={onClose} title={t('scanner.manualPickTitle')} size="lg">
      <div className="space-y-4 p-4">
        <p className="text-sm text-text-muted">{t('scanner.manualPickHint')}</p>
        <form onSubmit={search} className="flex gap-2">
          <label htmlFor="manual-card-search" className="sr-only">{t('scanner.manualPickPlaceholder')}</label>
          <input
            id="manual-card-search"
            name="manual-card-search"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder={t('scanner.manualPickPlaceholder')}
            autoComplete="off"
            className="input min-w-0 flex-1"
          />
          <button type="submit" disabled={loading || !query.trim()} className="btn-primary px-3">
            <Search size={16} /> {t('common.search')}
          </button>
        </form>
        <div>
          <label htmlFor="manual-card-language" className="mb-1.5 block text-xs font-medium text-text-muted">{t('scanner.manualPickLanguage')}</label>
          <TcgdexLanguageSelect
            id="manual-card-language"
            value={lang}
            onChange={next => {
              // Abandon anything in flight: its answer is for the old language.
              requestToken.current += 1
              setLang(next)
              setResults([])
              setSearched(false)
              setLoading(false)
            }}
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
            <button key={card.id} type="button" onClick={() => onSelect(card)}
              className="flex w-full items-center gap-3 rounded-xl border border-border bg-bg-card p-3 text-left hover:border-brand-red/60">
              {/* The picker exists because the scanner could not read the card,
                  so the artwork is how the user confirms this is the right one.
                  A name and a number cannot separate two printings that share
                  both. resolveCardImageUrl routes through the app's own image
                  endpoint, so a configured mirror is honoured here too. */}
              <img
                src={resolveCardImageUrl(card)}
                alt=""
                loading="lazy"
                width={56}
                height={80}
                className="h-20 w-14 shrink-0 rounded-md border border-border bg-bg-muted object-cover"
                onError={event => { event.currentTarget.style.visibility = 'hidden' }}
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-semibold text-text-primary">{card.name}</span>
                <span className="block font-mono text-xs font-bold text-brand-red">
                  {`${(card.set_abbreviation || '').toUpperCase()} ${card.number || ''}`.trim()}
                </span>
                {card.set_ref?.name && (
                  <span className="block truncate text-xs text-text-muted">{card.set_ref.name}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </Modal>
  )
}
