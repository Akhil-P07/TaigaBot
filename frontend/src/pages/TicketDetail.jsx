import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, timeAgo } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Alert, Page, Spinner } from '../components/Layout.jsx'

export default function TicketDetail() {
  const { ticketId } = useParams()
  const { user } = useAuth()
  const [ticket, setTicket] = useState(null)
  const [reply, setReply] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.ticket(ticketId).then(setTicket).catch((e) => setError(e.message))
  }, [ticketId])

  useEffect(load, [load])

  async function send(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      await api.ticketReply(ticketId, reply)
      setReply('')
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function setStatus(status) {
    setBusy(true); setError('')
    try {
      await api.ticketStatus(ticketId, status)
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (error && !ticket) {
    return (
      <Page narrow>
        <Alert kind="error">{error}</Alert>
        <Link className="btn secondary" to="/tickets">← Back to support</Link>
      </Page>
    )
  }
  if (!ticket) return <Page narrow><Spinner /></Page>

  const closed = ticket.status === 'closed'

  return (
    <Page narrow>
      <div className="page-head">
        <h2>#{ticket.id} · {ticket.subject}</h2>
        <span className={`badge ${ticket.status}`}>{ticket.status}</span>
        <div className="right">
          <Link className="btn small secondary" to="/tickets">← Support</Link>
        </div>
      </div>

      <p className="muted">
        {ticket.category}
        {ticket.guildName && ` · ${ticket.guildName}`}
        {' · opened by '}{ticket.username}{' '}{timeAgo(ticket.createdAt)}
      </p>

      <Alert kind="error">{error}</Alert>

      <div style={{ margin: '20px 0' }}>
        {ticket.messages.map((m) => (
          <div className={`msg${m.isStaff ? ' staff' : ''}`} key={m.id}>
            <div className="who">
              <strong>{m.authorName}</strong>
              {m.isStaff && <span className="badge">staff</span>}
              <span>{timeAgo(m.createdAt)}</span>
            </div>
            <div className="body">{m.body}</div>
          </div>
        ))}
      </div>

      {closed ? (
        <Alert kind="info">
          This ticket is closed.
          {user?.isOwner && (
            <> <button className="btn small secondary" onClick={() => setStatus('open')}>
              Reopen
            </button></>
          )}
        </Alert>
      ) : (
        <form onSubmit={send} className="card">
          <label htmlFor="reply">Reply</label>
          <textarea
            id="reply"
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            maxLength={4000}
            required
            placeholder="Add to the conversation…"
          />
          <div style={{ marginTop: 14, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button className="btn" disabled={busy || !reply.trim()}>
              {busy ? 'Sending…' : 'Send reply'}
            </button>
            <button
              type="button"
              className="btn secondary"
              disabled={busy}
              onClick={() => setStatus('closed')}
            >
              Close ticket
            </button>
          </div>
        </form>
      )}
    </Page>
  )
}
