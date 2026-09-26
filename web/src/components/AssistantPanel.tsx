import { useState } from 'react'
import { Sparkles, PanelRightClose, PanelRightOpen, Send } from 'lucide-react'

/**
 * Persistent global assistant panel. No assistant endpoint exists yet in the
 * API contract, so this deliberately ships as an honest, disabled shell
 * rather than a scripted or faked conversation. See API_CONTRACT.md for the
 * `/api/assistant` shape this will connect to.
 */
export function AssistantPanel() {
  const [collapsed, setCollapsed] = useState(false)
  const [draft, setDraft] = useState('')

  if (collapsed) {
    return (
      <button
        type="button"
        className="assistant-panel__reopen"
        onClick={() => setCollapsed(false)}
        aria-label="Open assistant panel"
      >
        <PanelRightOpen size={18} aria-hidden="true" />
      </button>
    )
  }

  return (
    <aside className="assistant-panel" aria-label="Assistant">
      <div className="assistant-panel__header">
        <div className="assistant-panel__title">
          <Sparkles size={16} aria-hidden="true" />
          <span>Assistant</span>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={() => setCollapsed(true)}
          aria-label="Collapse assistant panel"
        >
          <PanelRightClose size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="assistant-panel__body">
        <div className="assistant-panel__empty">
          <p>
            The assistant isn&rsquo;t connected yet. Once the backend exposes an assistant
            endpoint, this panel will let you ask about a company or draft outreach without
            leaving the page you&rsquo;re on.
          </p>
        </div>
      </div>

      <form
        className="assistant-panel__composer"
        onSubmit={(event) => event.preventDefault()}
      >
        <label htmlFor="assistant-input" className="sr-only">
          Message the assistant
        </label>
        <textarea
          id="assistant-input"
          rows={2}
          placeholder="Assistant actions activate once the backend connects"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled
        />
        <button type="submit" className="btn btn--primary" disabled>
          <Send size={14} aria-hidden="true" />
          Send
        </button>
      </form>
    </aside>
  )
}
