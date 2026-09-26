import type { SellerIntent } from '../lib/types'

const LABELS: Record<SellerIntent, string> = {
  unknown: 'Intent unknown',
  interested: 'Interested',
  conditional: 'Conditional',
  not_now: 'Not now',
  not_interested: 'Not interested',
}

export function SellerIntentBadge({ intent }: { intent: SellerIntent }) {
  return <span className={`intent-badge intent-badge--${intent}`}>{LABELS[intent]}</span>
}

export const SELLER_INTENT_OPTIONS: SellerIntent[] = [
  'unknown',
  'interested',
  'conditional',
  'not_now',
  'not_interested',
]
