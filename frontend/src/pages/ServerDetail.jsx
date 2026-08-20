import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, formatDate, timeAgo } from '../api.js'
import { Alert, Empty, GuildIcon, Page, Spinner, TierBadge } from '../components/Layout.jsx'

function hoursUntil(then, now) {
  const mins = Math.max(1, Math.ceil((then - now) / 60))
  if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'}`
  const hours = Math.ceil(mins / 60)
  return `${hours} hour${hours === 1 ? '' : 's'}`
}

/** Download this server's verified members as a decrypted CSV.
 *
 * The file holds real names and RIT emails, so the copy here says so plainly and
 * the server enforces a cooldown (this component only *shows* it — never trust
 * the disabled button to be the limit).
 */
function RosterExport({ guildId, info }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(0)
  const [availableAt, setAvailableAt] = useState(info?.availableAt || 0)

  const now = Math.floor(Date.now() / 1000)
  const onCooldown = availableAt > now

  async function download() {
    setBusy(true)
    setError('')
    try {
      const { blob, count } = await api.rosterExport(guildId)
      // Object URLs leak until revoked; do it as soon as the click is consumed.
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `roster-${guildId}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setDone(count)
      setAvailableAt(now + (info?.cooldownHours || 12) * 3600)
    } catch (e) {
      setError(e.message)
      if (e.retryAt) setAvailableAt(e.retryAt)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <p className="muted" style={{ marginTop: 0 }}>
        Exports every member holding the <strong>Verified</strong> role, with the real
        name and RIT email they verified with. This file is <strong>not</strong>{' '}
        encrypted — store it like you would the roster itself. Limited to one export
        every {info?.cooldownHours || 12} hours per server, and each export is logged.
      </p>

      {error && <Alert kind="error">{error}</Alert>}
      {done > 0 && !error && (
        <Alert kind="success">Downloaded {done} member(s).</Alert>
      )}

      <button
        className="btn"
        onClick={download}
        disabled={busy || onCooldown}
      >
        {busy ? 'Preparing…' : 'Export roster (CSV)'}
      </button>

      {onCooldown && (
        <p className="muted" style={{ marginBottom: 0 }}>
          {/* timeAgo() only formats the past — it reads a future timestamp as
              "just now" — so spell the remaining wait out instead. */}
          Available again in {hoursUntil(availableAt, now)}, on{' '}
          {formatDate(availableAt)}.
        </p>
      )}
      {!onCooldown && info?.lastExportAt > 0 && (
        <p className="muted" style={{ marginBottom: 0 }}>
          Last exported {timeAgo(info.lastExportAt)}.
        </p>
      )}
    </div>
  )
}

export default function ServerDetail() {
  const { guildId } = useParams()
  const [guild, setGuild] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.guild(guildId)
      .then(setGuild)
      .catch((e) => setError(e.message))
  }, [guildId])

  if (error) {
    return (
      <Page narrow>
        <Alert kind="error">{error}</Alert>
        <Link className="btn secondary" to="/dashboard">← Back to your servers</Link>
      </Page>
    )
  }
  if (!guild) return <Page><Spinner /></Page>

  const feedsUsed = guild.news.filter((n) => n.label === 'custom').length

  return (
    <Page>
      <div className="page-head">
        <GuildIcon icon={guild.icon} name={guild.name} />
        <h2>{guild.name}</h2>
        <TierBadge tier={guild.tier} />
        <div className="right">
          <Link className="btn small secondary" to="/dashboard">← All servers</Link>
        </div>
      </div>

      <div className="grid" style={{ marginBottom: 24 }}>
        <div className="card">
          <h3>Tier</h3>
          <p>
            {guild.tier === 'premium' ? (
              <>
                Premium is active
                {guild.premiumExpiresAt
                  ? <> until {formatDate(guild.premiumExpiresAt)}.</>
                  : <> with no expiry.</>}
              </>
            ) : (
              <>
                This server is on the free tier. Premium is arranged offline — open a
                support ticket to ask about it.
              </>
            )}
          </p>
        </div>
        <div className="card">
          <h3>Members</h3>
          <p>{guild.memberCount?.toLocaleString()} members</p>
        </div>
        <div className="card">
          <h3>Custom news feeds</h3>
          <p>
            {feedsUsed} of {guild.limits.customFeeds} used
            {guild.tier !== 'premium' && (
              <> · premium raises this to {guild.limits.customFeedsPremium}</>
            )}
          </p>
        </div>
      </div>

      <h3>News subscriptions</h3>
      <p className="muted" style={{ marginBottom: 14 }}>
        Add and remove these from Discord with <code>/news add</code> and{' '}
        <code>/news remove</code>. Name a source for this server with{' '}
        <code>/news rename</code>.
      </p>

      {guild.news.length === 0 ? (
        <Empty>
          No news sources yet. Run <code>/news add</code> in Discord to follow OpenAI,
          Anthropic, or any RSS feed.
        </Empty>
      ) : (
        guild.news.map((n) => (
          <div className="row" key={n.feedId}>
            <div className="meta">
              <strong>{n.name || (n.label === 'custom' ? n.url : n.label)}</strong>
              <span>
                {n.name && <>{n.url} · </>}
                {n.channelName ? `#${n.channelName}` : '⚠️ channel missing'} · checked{' '}
                {timeAgo(n.lastPolled)}
                {n.failCount > 0 && ` · ⚠️ ${n.failCount} failure(s): ${n.lastError}`}
              </span>
            </div>
          </div>
        ))
      )}

      <h3 style={{ marginTop: 32 }}>Member roster</h3>
      <RosterExport guildId={guildId} info={guild.rosterExport} />

      <div style={{ marginTop: 28 }}>
        <Link className="btn secondary" to="/tickets">Need help? Open a ticket</Link>
      </div>
    </Page>
  )
}
