import { useEffect, useState } from 'react'
import { Page } from '../components/Layout.jsx'

const STEPS = [
  ['Invite the bot', 'Use the invite link below. It requests exactly the permissions TaigaBot needs — no administrator.'],
  ['Run /setup', 'As a server administrator, run /setup. It creates the Verified, Unverified and Eboard roles, the #unverified, #welcome, #mod-log, #taiga-backups and #roles channels, and gates the rest of the server behind verification.'],
  ['Check the Eboard role', 'Give your officers the Eboard role — every moderation and configuration command is gated behind it.'],
  ['Set up news (optional)', 'Run /news add to follow OpenAI, Anthropic, or any RSS feed. New articles post automatically to the channel you choose.'],
  ['Verify yourself', 'Post in #unverified and follow the OTP prompt to confirm the email flow works end to end.'],
]

export default function Setup() {
  const [invite, setInvite] = useState('')

  useEffect(() => {
    fetch('/api/meta', { credentials: 'same-origin' })
      .then((r) => r.json())
      .then((d) => setInvite(d.inviteUrl || ''))
      .catch(() => {})
  }, [])

  return (
    <Page narrow>
      <div className="page-head"><h2>Setup guide</h2></div>

      {invite && (
        <p><a className="btn" href={invite} target="_blank" rel="noreferrer">➕ Add TaigaBot to your server</a></p>
      )}

      <ol>
        {STEPS.map(([title, body]) => (
          <li key={title} style={{ marginBottom: 16 }}>
            <strong>{title}</strong>
            <div className="muted">{body}</div>
          </li>
        ))}
      </ol>

      <div className="card" style={{ marginTop: 24 }}>
        <h3>Need help?</h3>
        <p>
          Sign in and open a support ticket from the Support tab — it goes straight to
          the bot maintainers.
        </p>
      </div>
    </Page>
  )
}
