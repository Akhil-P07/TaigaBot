import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert, Page } from '../components/Layout.jsx'
import { useAuth } from '../auth.jsx'

const FEATURES = [
  ['✅ Verification', 'RIT-email OTP keeps your server students-only. Verify once, recognized across every server running the bot, and recover your status on a new account with /recover.'],
  ['🛡️ Moderation', 'Automod with spam auto-warns, an on-device ML phishing filter, and a contact/solicitation filter — plus kick, ban, timeout and warn tools, and a deleted-message audit log.'],
  ['📊 Leveling', 'Members earn XP for chatting, with /rank and a per-server leaderboard.'],
  ['🗂️ Projects', 'Spin up gated project channels with roles and a request-to-join approval flow.'],
  ['🤖 AI Assistant', 'Ask questions right in chat with /ask, powered by Google Gemini.'],
  ['📰 News feeds', 'Follow any news site or blog with /news — OpenAI and Anthropic are one click, or paste any RSS/Atom URL. New articles land in the channel you pick, automatically.'],
]

// The OAuth callback bounces failures back here with ?error=…, so they can be
// explained in plain language instead of leaving people on a blank screen.
const OAUTH_ERRORS = {
  bad_state: 'That sign-in link expired or was already used. Please try again.',
  denied: 'Sign-in was cancelled.',
  consent_required: 'Discord needs you to approve the app first. Please try again.',
  invalid_redirect:
    "This site's redirect URL isn't registered with Discord. The bot operator " +
    'needs to add it in the Developer Portal.',
  no_code: 'Discord did not return an authorization code. Please try again.',
  token_exchange: "Couldn't complete sign-in with Discord. Please try again.",
  identify: "Couldn't read your Discord profile. Please try again.",
  oauth_failed: 'Sign-in failed unexpectedly. Please try again in a moment.',
  oauth_unconfigured: 'Sign-in is not configured on this instance.',
}

export default function Landing() {
  const { user } = useAuth()
  const [params, setParams] = useSearchParams()
  const [error, setError] = useState('')

  useEffect(() => {
    const code = params.get('error')
    if (!code) return
    setError(OAUTH_ERRORS[code] || 'Sign-in failed. Please try again.')
    // Clear it from the URL so a refresh doesn't re-show the message.
    params.delete('error')
    setParams(params, { replace: true })
  }, [params, setParams])

  return (
    <Page>
      <Alert kind="error">{error}</Alert>

      <div className="hero">
        <img src="/assets/TaigaBot.png" alt="TaigaBot" />
        <h1>TaigaBot</h1>
        <p className="tag">
          Verification, moderation, projects and news feeds for RIT Discord servers.
        </p>
        <div className="actions">
          {user?.authenticated ? (
            <Link className="btn" to="/dashboard">Manage your servers</Link>
          ) : (
            user?.loginEnabled && (
              <a className="btn" href="/api/auth/login">Sign in with Discord</a>
            )
          )}
          <Link className="btn secondary" to="/commands">📖 View commands</Link>
          <Link className="btn secondary" to="/setup">⚙️ Setup guide</Link>
        </div>
      </div>

      <div className="grid">
        {FEATURES.map(([title, body]) => (
          <div className="card" key={title}>
            <h3>{title}</h3>
            <p>{body}</p>
          </div>
        ))}
      </div>
    </Page>
  )
}
