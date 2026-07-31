import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, formatDate, timeAgo } from '../api.js'
import { Alert, Empty, GuildIcon, Page, Spinner, TierBadge } from '../components/Layout.jsx'

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
        <code>/news remove</code>.
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
              <strong>{n.label === 'custom' ? n.url : n.label}</strong>
              <span>
                {n.channelName ? `#${n.channelName}` : '⚠️ channel missing'} · checked{' '}
                {timeAgo(n.lastPolled)}
                {n.failCount > 0 && ` · ⚠️ ${n.failCount} failure(s): ${n.lastError}`}
              </span>
            </div>
          </div>
        ))
      )}

      <div style={{ marginTop: 28 }}>
        <Link className="btn secondary" to="/tickets">Need help? Open a ticket</Link>
      </div>
    </Page>
  )
}
