import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, timeAgo } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Alert, Empty, Page, Spinner } from '../components/Layout.jsx'

const CATEGORIES = [
  ['general', 'General question'],
  ['premium', 'Premium / billing'],
  ['bug', 'Something is broken'],
  ['feature', 'Feature request'],
]

function StatusBadge({ status }) {
  return <span className={`badge ${status}`}>{status}</span>
}

export default function Tickets() {
  const { user } = useAuth()
  const [tickets, setTickets] = useState(null)
  const [guilds, setGuilds] = useState([])
  const [scope, setScope] = useState('mine')
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(() => {
    api.tickets(scope === 'all' ? 'all' : '')
      .then((d) => setTickets(d.tickets))
      .catch((e) => { setError(e.message); setTickets([]) })
  }, [scope])

  useEffect(load, [load])
  useEffect(() => {
    // Offered in the form so a ticket can be tied to a specific server.
    api.guilds().then((d) => setGuilds(d.guilds)).catch(() => {})
  }, [])

  return (
    <Page>
      <div className="page-head">
        <h2>Support</h2>
        <div className="right">
          <button className="btn small" onClick={() => setShowForm((v) => !v)}>
            {showForm ? 'Cancel' : '+ New ticket'}
          </button>
        </div>
      </div>

      <Alert kind="error">{error}</Alert>

      {showForm && (
        <NewTicketForm
          guilds={guilds}
          onDone={() => { setShowForm(false); load() }}
          onError={setError}
        />
      )}

      {user?.isOwner && (
        <div className="tabs">
          <button className={scope === 'mine' ? 'on' : ''} onClick={() => setScope('mine')}>
            My tickets
          </button>
          <button className={scope === 'all' ? 'on' : ''} onClick={() => setScope('all')}>
            All tickets
          </button>
        </div>
      )}

      {tickets === null && <Spinner />}
      {tickets?.length === 0 && (
        <Empty>
          {scope === 'all'
            ? 'No tickets have been opened yet.'
            : "You haven't opened any tickets. Use “New ticket” if you need help."}
        </Empty>
      )}

      {tickets?.map((t) => (
        <Link className="row" to={`/tickets/${t.id}`} key={t.id} style={{ color: 'inherit' }}>
          <div className="meta">
            <strong>#{t.id} · {t.subject}</strong>
            <span>
              {t.category}
              {t.guildName && ` · ${t.guildName}`}
              {scope === 'all' && ` · ${t.username}`}
              {' · updated '}{timeAgo(t.updatedAt)}
            </span>
          </div>
          <div className="right"><StatusBadge status={t.status} /></div>
        </Link>
      ))}
    </Page>
  )
}

function NewTicketForm({ guilds, onDone, onError }) {
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [category, setCategory] = useState('general')
  const [guildId, setGuildId] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    onError('')
    try {
      await api.ticketCreate({ subject, body, category, guildId })
      setSubject(''); setBody('')
      onDone()
    } catch (err) {
      onError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card" onSubmit={submit} style={{ marginBottom: 22 }}>
      <h3>Open a ticket</h3>

      <label htmlFor="t-subject">Subject</label>
      <input
        id="t-subject"
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
        maxLength={150}
        required
        placeholder="Short summary"
      />

      <label htmlFor="t-category">Category</label>
      <select id="t-category" value={category} onChange={(e) => setCategory(e.target.value)}>
        {CATEGORIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>

      {guilds.length > 0 && (
        <>
          <label htmlFor="t-guild">Related server (optional)</label>
          <select id="t-guild" value={guildId} onChange={(e) => setGuildId(e.target.value)}>
            <option value="">— not server-specific —</option>
            {guilds.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </>
      )}

      <label htmlFor="t-body">Details</label>
      <textarea
        id="t-body"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        maxLength={4000}
        required
        placeholder="What's going on? Include anything that would help us reproduce it."
      />
      <div className="field-hint">
        {body.length}/4000 · Don't include passwords or other sensitive information.
      </div>

      <div style={{ marginTop: 16 }}>
        <button className="btn" disabled={busy || !subject.trim() || !body.trim()}>
          {busy ? 'Sending…' : 'Submit ticket'}
        </button>
      </div>
    </form>
  )
}
