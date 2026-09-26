import { Compass } from 'lucide-react'
import { PlaceholderView } from './PlaceholderView'

export function FuturesView() {
  return (
    <PlaceholderView
      title="Futures"
      lede="Conditions an owner would need before committing to a sale."
      icon={<Compass size={28} aria-hidden="true" />}
      emptyTitle="No futures recorded"
      emptyDescription="Futures show up per company once owner preferences are captured. This view opens once that data is connected."
      expectedApi="GET /api/futures"
    />
  )
}
