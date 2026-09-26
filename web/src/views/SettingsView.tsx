import { useEffect, useState } from 'react'
import { CheckCircle2, HelpCircle } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Connector, SettingsStatus } from '../lib/types'
import { LoadingBlock, ErrorBlock } from '../components/StateViews'
import { useAuth } from '../lib/auth'

function StatusRow({ connector }: { connector: Connector }) {
  const Icon = connector.configured ? CheckCircle2 : HelpCircle
  return (
    <li className={`status-row status-row--${connector.configured ? 'ok' : 'unconfigured'}`}>
      <Icon size={16} aria-hidden="true" />
      <div>
        <div className="status-row__name">{connector.label}</div>
        <div className="muted small">
          {connector.configured ? 'Configured' : `Missing: ${connector.missing.join(', ') || 'details'}`}
          {' · '}
          {connector.implemented ? 'Available' : 'Not implemented'}
        </div>
        <div className="muted small">{connector.note}</div>
      </div>
    </li>
  )
}

export function SettingsView() {
  const [data, setData] = useState<SettingsStatus | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const { logout } = useAuth()

  function load() {
    setStatus('loading')
    setError(null)
    api
      .get<SettingsStatus>('/api/settings/status')
      .then((result) => {
        setData(result)
        setStatus('ready')
      })
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : 'Could not load status.')
        setStatus('error')
      })
  }

  useEffect(load, [])

  return (
    <div className="page">
      <header className="page__header">
        <h1>Settings</h1>
        <p className="page__lede">Environment status and workspace access.</p>
      </header>

      {status === 'loading' && <LoadingBlock label="Checking status…" />}
      {status === 'error' && <ErrorBlock message={error ?? 'Something went wrong.'} onRetry={load} />}

      {status === 'ready' && data && (
        <section className="panel">
          <div className="panel__row">
            <h2>Connectors</h2>
          </div>
          <p className="muted small">Status is read-only here. Configure missing environment values in the backend deployment.</p>
          {data.connectors.length > 0 ? (
            <ul className="status-list">
              {data.connectors.map((connector) => (
                <StatusRow key={connector.id} connector={connector} />
              ))}
            </ul>
          ) : (
            <p className="muted">No service checks reported.</p>
          )}
        </section>
      )}

      {status === 'ready' && data && (
        <section className="panel">
          <h2>Workspace safeguards</h2>
          <dl className="detail-grid">
            <div><dt>Database</dt><dd>{data.database}</dd></div>
            <div><dt>Authentication</dt><dd>{data.auth.configured ? 'Configured' : 'Not configured'}</dd></div>
            <div><dt>Email dispatch</dt><dd>{data.outbound_dispatch.email}</dd></div>
            <div><dt>Phone dispatch</dt><dd>{data.outbound_dispatch.phone}</dd></div>
          </dl>
        </section>
      )}

      <section className="panel">
        <h2>Session</h2>
        <p className="muted">Sign out of this workspace on this device.</p>
        <button type="button" className="btn btn--secondary" onClick={() => void logout()}>
          Sign out
        </button>
      </section>
    </div>
  )
}
