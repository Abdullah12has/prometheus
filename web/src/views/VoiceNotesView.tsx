import { NotesPanel } from '../components/NotesPanel'
import { VoicePanel } from '../components/VoicePanel'

export function VoiceNotesView() {
  return <div className="page">
    <header className="page__header"><h1>Voice & notes</h1><p className="page__lede">Have a browser conversation or save a meeting recording.</p></header>
    <VoicePanel />
    <NotesPanel />
  </div>
}
