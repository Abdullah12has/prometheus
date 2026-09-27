import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { Company, CompanyDetail, CompanyListResponse } from '../lib/types'
import './company-picker.css'

interface CompanyPickerProps {
  id: string
  value: string | string[]
  onChange: (value: string | string[]) => void
  multiple?: boolean
  required?: boolean
  disabled?: boolean
  emptyLabel?: string
  searchLabel?: string
  selectLabel?: string
  onResolved?: (companies: Company[]) => void
}

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : 'Could not search companies.'
}

export function CompanyPicker({
  id,
  value,
  onChange,
  multiple = false,
  required = false,
  disabled = false,
  emptyLabel = 'Select a company…',
  searchLabel = 'Search companies by name or business ID',
  selectLabel,
  onResolved,
}: CompanyPickerProps) {
  const valueKey = Array.isArray(value) ? value.join('\u0000') : value
  const selectedIds = useMemo(() => valueKey ? valueKey.split('\u0000') : [], [valueKey])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Company[]>([])
  const [known, setKnown] = useState<Record<string, Company>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  useEffect(() => {
    const currentRequest = ++requestId.current
    const timer = window.setTimeout(() => {
      setLoading(true)
      const params = new URLSearchParams({ limit: '20' })
      if (query.trim()) params.set('q', query.trim())
      api.get<CompanyListResponse | Company[]>(`/api/companies?${params.toString()}`)
        .then((response) => {
          if (requestId.current !== currentRequest) return
          const items = Array.isArray(response) ? response : response.items
          setResults(items)
          setKnown((current) => ({ ...current, ...Object.fromEntries(items.map((company) => [company.id, company])) }))
          setError(null)
        })
        .catch((cause) => {
          if (requestId.current === currentRequest) setError(errorMessage(cause))
        })
        .finally(() => {
          if (requestId.current === currentRequest) setLoading(false)
        })
    }, 200)
    return () => { window.clearTimeout(timer); requestId.current = currentRequest + 1 }
  }, [query])

  useEffect(() => {
    const missing = selectedIds.filter((companyId) => !known[companyId] && !results.some((company) => company.id === companyId))
    if (!missing.length) return
    let cancelled = false
    Promise.all(missing.map((companyId) => api.get<CompanyDetail>(`/api/companies/${encodeURIComponent(companyId)}`)
      .then((company) => company as Company)
      .catch(() => null)))
      .then((items) => {
        if (cancelled) return
        const found = items.filter((company): company is Company => company !== null)
        if (found.length) setKnown((current) => ({ ...current, ...Object.fromEntries(found.map((company) => [company.id, company])) }))
      })
    return () => { cancelled = true }
  }, [selectedIds, known, results])

  const options = useMemo(() => {
    const byId = new Map(results.map((company) => [company.id, company]))
    for (const companyId of selectedIds) {
      const selected = known[companyId]
      if (selected) byId.set(companyId, selected)
    }
    return Array.from(byId.values())
  }, [known, results, selectedIds])
  const resolved = useMemo(
    () => selectedIds.map((companyId) => known[companyId] ?? results.find((company) => company.id === companyId)).filter((company): company is Company => Boolean(company)),
    [known, results, selectedIds],
  )

  useEffect(() => { onResolved?.(resolved) }, [onResolved, resolved])

  return <div className="company-picker-control">
    <input
      className="company-picker-control__search"
      type="search"
      aria-label={searchLabel}
      placeholder="Search by company name or ID"
      maxLength={200}
      value={query}
      onChange={(event) => setQuery(event.target.value)}
      disabled={disabled}
      autoComplete="off"
    />
    <select
      id={id}
      value={value}
      onChange={(event) => {
        if (multiple) {
          const next = Array.from(event.currentTarget.selectedOptions, (option) => option.value)
          onChange(next)
        } else {
          onChange(event.target.value)
        }
      }}
      multiple={multiple}
      required={required}
      disabled={disabled}
      aria-describedby={`${id}-status`}
      aria-label={selectLabel}
      className="company-picker-control__select"
      size={multiple ? Math.min(Math.max(options.length, 4), 8) : undefined}
    >
      {!multiple && <option value="">{emptyLabel}</option>}
      {selectedIds.filter((companyId) => !options.some((company) => company.id === companyId)).map((companyId) => (
        <option key={companyId} value={companyId}>Loading selected company…</option>
      ))}
      {options.map((company) => <option key={company.id} value={company.id}>{company.name || 'Unnamed company'}{company.country ? ` — ${company.country}` : ''}</option>)}
    </select>
    <span id={`${id}-status`} className="company-picker-control__status" role="status" aria-live="polite">
      {loading ? 'Searching…' : error ?? `${options.length} result${options.length === 1 ? '' : 's'}${query.trim() ? ` for “${query.trim()}”` : ''}`}
    </span>
  </div>
}
