import { useState, type FormEvent } from 'react'
import { Lock } from 'lucide-react'
import { useAuth } from '../lib/auth'

export function LoginView() {
  const { login } = useAuth()
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!password) return
    setSubmitting(true)
    setError(null)
    try {
      await login(password)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Sign-in failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit}>
        <img className="login-card__mark" src="/logo.png" alt="" />
        <h1>Permetheus</h1>
        <p className="login-card__subtitle">Sign in to your workspace</p>

        <label htmlFor="password">Password</label>
        <div className="login-card__field">
          <Lock size={16} aria-hidden="true" />
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </div>

        {error && (
          <p className="login-card__error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="btn btn--primary btn--block" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
