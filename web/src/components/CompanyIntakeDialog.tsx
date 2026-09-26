import { useEffect, useRef, useState, type FormEvent } from 'react'
import { X } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyDraft, IntakeResponse } from '../lib/types'
import { useToast } from '../lib/toast'

const URL_PATTERN = /^https?:\/\/|^[\w-]+\.[a-z]{2,}(\/.*)?$/i

function looksLikeUrl(value: string): boolean {
  return URL_PATTERN.test(value.trim())
}

function normalizeUrl(value: string): string {
  const trimmed = value.trim()
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  return `https://${trimmed}`
}

export function CompanyIntakeDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (company: Company) => void
}) {
  const [quickValue, setQuickValue] = useState('')
  const [country, setCountry] = useState('')
  const [industry, setIndustry] = useState('')
  const [businessId, setBusinessId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<Company[]>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const { push } = useToast()

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const isUrl = quickValue.trim().length > 0 && looksLikeUrl(quickValue)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = quickValue.trim()
    if (!trimmed) {
      setError('Enter a company name or website.')
      return
    }

    const draft: CompanyDraft = isUrl
      ? { website: normalizeUrl(trimmed) }
      : { name: trimmed }
    if (country.trim()) draft.country = country.trim()
    if (industry.trim()) draft.industry = industry.trim()
    if (businessId.trim()) draft.business_id = businessId.trim()

    setSubmitting(true)
    setError(null)
    try {
      const result = await api.post<IntakeResponse>('/api/companies', draft)
      push(`${result.resolution === 'created' ? 'Added' : 'Found'} ${result.company.name || trimmed}`, 'success')
      onCreated(result.company)
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === 'ambiguous_company') {
        const details = cause.detail as { candidates?: Company[] } | undefined
        setCandidates(details?.candidates ?? [])
        setError('Possible matches found. Review them, or create a separate company record.')
      } else {
        setError(cause instanceof ApiError ? cause.message : 'Could not add this company.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function createSeparate() {
    const trimmed = quickValue.trim()
    const draft: CompanyDraft = isUrl ? { website: normalizeUrl(trimmed) } : { name: trimmed }
    if (country.trim()) draft.country = country.trim()
    if (industry.trim()) draft.industry = industry.trim()
    if (businessId.trim()) draft.business_id = businessId.trim()
    draft.allow_new = true
    setSubmitting(true)
    setError(null)
    try {
      const result = await api.post<IntakeResponse>('/api/companies', draft)
      push(`Added ${result.company.name || trimmed}`, 'success')
      onCreated(result.company)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not create a separate company.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-overlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="intake-title">
        <div className="dialog__header">
          <h2 id="intake-title">Add a company</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="dialog__body">
          <label htmlFor="intake-quick">Company name or website</label>
          <input
            id="intake-quick"
            ref={inputRef}
            type="text"
            placeholder="Acme Oy or acme.fi"
            value={quickValue}
            onChange={(event) => setQuickValue(event.target.value)}
            disabled={submitting}
          />
          <p className="field-hint">
            {quickValue.trim()
              ? isUrl
                ? 'Recognized as a website.'
                : 'Will be added by name.'
              : 'Paste a name or a website — we\u2019ll tell you which one it is.'}
          </p>

          <div className="field-row">
            <div className="field-col">
              <label htmlFor="intake-country">Country (optional)</label>
              <input
                id="intake-country"
                type="text"
                value={country}
                onChange={(event) => setCountry(event.target.value)}
                disabled={submitting}
              />
            </div>
            <div className="field-col">
              <label htmlFor="intake-industry">Industry (optional)</label>
              <input
                id="intake-industry"
                type="text"
                value={industry}
                onChange={(event) => setIndustry(event.target.value)}
                disabled={submitting}
              />
            </div>
          </div>

          <label htmlFor="intake-business-id">Business ID (optional)</label>
          <input
            id="intake-business-id"
            type="text"
            placeholder="1234567-8"
            value={businessId}
            onChange={(event) => setBusinessId(event.target.value)}
            disabled={submitting}
          />
          <p className="field-hint">A business ID needs a two-letter country code.</p>

          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}

          {candidates.length > 0 && (
            <div className="candidate-list">
              <strong>Possible matches</strong>
              {candidates.map((candidate) => (
                <button
                  type="button"
                  className="candidate-list__item"
                  key={candidate.id}
                  onClick={() => onCreated(candidate)}
                  disabled={submitting}
                >
                  <span>{candidate.name}</span>
                  <span className="muted small">{candidate.country ?? 'Country unknown'}</span>
                </button>
              ))}
              <button type="button" className="btn btn--secondary" onClick={() => void createSeparate()} disabled={submitting}>
                Create separate company
              </button>
            </div>
          )}

          <div className="dialog__actions">
            <button type="button" className="btn btn--ghost" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="btn btn--primary" disabled={submitting}>
              {submitting ? 'Adding…' : 'Add company'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
