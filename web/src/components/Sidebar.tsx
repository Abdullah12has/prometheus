import { useState } from 'react'
import {
  LayoutDashboard,
  Building2,
  Send,
  Compass,
  GitCompareArrows,
  Mic,
  Settings,
  LogOut,
  Sparkles,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react'
import { Link, useSegments } from '../lib/router'
import { useAuth } from '../lib/auth'

const NAV_ITEMS = [
  { to: '/overview', label: 'Overview', icon: LayoutDashboard, segment: 'overview' },
  { to: '/companies', label: 'Companies', icon: Building2, segment: 'companies' },
  { to: '/outreach', label: 'Outreach', icon: Send, segment: 'outreach' },
  { to: '/futures', label: 'Futures', icon: Compass, segment: 'futures' },
  { to: '/matches', label: 'Matches', icon: GitCompareArrows, segment: 'matches' },
  { to: '/voice-notes', label: 'Voice & notes', icon: Mic, segment: 'voice-notes' },
  { to: '/settings', label: 'Settings', icon: Settings, segment: 'settings' },
]

const FOLD_KEY = 'sidebar-folded'

export function Sidebar({ assistantOpen, onToggleAssistant }: { assistantOpen: boolean; onToggleAssistant: () => void }) {
  const segments = useSegments()
  const activeSegment = segments[0] ?? 'overview'
  const { logout } = useAuth()
  const [folded, setFolded] = useState(() => window.localStorage.getItem(FOLD_KEY) === '1')

  function toggleFold() {
    setFolded((value) => {
      window.localStorage.setItem(FOLD_KEY, value ? '0' : '1')
      return !value
    })
  }

  return (
    <nav className={folded ? 'sidebar sidebar--folded' : 'sidebar'} aria-label="Primary">
      <div className="sidebar__brand">
        <span className="sidebar__mark" aria-hidden="true" />
        <span className="sidebar__name">Permetheus</span>
        <button
          type="button"
          className="icon-button sidebar__fold"
          onClick={toggleFold}
          aria-label={folded ? 'Expand sidebar' : 'Collapse sidebar'}
          title={folded ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {folded ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftClose size={16} aria-hidden="true" />}
        </button>
      </div>

      <ul className="sidebar__list">
        {NAV_ITEMS.map(({ to, label, icon: Icon, segment }) => {
          const isActive = activeSegment === segment
          return (
            <li key={to} title={folded ? label : undefined}>
              <Link
                to={to}
                className={isActive ? 'sidebar__link sidebar__link--active' : 'sidebar__link'}
                ariaCurrent={isActive ? 'page' : undefined}
              >
                <Icon size={16} aria-hidden="true" />
                <span>{label}</span>
              </Link>
            </li>
          )
        })}
      </ul>

      <div className="sidebar__footer">
        <button
          type="button"
          className={assistantOpen ? 'sidebar__link sidebar__link--active' : 'sidebar__link'}
          onClick={onToggleAssistant}
          aria-expanded={assistantOpen}
          aria-controls="assistant-panel"
          title={folded ? 'Assistant' : undefined}
        >
          <Sparkles size={16} aria-hidden="true" />
          <span>Assistant</span>
        </button>
        <button
          type="button"
          className="sidebar__signout"
          onClick={() => void logout()}
          title={folded ? 'Sign out' : undefined}
        >
          <LogOut size={16} aria-hidden="true" />
          <span>Sign out</span>
        </button>
      </div>
    </nav>
  )
}
