import { Link } from 'react-router-dom'
import { Page } from '../components/Layout.jsx'
import { useMeta } from '../useMeta.js'

const STEPS = [
  ['Invite the bot', 'Use the invite link below. It requests exactly the permissions TaigaBot needs, and never Administrator.'],
  ['Run /setup', 'As the server owner or an administrator, run /setup. A panel appears where you can exclude channels or categories from gating and toggle role reset (read the warning below). It then creates the Verified, Unverified and Eboard roles, the #unverified, #welcome, #mod-log, #taiga-backups and #roles channels, and gates the rest of the server behind verification.'],
  ['Check the Eboard role', 'Give your officers the Eboard role. Every moderation and configuration command is gated behind it.'],
  ['Verify yourself', 'Post in #unverified and follow the OTP prompt to confirm the email flow works end to end.'],
]

// Answers are kept short and concrete. Anything that needs a page of its own
// links to it rather than being restated here.
const FAQS = [
  ['Does everyone need an RIT email to join?', (
    <>
      <p>
        To self-verify, yes. <code>/verify</code> only accepts an address on the
        domains the bot is configured for (<code>rit.edu</code> and{' '}
        <code>g.rit.edu</code> by default).
      </p>
      <p>
        For sponsors, alumni or guests without one, an admin can simply give them the
        Verified role by hand. The bot never takes that role away, so manual grants
        stick.
      </p>
    </>
  )],
  ['How does verification actually work?', (
    <p>
      A member runs <code>/verify</code> with their university email. The bot emails a
      one-time code, they run <code>/confirm</code> with it, and they get the Verified
      role. Codes expire after about 10 minutes and allow a handful of attempts. Codes
      are held in memory only and never written to the database.
    </p>
  )],
  ['Is it safe to re-run /setup?', (
    <>
      <p>
        Yes, it's idempotent. It creates anything missing, re-applies the channel
        permissions, and backfills roles for members who joined while the bot was
        offline. Running it on an established server is the supported way to migrate.
      </p>
      <p>
        The one exception is the <strong>Role reset</strong> toggle, which is
        destructive and off by default. See the warning above before enabling it.
      </p>
    </>
  )],
  ['Do members have to verify again in every server?', (
    <p>
      No. Verification is tied to the Discord account, not the server, so someone who
      verified in another TaigaBot server is recognised the moment they join yours. One
      university email maps to exactly one Discord account, which is what makes alt
      accounts and ban evasion hard.
    </p>
  )],
  ['Someone lost access to their Discord account. Now what?', (
    <p>
      They run <code>/recover</code> from the new account and verify with the same
      email. That <em>moves</em> the verification record across and removes it from the
      old account, so nothing is duplicated and the one-account rule still holds.
    </p>
  )],
  ['Why does the bot ask for these permissions? Does it need Administrator?', (
    <p>
      It never asks for Administrator. The invite link requests exactly what the
      features need: managing roles and channels for <code>/setup</code>, the
      moderation permissions for kick/ban/timeout, and reading messages for automod and
      XP. If you deny a permission, only the feature that needs it stops working.
    </p>
  )],
  ['What is the Eboard role for?', (
    <p>
      It's the bot's staff role: every moderation and configuration command is gated
      behind it (or server Administrator). It also controls who can manage the server
      from this website. Give it to your officers and nobody else.
    </p>
  )],
  ['Which channels must stay private?', (
    <>
      <p>
        <code>#mod-log</code> and <code>#taiga-backups</code>. <code>/setup</code>{' '}
        restricts them to Eboard, and it should stay that way.
      </p>
      <p>
        The backup roster is a CSV of your verified members' real names and email
        addresses, and the mod log can contain deleted message content. Both are
        personal data; see the <Link to="/privacy">Privacy Policy</Link>.
      </p>
    </>
  )],
  ['Can I rename the roles and channels the bot uses?', (
    <p>
      The bot finds them by name, so renaming <code>#mod-log</code> or the Verified role
      inside Discord will make it lose track of them. The names are set by whoever hosts
      the bot, not per-server. If you need different ones, open a ticket and ask.
    </p>
  )],
  ['How many news feeds can I follow?', (
    <p>
      Five custom feeds per server, or 25 on premium. Built-in sources such as OpenAI's
      and Anthropic's newsrooms are free to follow on top of that. Add one with{' '}
      <code>/news add</code> and pick the channel it posts to.
    </p>
  )],
  ['What does premium change, and how do I get it?', (
    <p>
      It raises limits, most visibly the custom news feed cap. It's arranged offline
      and granted by hand; this site never processes payments. Open a support ticket to
      ask about it.
    </p>
  )],
  ['How do I remove the bot?', (
    <p>
      Kick it like any other member. That stops all collection for your server
      immediately. Verification records are account-wide and survive, so re-adding the
      bot later picks up where you left off. The maintainers can also remove the bot
      from a server that abuses it; if that happens, the bot posts the reason in the
      server before it leaves.
    </p>
  )],
  ['Something is broken. Where do I report it?', (
    <p>
      Sign in and open a support ticket. It goes straight to the maintainers. Include
      the command you ran and what happened. <code>/health</code> is a quick way to
      check the bot's own view of your server first.
    </p>
  )],
]

export default function Setup() {
  const { inviteUrl: invite } = useMeta()

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

      <div className="notice warn" style={{ marginTop: 8 }}>
        <h4>⚠️ Keep #mod-log and #taiga-backups Eboard-only</h4>
        <p>
          <code>/setup</code> restricts both channels to the Eboard role. Open each
          one's permissions afterwards and confirm it stayed that way, especially if
          you excluded channels from gating or moved them into another category.
        </p>
        <p>
          <code>#taiga-backups</code> receives a CSV of your verified members' real
          names and email addresses, and <code>#mod-log</code> can contain deleted
          message content. Anyone who can read those channels can read all of it, so
          widening them exposes personal data your members gave you for verification.
        </p>
      </div>

      <div className="notice warn" style={{ marginTop: 12 }}>
        <h4>⚠️ Before you enable role reset in /setup</h4>
        <p>
          The <strong>Role reset</strong> toggle on the <code>/setup</code> panel is{' '}
          <strong>destructive and off by default</strong>. Turning it on strips{' '}
          <em>every</em> member's roles so old interest or self-assign roles can't keep
          granting access to channels you're about to gate.
        </p>
        <p>It keeps only:</p>
        <ul>
          <li>Verified, Unverified and Eboard;</li>
          <li>bot-managed roles (integrations, Nitro booster);</li>
          <li>any role positioned above TaigaBot's own role, which Discord won't let
            it touch, so those roles survive whether you want them to or not.</li>
        </ul>
        <p style={{ marginTop: 8 }}>
          Everything else is removed from everyone in one pass. There is no undo:
          Discord doesn't restore removed roles, and members have to re-pick theirs in{' '}
          <code>#roles</code> after verifying. Leave it off unless you're deliberately
          migrating a server to a clean slate, and if you are, note who holds which
          roles first.
        </p>
      </div>

      <h3 style={{ marginTop: 34 }}>Frequently asked questions</h3>
      {FAQS.map(([question, answer]) => (
        <details className="faq" key={question}>
          <summary>{question}</summary>
          <div className="answer">{answer}</div>
        </details>
      ))}

      <div className="card" style={{ marginTop: 24 }}>
        <h3>Need help?</h3>
        <p>
          Sign in and open a support ticket from the Support tab.
        </p>
      </div>
    </Page>
  )
}
