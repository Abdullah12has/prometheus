import { NotesPanel } from '../components/NotesPanel'
import { VoicePanel } from '../components/VoicePanel'
import { useRouter } from '../lib/router'

type Tab = 'agents' | 'notes'

export function VoiceNotesView() {
  const { search, navigate } = useRouter()
  const tab: Tab = new URLSearchParams(search).get('tab') === 'notes' ? 'notes' : 'agents'

  function chooseTab(next: Tab) {
    const url = new URL(window.location.href)
    if (next === 'notes') url.searchParams.set('tab', 'notes')
    else url.searchParams.delete('tab')
    navigate(`${url.pathname}${url.search}`)
  }

  return <div className="page">
    <header className="page__header"><h1>Voice & notes</h1><p className="page__lede">Run a browser conversation or capture a meeting for review.</p></header>
    <div className="voice-tabs" role="tablist" aria-label="Voice workspace">
      <button type="button" role="tab" aria-selected={tab === 'agents'} aria-controls="voice-tab-agents" className={tab === 'agents' ? 'is-active' : ''} onClick={() => chooseTab('agents')}>Voice agents</button>
      <button type="button" role="tab" aria-selected={tab === 'notes'} aria-controls="voice-tab-notes" className={tab === 'notes' ? 'is-active' : ''} onClick={() => chooseTab('notes')}>Notes</button>
    </div>
    {tab === 'agents'
      ? <div id="voice-tab-agents" role="tabpanel"><VoicePanel /></div>
      : <div id="voice-tab-notes" role="tabpanel"><NotesPanel /></div>}
  </div>
}
