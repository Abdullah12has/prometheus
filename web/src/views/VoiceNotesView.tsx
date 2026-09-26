import { Mic } from 'lucide-react'
import { PlaceholderView } from './PlaceholderView'

export function VoiceNotesView() {
  return (
    <PlaceholderView
      title="Voice & notes"
      lede="Call recordings, meeting notes and their reviewed transcripts."
      icon={<Mic size={28} aria-hidden="true" />}
      emptyTitle="Nothing recorded yet"
      emptyDescription="Recordings and notes appear here once the recording and transcription API is connected. Raw transcript and reviewed summary stay visibly separate."
      expectedApi="GET /api/recordings"
    />
  )
}
