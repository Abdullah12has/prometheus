import { GitCompareArrows } from 'lucide-react'
import { PlaceholderView } from './PlaceholderView'

export function MatchesView() {
  return (
    <PlaceholderView
      title="Matches"
      lede="Buyer mandates ranked against companies in the pipeline."
      icon={<GitCompareArrows size={28} aria-hidden="true" />}
      emptyTitle="No matches yet"
      emptyDescription="Matches need buyer mandates and a completed match run. This view opens once that data is connected."
      expectedApi="GET /api/matches"
    />
  )
}
