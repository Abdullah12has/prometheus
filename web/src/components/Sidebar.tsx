import {
  LayoutDashboard,
  Building2,
  Send,
  Compass,
  GitCompareArrows,
  Mic,
  Settings,
  LogOut,
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

export function Sidebar() {
  const segments = useSegments()
  const activeSegment = segments[0] ?? 'overview'
  const { logout } = useAuth()

  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__brand">
        <span className="sidebar__mark" aria-hidden="true" />
        <span className="sidebar__name">Permetheus</span>
      </div>

      <ul className="sidebar__list">
        {NAV_ITEMS.map(({ to, label, icon: Icon, segment }) => {
          const isActive = activeSegment === segment
          return (
            <li key={to}>
              <Link
                to={to}
                className={isActive ? 'sidebar__link sidebar__link--active' : 'sidebar__link'}
              >
                <Icon size={18} aria-hidden="true" />
                <span>{label}</span>
              </Link>
            </li>
          )
        })}
      </ul>

      <button type="button" className="sidebar__signout" onClick={() => void logout()}>
        <LogOut size={16} aria-hidden="true" />
        <span>Sign out</span>
      </button>
    </nav>
  )
}
