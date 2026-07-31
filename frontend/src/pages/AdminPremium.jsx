import { useCallback, useEffect, useState } from 'react'
import { api, formatDate } from '../api.js'
import { Alert, Empty, GuildIcon, Page, Spinner, TierBadge } from '../components/Layout.jsx'

// Owner-only. The API enforces this too — this page just shouldn't be reachable
// for anyone else, and App.jsx gates the route.
export default function AdminPremium() {
  const [servers, setServers] = useState(null)
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    api.premiumList()
      .then((d) => setServers(d.servers))
      .catch((e) => { setError(e.message); setServers([]) })
  }, [])

  useEffect(load, [load])

  async function grant(server) {
    const raw = window.prompt(
      `Grant premium to "${server.name}".\n\n` +
      'Days until it expires — leave blank or enter 0 for no expiry:',
      '',
    )
    if (raw === null) return
    const days = parseInt(raw, 10) || 0
    const note = window.prompt('Note (who paid, how much, when) — optional:', server.note || '') ?? ''

    setBusy(server.id); setError(''); setNotice('')
    try {
      await api.premiumGrant(server.id, days, note)
      setNotice(`Premium granted to ${server.name}${days > 0 ? ` for ${days} days` : ''}.`)
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function revoke(server) {
    if (!window.confirm(`Remove premium from "${server.name}"?`)) return
    setBusy(server.id); setError(''); setNotice('')
    try {
      await api.premiumRevoke(server.id)
      setNotice(`Premium revoked from ${server.name}.`)
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const visible = (servers || []).filter((s) => {
    if (filter === 'premium' && s.tier !== 'premium') return false
    if (filter === 'free' && s.tier === 'premium') return false
    if (query && !s.name.toLowerCase().includes(query.toLowerCase()) && !s.id.includes(query)) {
      return false
    }
    return true
  })

  const premiumCount = (servers || []).filter((s) => s.tier === 'premium').length

  return (
    <Page>
      <div className="page-head">
        <h2>Premium management</h2>
      </div>

      <p className="muted">
        Payment is handled offline — grant a tier here once you've been paid. Grants
        can be time-limited or open-ended, and can be revoked at any time.
      </p>

      <Alert kind="error">{error}</Alert>
      <Alert kind="success">{notice}</Alert>

      <div className="tabs">
        {['all', 'premium', 'free'].map((f) => (
          <button key={f} className={filter === f ? 'on' : ''} onClick={() => setFilter(f)}>
            {f === 'all' ? `All (${servers?.length ?? 0})`
              : f === 'premium' ? `Premium (${premiumCount})`
              : 'Free'}
          </button>
        ))}
      </div>

      <input
        placeholder="Search by server name or ID…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      {servers === null && <Spinner />}
      {servers !== null && visible.length === 0 && <Empty>No servers match.</Empty>}

      {visible.map((s) => (
        <div className="row" key={s.id}>
          <GuildIcon icon={s.icon} name={s.name} />
          <div className="meta">
            <strong>
              {s.name}
              {s.orphaned && <span className="badge" style={{ marginLeft: 8 }}>bot removed</span>}
            </strong>
            <span>
              <code>{s.id}</code>
              {s.memberCount > 0 && ` · ${s.memberCount.toLocaleString()} members`}
              {s.tier === 'premium' && (
                <> · {s.expiresAt ? `expires ${formatDate(s.expiresAt)}` : 'no expiry'}</>
              )}
              {s.note && ` · ${s.note}`}
            </span>
          </div>
          <div className="right">
            <TierBadge tier={s.tier} />
            <button
              className="btn small secondary"
              disabled={busy === s.id}
              onClick={() => grant(s)}
            >
              {s.tier === 'premium' ? 'Edit' : 'Grant'}
            </button>
            {s.tier === 'premium' && (
              <button
                className="btn small danger"
                disabled={busy === s.id}
                onClick={() => revoke(s)}
              >
                Revoke
              </button>
            )}
          </div>
        </div>
      ))}
    </Page>
  )
}
