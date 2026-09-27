import { useState } from 'react'
import { AuthProvider, useAuth } from './lib/auth'
import { RouterProvider, useSegments } from './lib/router'
import { ToastProvider } from './lib/toast'
import { Sidebar } from './components/Sidebar'
import { AssistantPanel } from './components/AssistantPanel'
import { LoadingBlock } from './components/StateViews'
import { LoginView } from './views/LoginView'
import { OverviewView } from './views/OverviewView'
import { CompaniesView } from './views/CompaniesView'
import { CompanyDetailView } from './views/CompanyDetailView'
import { BuyersView } from './views/BuyersView'
import { BuyerDetailView } from './views/BuyerDetailView'
import { OutreachView } from './views/OutreachView'
import { FuturesView } from './views/FuturesView'
import { MatchesView } from './views/MatchesView'
import { VoiceNotesView } from './views/VoiceNotesView'
import { SettingsView } from './views/SettingsView'

function RouteOutlet() {
  const segments = useSegments()
  const [section, ...rest] = segments

  switch (section) {
    case undefined:
    case 'overview':
      return <OverviewView />
    case 'companies':
      return rest[0] ? <CompanyDetailView id={rest[0]} /> : <CompaniesView />
    case 'buyers':
      return rest[0] ? <BuyerDetailView id={rest[0]} /> : <BuyersView />
    case 'outreach':
      return <OutreachView />
    case 'futures':
      return <FuturesView />
    case 'matches':
      return <MatchesView />
    case 'voice-notes':
      return <VoiceNotesView />
    case 'settings':
      return <SettingsView />
    default:
      return <OverviewView />
  }
}

function Shell() {
  const { status } = useAuth()
  const [assistantOpen, setAssistantOpen] = useState(false)

  if (status === 'checking') {
    return (
      <div className="boot-screen">
        <LoadingBlock label="Loading workspace…" />
      </div>
    )
  }

  if (status === 'signed-out') {
    return <LoginView />
  }

  return (
    <div className="app-shell">
      <Sidebar assistantOpen={assistantOpen} onToggleAssistant={() => setAssistantOpen((open) => !open)} />
      <main className="app-shell__content" id="main-content">
        <RouteOutlet />
      </main>
      <AssistantPanel open={assistantOpen} onClose={() => setAssistantOpen(false)} />
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <RouterProvider>
        <ToastProvider>
          <Shell />
        </ToastProvider>
      </RouterProvider>
    </AuthProvider>
  )
}
