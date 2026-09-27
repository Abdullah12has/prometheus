import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { AlertCircle, CheckCircle2, FileText, Loader2, RefreshCw, Upload } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import './documents.css'

const MAX_BYTES = 25 * 1024 * 1024
const ACTIVE = new Set(['queued', 'processing'])

type ProposedFinancial = {
  metric: string
  amount: string | null
  currency: string | null
  review_status: string
}

type CompanyDocument = {
  id: string
  company_id: string
  title: string
  media_type: string
  byte_size: number
  sha256: string
  status: string
  progress: number
  result: unknown[]
  financials: ProposedFinancial[]
  warning: string | null
  error: string | null
  job_id?: string | null
  created_at: string
}

type DocumentsPanelProps = {
  companyId: string
  onUpdated?: () => void
}

function formatBytes(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function explain(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.message : fallback
}

export function DocumentsPanel({ companyId, onUpdated }: DocumentsPanelProps) {
  const [documents, setDocuments] = useState<CompanyDocument[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const previousStatuses = useRef<Map<string, string> | null>(null)
  const [loadedCompanyId, setLoadedCompanyId] = useState(companyId)

  const load = useCallback(async () => {
    try {
      const rows = await api.get<CompanyDocument[]>(`/api/companies/${companyId}/documents`)
      const prior = previousStatuses.current
      if (prior) {
        const completed = rows.some((row) => prior.get(row.id) && ACTIVE.has(prior.get(row.id)!) && row.status === 'completed')
        if (completed) onUpdated?.()
      }
      previousStatuses.current = new Map(rows.map((row) => [row.id, row.status]))
      setDocuments(rows)
      setLoadedCompanyId(companyId)
      setError(null)
    } catch (cause) {
      setError(explain(cause, 'Could not load company documents.'))
    } finally {
      setLoading(false)
    }
  }, [companyId, onUpdated])

  useEffect(() => {
    previousStatuses.current = null
    void Promise.resolve().then(load)
  }, [load])

  useEffect(() => {
    if (!documents.some((row) => ACTIVE.has(row.status))) return
    const timer = window.setTimeout(() => void load(), 2500)
    return () => window.clearTimeout(timer)
  }, [documents, load])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setError('Choose a PDF or XHTML/iXBRL document first.')
      return
    }
    if (file.size === 0 || file.size > MAX_BYTES) {
      setError('The file must be non-empty and no larger than 25 MiB.')
      return
    }
    const lowerName = file.name.toLowerCase()
    const allowed = file.type === 'application/pdf' || file.type === 'application/xhtml+xml' ||
      file.type === 'application/ixbrl+xml' || lowerName.endsWith('.pdf') || lowerName.endsWith('.xhtml')
    if (!allowed) {
      setError('Choose a PDF or XHTML/iXBRL document.')
      return
    }
    const form = new FormData()
    form.append('document', file)
    setUploading(true)
    setError(null)
    setNotice(null)
    try {
      await api.upload<CompanyDocument>(`/api/companies/${companyId}/documents`, form)
      if (fileRef.current) fileRef.current.value = ''
      setNotice('Document uploaded. Financial figures will appear as proposals for review after extraction.')
      await load()
    } catch (cause) {
      setError(explain(cause, 'Could not upload the document.'))
    } finally {
      setUploading(false)
    }
  }

  async function retry(row: CompanyDocument) {
    if (!row.job_id) return
    setRetrying(row.id)
    setError(null)
    try {
      await api.post(`/api/jobs/${row.job_id}/retry`)
      setNotice('Extraction queued again.')
      await load()
    } catch (cause) {
      setError(explain(cause, 'Could not retry extraction.'))
    } finally {
      setRetrying(null)
    }
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    setError(file && file.size > MAX_BYTES ? 'The file must be no larger than 25 MiB.' : null)
  }

  const visibleDocuments = loadedCompanyId === companyId ? documents : []
  const isLoading = loading || loadedCompanyId !== companyId

  return (
    <section className="documents-panel" aria-labelledby="documents-heading">
      <div className="documents-panel__heading">
        <div>
          <h2 id="documents-heading">Financial documents</h2>
          <p className="muted">Upload a report to extract cited financial figures for review.</p>
        </div>
        <button className="btn btn--ghost" type="button" onClick={() => void load()} aria-label="Refresh documents">
          <RefreshCw size={15} aria-hidden="true" /> Refresh
        </button>
      </div>

      <form className="documents-upload" onSubmit={submit}>
        <label htmlFor="company-document-file">PDF or XHTML/iXBRL file</label>
        <div className="documents-upload__row">
          <input
            ref={fileRef}
            id="company-document-file"
            name="document"
            type="file"
            accept=".pdf,.xhtml,application/pdf,application/xhtml+xml,application/ixbrl+xml"
            onChange={onFileChange}
            disabled={uploading}
          />
          <button className="btn btn--primary" type="submit" disabled={uploading}>
            {uploading ? <Loader2 size={15} className="documents-spin" aria-hidden="true" /> : <Upload size={15} aria-hidden="true" />}
            {uploading ? 'Uploading…' : 'Upload document'}
          </button>
        </div>
        <p className="muted small">Maximum size 25 MiB. Extracted figures stay proposed until reviewed.</p>
      </form>

      {error && <p className="documents-message documents-message--error" role="alert"><AlertCircle size={16} aria-hidden="true" />{error}</p>}
      {notice && <p className="documents-message documents-message--success" role="status"><CheckCircle2 size={16} aria-hidden="true" />{notice}</p>}

      {isLoading ? <p className="muted">Loading documents…</p> : visibleDocuments.length === 0 ? (
        <p className="muted documents-empty">No documents uploaded yet.</p>
      ) : (
        <ul className="documents-list">
          {visibleDocuments.map((row) => {
            const proposed = row.financials?.filter((item) => item.review_status === 'proposed').length ?? 0
            return (
              <li className="documents-item" key={row.id}>
                <div className="documents-item__top">
                  <FileText size={18} aria-hidden="true" />
                  <div className="documents-item__title">
                    <strong>{row.title || 'Company document'}</strong>
                    <span className="muted small">{formatBytes(row.byte_size)} · {new Date(row.created_at).toLocaleDateString()}</span>
                  </div>
                  <span className={`documents-status documents-status--${row.status}`}>
                    {ACTIVE.has(row.status) && <Loader2 size={13} className="documents-spin" aria-hidden="true" />}
                    {row.status}
                  </span>
                </div>
                {ACTIVE.has(row.status) && <progress className="documents-progress" value={row.progress} max={100} aria-label={`Extraction ${row.progress}% complete`} />}
                <div className="documents-item__meta">
                  <span>{proposed} proposed {proposed === 1 ? 'financial figure' : 'financial figures'}</span>
                  <a href={`/api/documents/${row.id}/source`} target="_blank" rel="noreferrer">Open original</a>
                </div>
                {row.warning && <p className="documents-warning" role="status">{row.warning}</p>}
                {row.error && <p className="documents-message documents-message--error" role="alert">{row.error}</p>}
                {row.status === 'failed' && row.job_id && (
                  <button className="btn btn--ghost" type="button" onClick={() => void retry(row)} disabled={retrying === row.id}>
                    {retrying === row.id ? 'Retrying…' : 'Retry extraction'}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
      <p className="documents-review-note">Extracted amounts remain proposed. Review them in the Financials tab before accepting.</p>
    </section>
  )
}
