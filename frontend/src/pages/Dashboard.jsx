import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { Alert, Empty, GuildIcon, Page, Spinner, TierBadge } from '../components/Layout.jsx'

export default function Dashboard() {
  const { user } = useAuth()
  const [guilds, setGuilds] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.guilds()
      .then((d) => setGuilds(d.guilds))
      .catch((e) => { setError(e.message); setGuilds([]) })
  }, [])

  return (
    <Page>
      <div className="page-head">
        <h2>Your servers</h2>
        {user?.isOwner && (
          <div className="right">
            <Link className="btn small secondary" to="/admin/premium">Manage premium</Link>
          </div>
        )}
      </div>

      <Alert kind="error">{error}</Alert>

      {guilds === null && <Spinner />}

      {guilds?.length === 0 && !error && (
        <Empty>
          <p>No servers to manage yet.</p>
          <p className="muted">
            You'll see a server here once you hold the Eboard role (or are an
            administrator) in a server TaigaBot is in.
          </p>
        </Empty>
      )}

      {guilds?.map((g) => (
        <Link className="row" to={`/servers/${g.id}`} key={g.id} style={{ color: 'inherit' }}>
          <GuildIcon icon={g.icon} name={g.name} />
          <div className="meta">
            <strong>{g.name}</strong>
            <span>
              {g.memberCount?.toLocaleString()} members · you are{' '}
              {g.role === 'admin' ? 'an admin' : 'Eboard'}
            </span>
          </div>
          <div className="right">
            <TierBadge tier={g.tier} />
          </div>
        </Link>
      ))}
    </Page>
  )
}
