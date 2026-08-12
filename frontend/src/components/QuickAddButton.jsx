import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Camera, PenLine, Plus, ScanLine, Search, X } from 'lucide-react'

import { useSettings } from '../contexts/SettingsContext'
import {
  QUICK_ADD_CUSTOM,
  QUICK_ADD_QUEUE,
  QUICK_ADD_SCAN,
  QUICK_ADD_SEARCH,
  quickAddHiddenOn,
  useScanner,
} from '../contexts/ScannerContext'

// The pokeball home button sits bottom left at the same size and weight. These
// two are the only floating controls, so they are deliberately a mirrored pair:
// same diameter, same shadow, opposite corners.
const FLOATING_SHADOW = '0 4px 20px rgba(0,0,0,0.5), 0 0 0 2px rgba(0,0,0,0.8), 0 0 16px rgba(227,0,11,0.3)'

// Presentational half, exported so its two states can be rendered directly. The
// container below owns the open/closed state and the context wiring.
export function QuickAddMenu({
  open,
  onToggle,
  onClose,
  onSelect,
  attention = 0,
  active = false,
  toggleRef = null,
  menuRef = null,
}) {
  const { t } = useSettings()

  const items = [
    { action: QUICK_ADD_SCAN, label: t('scanner.title'), icon: <Camera size={16} /> },
    { action: QUICK_ADD_SEARCH, label: t('nav.cardSearch'), icon: <Search size={16} /> },
    { action: QUICK_ADD_CUSTOM, label: t('cardSearch.createCustomCard'), icon: <PenLine size={16} /> },
    { action: QUICK_ADD_QUEUE, label: t('scanner.queueTitle'), icon: <ScanLine size={16} /> },
  ]

  return (
    <>
      {/* Only drawn while the menu is open. A permanent full-screen layer would
          swallow clicks meant for the page and for the home button. Closing
          rather than toggling: the click that reaches this layer has already
          moved focus off the menu, and a toggle would reopen what the blur
          just closed. */}
      {open && (
        <button
          type="button"
          aria-label={t('common.close')}
          onClick={onClose}
          className="fixed inset-0 z-30 cursor-default bg-black/20"
        />
      )}
      {/* Below the dialog layer (z-50) on purpose: a floating button drawn over
          an open sheet reads as a rendering fault. */}
      <div
        className="fixed z-40 flex flex-col items-end gap-2"
        style={{
          bottom: 'max(1.5rem, env(safe-area-inset-bottom))',
          right: 'max(1rem, env(safe-area-inset-right))',
        }}
        onBlur={event => {
          // A menu is not a dialog: tabbing out of it dismisses it rather than
          // trapping the user inside four items. Focus has already moved where
          // the user asked, so it is not pulled back to the button.
          if (open && !event.currentTarget.contains(event.relatedTarget)) onClose()
        }}
      >
        {open && (
          <div
            ref={menuRef}
            role="menu"
            aria-label={t('quickAdd.title')}
            className="quick-add-menu w-64 overflow-hidden rounded-2xl border border-border bg-bg-surface/95 shadow-2xl backdrop-blur"
          >
            {items.map(item => (
              <button
                key={item.action}
                type="button"
                role="menuitem"
                onClick={() => onSelect(item.action)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-text-primary transition-colors hover:bg-white/5"
              >
                <span className="text-text-muted">{item.icon}</span>
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                {item.action === QUICK_ADD_QUEUE && attention > 0 && (
                  <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-yellow px-1 text-[9px] font-bold leading-none text-black">
                    {attention > 99 ? '99+' : attention}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}

        <button
          ref={toggleRef}
          type="button"
          onClick={onToggle}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t('quickAdd.title')}
          className="quick-add-button relative flex h-12 w-12 items-center justify-center rounded-full text-white transition-transform duration-200 active:scale-90"
          style={{
            background: 'linear-gradient(180deg, #ff2d38 0%, #c2000a 100%)',
            boxShadow: FLOATING_SHADOW,
            border: '2px solid #111',
          }}
        >
          {open ? <X size={20} /> : <Plus size={22} />}
          {attention > 0 && (
            <span
              // scanner.needReview is a sentence fragment the queue completes
              // with a count. On its own it read as "need review", so it is
              // given the same count here.
              title={`${attention} ${t('scanner.needReview')}`}
              className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-yellow px-1 text-[10px] font-bold leading-none text-black"
            >
              {attention > 99 ? '99+' : attention}
            </span>
          )}
          {/* White rather than the red the search header used: the same red on
              a red button is invisible, and this dot has to read as "work in
              progress" next to a yellow count badge. */}
          {attention === 0 && active && (
            <span
              title={t('scanner.processing')}
              className="absolute -right-0.5 -top-0.5 h-3 w-3 animate-pulse rounded-full bg-white ring-2 ring-black/50"
            />
          )}
        </button>
      </div>
    </>
  )
}

export default function QuickAddButton() {
  const { runQuickAdd, scanAttention, scansActive } = useScanner()
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const toggleRef = useRef(null)
  const menuRef = useRef(null)

  const close = useCallback(({ restoreFocus = false } = {}) => {
    setOpen(false)
    if (restoreFocus) toggleRef.current?.focus()
  }, [])

  // A quick-add action can change route. Leaving the menu open over the page
  // the user just landed on would hide the thing they asked for.
  useEffect(() => { setOpen(false) }, [location.pathname])

  // On the document rather than on the control: the menu is dismissible from
  // wherever the user is looking, and by the time they reach for Escape their
  // focus may well have left the four items.
  useEffect(() => {
    if (!open) return undefined
    const handleKeyDown = event => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      close({ restoreFocus: true })
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [close, open])

  // Opening moves focus into the menu, so a keyboard user does not have to tab
  // through the page to reach what they just opened, and closing puts it back
  // on the button they opened it with.
  useEffect(() => {
    if (!open) return
    menuRef.current?.querySelector('[role="menuitem"]')?.focus()
  }, [open])

  // Hooks first: the control is absent on the scan-queue routes, not disabled.
  if (quickAddHiddenOn(location.pathname)) return null

  return (
    <QuickAddMenu
      open={open}
      onToggle={() => setOpen(current => !current)}
      onClose={close}
      onSelect={action => {
        close({ restoreFocus: true })
        runQuickAdd(action)
      }}
      attention={scanAttention}
      active={scansActive}
      toggleRef={toggleRef}
      menuRef={menuRef}
    />
  )
}
