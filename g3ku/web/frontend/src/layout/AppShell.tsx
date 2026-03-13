import { Outlet } from 'react-router-dom'

import { Sidebar } from '@/layout/Sidebar'
import { useLayoutStore } from '@/stores/layoutStore'

export function AppShell() {
  const toast = useLayoutStore((state) => state.toast)
  const clearToast = useLayoutStore((state) => state.clearToast)

  return (
    <div className="app-shell">
      <Sidebar />
      <main className="app-main">
        {toast ? (
          <button className={`toast toast-${toast.tone}`} onClick={clearToast} type="button">
            {toast.message}
          </button>
        ) : null}
        <Outlet />
      </main>
    </div>
  )
}
