import { NotesPanel } from '../components/NotesPanel'
import { VoicePanel } from '../components/VoicePanel'
import { useRouter } from '../lib/router'

type Tab = 'agents' | 'notes'

export function VoiceNotesView({ active }: { active: boolean }) {
  const { search, navigate } = useRouter()
  const tab: Tab = new URLSearchParams(search).get('tab') === 'notes' ? 'notes' : 'agents'

  function chooseTab(next: Tab) {
    const url = new URL(window.location.href)
    if (next === 'notes') url.searchParams.set('tab', 'notes')
    else url.searchParams.delete('tab')
    navigate(`${url.pathname}${url.search}`)
  }

  return <div className={active ? 'page' : undefined}>
    {active && <header className="page__header"><h1>Voice & notes</h1><p className="page__lede">Run a browser conversation with a voice agent, or capture a meeting for review.</p></header>}
    {active && <div className="view-tabs" role="tablist" aria-label="Voice workspace">
      <button type="button" role="tab" aria-selected={tab === 'agents'} aria-controls="voice-tab-agents" className={tab === 'agents' ? 'is-active' : ''} onClick={() => chooseTab('agents')}>Voice agents</button>
      <button type="button" role="tab" aria-selected={tab === 'notes'} aria-controls="voice-tab-notes" className={tab === 'notes' ? 'is-active' : ''} onClick={() => chooseTab('notes')}>Notes</button>
    </div>}
    {/* Keep the recording owner mounted across tabs and workspace routes. */}
    <div id="voice-tab-notes" role={active && tab === 'notes' ? 'tabpanel' : undefined}>
      <NotesPanel active={active && tab === 'notes'} />
    </div>
    {active && tab === 'agents' && <div id="voice-tab-agents" role="tabpanel"><VoicePanel /></div>}
  </div>
}
