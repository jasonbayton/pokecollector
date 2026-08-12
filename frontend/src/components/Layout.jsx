import { Outlet, useLocation } from 'react-router-dom'
import AppNav from './AppNav'
import QuickAddButton from './QuickAddButton'
import { useReleaseManualHistoryScrollRestoration } from '../hooks/useListScrollRestoration'

export default function Layout() {
  const location = useLocation()
  const isHome = location.pathname === '/'
  useReleaseManualHistoryScrollRestoration()

  return (
    <div className="min-h-dvh flex flex-col bg-bg overflow-x-hidden">
      {!isHome && <AppNav />}
      <main className={`flex-1 ${!isHome ? 'w-full px-6 pb-8 sm:px-8 lg:px-10 xl:px-12' : ''}`}>
        <Outlet />
      </main>
      {/* Rendered here rather than from AppNav: AppNav is dropped on the home
          route, and quick add has to be reachable from every page. */}
      <QuickAddButton />
    </div>
  )
}
