import { Bot, Boxes, Hammer, Network, PanelLeftClose, PanelLeftOpen, Wrench } from 'lucide-react'
import { NavLink } from 'react-router-dom'

import { useLayoutStore } from '@/stores/layoutStore'

const NAV_ITEMS = [
  { to: '/', label: 'CEO 会话', icon: Bot },
  { to: '/tasks', label: '任务大厅', icon: Network },
  { to: '/skills', label: 'Skill 管理', icon: Boxes },
  { to: '/tools', label: 'Tool 管理', icon: Wrench },
  { to: '/models', label: '模型管理', icon: Hammer },
]

export function Sidebar() {
  const collapsed = useLayoutStore((state) => state.sidebarCollapsed)
  const toggleSidebar = useLayoutStore((state) => state.toggleSidebar)

  return (
    <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-top">
        <div>
          <p className="eyebrow">G3KU</p>
          {!collapsed ? <h1>Runtime Console</h1> : null}
        </div>
        <button className="icon-button" type="button" onClick={toggleSidebar} aria-label="切换侧边栏">
          {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
      </div>
      <nav className="sidebar-nav" aria-label="主导航">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          return (
            <NavLink key={item.to} to={item.to} end={item.to === '/'} className={({ isActive }) => `sidebar-link ${isActive ? 'active' : ''}`}>
              <Icon size={18} />
              {!collapsed ? <span>{item.label}</span> : null}
            </NavLink>
          )
        })}
      </nav>
    </aside>
  )
}
