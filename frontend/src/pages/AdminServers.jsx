import { useCallback, useEffect, useState } from 'react'
import { api, formatDate } from '../api.js'
import { Alert, Empty, GuildIcon, Page, Spinner, TierBadge } from '../components/Layout.jsx'

// Owner-only. The API enforces this too — this page just shouldn't be reachable
// for anyone else, and App.jsx gates the route.
export default function AdminServers() {
  const [data, setData] = useState(null)
  const [query, setQuery] = useState('')
  const [banId, setBanId] = useState('')
  const [banReason, setBanReason] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    api.servers()
      .then(setData)
      .catch((e) => { setError(e.message); setData({ servers: [], banned: [] }) })
  }, [])

  useEffect(load, [load])

  // "reason posted in #mod-log" / "no channel available" — the maintainer needs
  // to know whether the server actually heard why the bot left.
  function delivery(d) {
    return d.announced
      ? ` — reason posted in #${d.channel}`
      : ' — no channel available, reason not posted'
  }

  // Shared by Eject and Ban: both leave the server, they differ only in whether
  // the server is also blocked from re-inviting.
  async function ejectFlow(server, forceBan) {
    const reason = window.prompt(
      forceBan
        ? `Ban "${server.name}" from using TaigaBot?\n\nReason (posted publicly in the server):`
        : `Eject TaigaBot from "${server.name}"?\n\nReason (posted publicly in the server):`,
      '',
    )
    if (reason === null || !reason.trim()) return

    const confirmText = forceBan
      ? `Really BAN "${server.name}"? The bot will post the reason and leave, then ` +
        'leave again immediately every time it is re-invited.'
      : `Really eject from "${server.name}"? The bot will post the reason, then leave. ` +
        'Re-invite requires a server admin.'
    if (!window.confirm(confirmText)) return

    const ban = forceBan || window.confirm(
      `Also BAN "${server.name}"? The bot will immediately leave again if re-invited.\n\n` +
      'OK to ban · Cancel to just eject.',
    )

    setBusy(server.id); setError(''); setNotice('')
    try {
      const d = await api.guildEject(server.id, reason.trim(), ban)
      setNotice(
        `Ejected from ${server.name}${delivery(d)}${d.banned ? ' · banned' : ''}.`,
      )
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  // Ban by ID — for servers the bot isn't in yet, so an abuser can be blocked
  // before they ever add it.
  async function banById(e) {
    e.preventDefault()
    const id = banId.trim()
    const reason = banReason.trim()
    if (!/^\d+$/.test(id)) { setError('Server ID must be numeric.'); return }
    if (!reason) { setError('A reason is required.'); return }
    if (!window.confirm(
      `Ban server ${id}? If the bot is in it, it will post the reason and leave.`,
    )) return

    setBusy(id); setError(''); setNotice('')
    try {
      const d = await api.banAdd(id, reason)
      setNotice(`Server ${id} banned${d.ejected ? `, and ejected${delivery(d)}` : ''}.`)
      setBanId(''); setBanReason('')
      load()
    } catch (e2) {
      setError(e2.message)
    } finally {
      setBusy('')
    }
  }

  async function unban(row) {
    if (!window.confirm(`Unban "${row.name}"? It will be able to add the bot again.`)) return
    setBusy(row.id); setError(''); setNotice('')
    try {
      await api.banRemove(row.id)
      setNotice(`${row.name} unbanned.`)
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const servers = data?.servers ?? []
  const banned = data?.banned ?? []
  const q = query.trim().toLowerCase()
  const visible = servers.filter(
    (s) => !q || s.name.toLowerCase().includes(q) || s.id.includes(q),
  )

  return (
    <Page>
      <div className="page-head">
        <h2>Servers</h2>
      </div>

      <p className="muted">
        Eject removes the bot from a server: it posts your reason there first, then
        leaves. A banned server can't use the bot at all — if it re-invites it, the
        bot posts the ban reason and leaves again.
      </p>

      <Alert kind="error">{error}</Alert>
      <Alert kind="success">{notice}</Alert>

      <h3>In {servers.length} server{servers.length === 1 ? '' : 's'}</h3>

      <input
        placeholder="Search by server name or ID…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      {data === null && <Spinner />}
      {data !== null && visible.length === 0 && <Empty>No servers match.</Empty>}

      {visible.map((s) => (
        <div className="row" key={s.id}>
          <GuildIcon icon={s.icon} name={s.name} />
          <div className="meta">
            <strong>
              {s.name}
              {s.banned && <span className="badge" style={{ marginLeft: 8 }}>🚫 banned</span>}
            </strong>
            <span>
              <code>{s.id}</code>
              {s.memberCount > 0 && ` · ${s.memberCount.toLocaleString()} members`}
            </span>
          </div>
          <div className="right">
            <TierBadge tier={s.tier} />
            <button
              className="btn small danger"
              disabled={busy === s.id}
              onClick={() => ejectFlow(s, false)}
            >
              Eject
            </button>
            {!s.banned && (
              <button
                className="btn small danger"
                disabled={busy === s.id}
                onClick={() => ejectFlow(s, true)}
              >
                Ban
              </button>
            )}
          </div>
        </div>
      ))}

      <h3 style={{ marginTop: 34 }}>Banned ({banned.length})</h3>

      <form className="card" onSubmit={banById} style={{ marginBottom: 16 }}>
        <label htmlFor="ban-id">Ban a server by ID</label>
        <input
          id="ban-id"
          placeholder="Server ID"
          value={banId}
          onChange={(e) => setBanId(e.target.value)}
        />
        <input
          placeholder="Reason (shown to the server)"
          value={banReason}
          onChange={(e) => setBanReason(e.target.value)}
          style={{ marginTop: 8 }}
        />
        <button className="btn small danger" type="submit" style={{ marginTop: 12 }}>
          Ban server
        </button>
      </form>

      {data !== null && banned.length === 0 && <Empty>No servers are banned.</Empty>}

      {banned.map((b) => (
        <div className="row" key={b.id}>
          {/* Blank name → GuildIcon's own "?" placeholder, rather than the "("
              of a "(bot never joined)" label. */}
          <GuildIcon icon="" name={b.name.startsWith('(') ? '' : b.name} />
          <div className="meta">
            <strong>
              🚫 {b.name}
              {b.present && (
                <span className="badge" style={{ marginLeft: 8 }}>bot still in server</span>
              )}
            </strong>
            <span>
              <code>{b.id}</code>
              {` · banned ${formatDate(b.bannedAt)}`}
              {b.reason && ` · ${b.reason}`}
            </span>
          </div>
          <div className="right">
            <button
              className="btn small secondary"
              disabled={busy === b.id}
              onClick={() => unban(b)}
            >
              Unban
            </button>
          </div>
        </div>
      ))}
    </Page>
  )
}
