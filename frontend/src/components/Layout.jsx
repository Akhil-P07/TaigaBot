import { Link, NavLink } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { useMeta } from '../useMeta.js'

export function Nav() {
  const { user, logout } = useAuth()
  const authed = user?.authenticated

  return (
    <nav className="nav">
      <div className="nav-inner">
        <Link className="brand" to="/">
          <img src="/assets/TaigaBot.png" alt="" />
          <span>TaigaBot</span>
        </Link>
        <div className="nav-links">
          <NavLink to="/commands">Commands</NavLink>
          <NavLink to="/setup">Setup</NavLink>
          {authed && <NavLink to="/tickets">Support</NavLink>}
          {user?.isOwner && <NavLink to="/admin/premium">Premium</NavLink>}
          {authed ? (
            <>
              <NavLink className="btn small" to="/dashboard">Manage Server</NavLink>
              <button className="btn small secondary" onClick={logout}>Sign out</button>
            </>
          ) : (
            user?.loginEnabled && (
              <a className="btn small" href="/api/auth/login">Manage Server</a>
            )
          )}
        </div>
      </div>
    </nav>
  )
}

export function Footer() {
  const { githubUrl } = useMeta()
  return (
    <footer>
      <Link to="/commands">Commands</Link> · <Link to="/setup">Setup</Link>
      {/* Only shown when GITHUB_URL is configured, matching the old site. */}
      {githubUrl && (
        <> · <a href={githubUrl} target="_blank" rel="noreferrer">GitHub</a></>
      )}
      {' · '}<Link to="/terms">Terms</Link> · <Link to="/privacy">Privacy</Link>
    </footer>
  )
}

export function Page({ children, narrow = false }) {
  return (
    <>
      <Nav />
      <div className={`wrap${narrow ? ' narrow' : ''}`}>
        {children}
        <Footer />
      </div>
    </>
  )
}

export function Spinner({ label = 'Loading…' }) {
  return <div className="spinner">{label}</div>
}

export function Alert({ kind = 'info', children }) {
  if (!children) return null
  return <div className={`alert ${kind}`}>{children}</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function TierBadge({ tier }) {
  return (
    <span className={`badge${tier === 'premium' ? ' premium' : ''}`}>
      {tier === 'premium' ? '✨ Premium' : 'Free'}
    </span>
  )
}

export function GuildIcon({ icon, name }) {
  if (icon) return <img className="icon" src={icon} alt="" />
  return <div className="icon">{(name || '?').slice(0, 1).toUpperCase()}</div>
}
