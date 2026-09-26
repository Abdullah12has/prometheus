import { Send } from 'lucide-react'
import { PlaceholderView } from './PlaceholderView'

export function OutreachView() {
  return (
    <PlaceholderView
      title="Outreach"
      lede="Email threads, sequences and calls with company contacts."
      icon={<Send size={28} aria-hidden="true" />}
      emptyTitle="No conversations yet"
      emptyDescription="Outreach opens once a conversations API is connected. You'll see threads, the current sequence step and the next scheduled action here."
      expectedApi="GET /api/outreach"
    />
  )
}
