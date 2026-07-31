import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Page } from '../components/Layout.jsx'

// Ported verbatim from the old keep_alive.py pages, with two additions the
// dashboard made necessary: the site now sets a login cookie and stores support
// tickets, so the "no cookies" claim and the data inventory had to be corrected.
const LAST_UPDATED = 'July 31, 2026'

function Contact({ github, club }) {
  const parts = []
  if (github) parts.push(<a key="gh" href={github}>opening an issue on GitHub</a>)
  if (club) parts.push(<a key="cl" href={club}>reaching the club through its website</a>)
  if (parts.length === 0) return <>contacting your server's Eboard</>
  return parts.length === 1 ? parts[0] : <>{parts[0]} or {parts[1]}</>
}

export default function Legal({ kind }) {
  const [meta, setMeta] = useState({ githubUrl: '', clubUrl: '' })

  useEffect(() => {
    fetch('/api/meta', { credentials: 'same-origin' })
      .then((r) => r.json())
      .then(setMeta)
      .catch(() => {})
  }, [])

  const contact = <Contact github={meta.githubUrl} club={meta.clubUrl} />
  const isTerms = kind === 'terms'

  return (
    <Page narrow>
      <div className="hero" style={{ paddingTop: 20 }}>
        <h1>{isTerms ? 'Terms of Service' : 'Privacy Policy'}</h1>
        <p className="tag">Last updated: {LAST_UPDATED}</p>
        <div className="actions">
          <Link className="btn secondary" to="/">← Back to home</Link>
          <Link className="btn secondary" to={isTerms ? '/privacy' : '/terms'}>
            {isTerms ? '🔒 Privacy Policy' : '📜 Terms of Service'}
          </Link>
        </div>
      </div>

      <div className="prose">
        {isTerms ? <Terms contact={contact} /> : <Privacy contact={contact} />}
      </div>
    </Page>
  )
}

function Terms({ contact }) {
  return (
    <>
      <h2>1. What TaigaBot is</h2>
      <p>
        TaigaBot ("the bot", "the service") is a free, open-source Discord bot built by
        the RIT AI Club for RIT student club servers. It provides university-email
        verification, moderation, leveling, projects, and related features. By adding the
        bot to a server or using its commands, you agree to these terms.
      </p>

      <h2>2. Who may use it</h2>
      <ul>
        <li>
          You must comply with Discord's <a href="https://discord.com/terms">Terms of
          Service</a> and <a href="https://discord.com/guidelines">Community
          Guidelines</a>, including Discord's minimum age requirement.
        </li>
        <li>
          Email verification is intended for holders of a valid university email address
          on the domains the hosting club has configured. Server admins may additionally
          grant access manually at their discretion.
        </li>
      </ul>

      <h2>3. Acceptable use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>verify with an email address that isn't yours, or otherwise impersonate another person;</li>
        <li>attempt to evade moderation (spam, ban evasion, alt accounts — verification is
          deliberately one account per university email);</li>
        <li>abuse, overload, or attempt to disrupt the bot, its commands, or this website;</li>
        <li>use the bot's features (e.g. <code>/ask</code>) to generate or spread content
          that violates Discord's rules or applicable law.</li>
      </ul>
      <p>
        Server moderators ("Eboard") may warn, time out, kick, or ban members who break
        server rules; the bot's automod may delete messages and issue automatic warnings.
        Moderation decisions belong to each server's Eboard, not to the bot's developers.
      </p>

      <h2>4. Server admins' responsibilities</h2>
      <ul>
        <li>Admins who add the bot are responsible for telling their members that
          verification stores a real name and university email (see the{' '}
          <Link to="/privacy">Privacy Policy</Link>).</li>
        <li>Backup rosters and moderation logs contain personal data — admins must keep
          the <code>#taiga-backups</code> and <code>#mod-log</code> channels restricted to
          Eboard.</li>
      </ul>

      <h2>5. Premium servers</h2>
      <p>
        Some servers hold a "premium" tier, which raises certain limits. Premium is
        arranged and paid for <strong>offline</strong> and granted manually by the bot's
        operators — this site does not process payments and never asks for payment
        details. Premium grants can be revoked, and no refund process is guaranteed;
        it is a volunteer project, not a commercial product.
      </p>

      <h2>6. Third-party services</h2>
      <p>
        Some features rely on third parties: verification emails are delivered via Brevo,
        and <code>/ask</code> sends your prompt to Google Gemini. The news watcher fetches
        publicly available feeds from the sites you subscribe to. Their terms apply to
        those interactions. See the <Link to="/privacy">Privacy Policy</Link> for details.
      </p>

      <h2>7. No warranty</h2>
      <p>
        The bot is a volunteer-run student project provided <strong>"as is" and "as
        available"</strong>, without warranties of any kind. We don't guarantee uptime,
        that data (XP, warnings, verification records) will never be lost, or that any
        feature will keep working. To the maximum extent permitted by law, the developers
        and the RIT AI Club are not liable for any damages arising from use of the bot.
      </p>

      <h2>8. Termination</h2>
      <p>
        You can stop using the bot at any time; server admins can remove it at any time,
        which stops all collection for that server. We may block users or servers that
        abuse the service. You may request deletion of your stored data as described in
        the <Link to="/privacy">Privacy Policy</Link>.
      </p>

      <h2>9. Changes</h2>
      <p>
        We may update these terms as the bot evolves; the "Last updated" date above will
        change when we do. Continued use after a change means you accept the new terms.
      </p>

      <h2>10. Contact</h2>
      <p>Questions about these terms are best raised by {contact}.</p>
    </>
  )
}

function Privacy({ contact }) {
  return (
    <>
      <p>
        TaigaBot is an open-source Discord bot run by the RIT AI Club for RIT student club
        servers. This page explains what data the bot stores, why, who can see it, and how
        to get it removed. The source code is public, so everything here can be checked
        against what the bot actually does.
      </p>

      <h2>1. What we collect and why</h2>
      <ul>
        <li>
          <strong>Verification records</strong> — when you run <code>/verify</code> and{' '}
          <code>/confirm</code>, the bot stores your Discord user ID, Discord username,
          the real name you entered, and your university email address. This is the core
          feature: it keeps club servers students-only and lets clubs know who their
          members are. Verification is shared across every server the bot is in, so you
          only verify once.
        </li>
        <li>
          <strong>Moderation records</strong> — warnings (who was warned, by whom, when,
          and the reason) are stored per server. Other servers' moderators can see only a{' '}
          <em>count</em> of your warnings elsewhere — never the reasons, server names, or
          details.
        </li>
        <li>
          <strong>Leveling</strong> — a message count/XP total per user, shared across
          servers so your rank follows you. No message content is stored for leveling.
        </li>
        <li>
          <strong>Server configuration</strong> — per-server settings such as automod
          toggles, banned-word lists, reaction-role bindings, news feed subscriptions, and
          project records (project names, channels, and member/lead user IDs).
        </li>
        <li>
          <strong>Message content, transiently</strong> — automod (spam, banned words,
          invite links, phishing, contact-info filters) reads messages as they arrive but
          does not store them. The phishing filter runs a small on-device model; message
          text never leaves the bot for filtering. When automod or a moderator deletes a
          message, an entry (author, channel, the deleted content when available, and who
          deleted it) is posted to the server's Eboard-only <code>#mod-log</code> channel
          so moderation is auditable.
        </li>
        <li>
          <strong>Dashboard sign-in</strong> — if you sign in to this website with
          Discord, we request only the <code>identify</code> scope and store your Discord
          user ID, display name and avatar hash against a login session. We set one
          strictly-necessary cookie holding a random session token so you stay signed in;
          it is not used for tracking or analytics. Sessions expire automatically, and
          signing out deletes them. We never ask Discord for a list of your servers — the
          servers you can manage are worked out from the bot's own membership data.
        </li>
        <li>
          <strong>Support tickets</strong> — if you open a ticket, we store your Discord
          ID and display name, the server it relates to (if any), and the text of every
          message in the thread. Don't put sensitive information in a ticket.
        </li>
        <li>
          <strong>This website</strong> — visitor IP addresses are held briefly in memory
          for rate limiting only; they are not logged or stored. Apart from the sign-in
          cookie described above, there are no cookies and no analytics.
        </li>
      </ul>

      <h2>2. Third parties we share data with</h2>
      <ul>
        <li><strong>Brevo</strong> — your email address (and the one-time code) is passed
          to Brevo to deliver verification emails.</li>
        <li><strong>Google Gemini</strong> — if you use <code>/ask</code>, your prompt is
          sent to Google's Gemini API to generate the answer. Don't put personal
          information in prompts.</li>
        <li><strong>Discord</strong> — the bot runs on Discord; everything it posts
          (mod-log entries, backup rosters, replies) lives in Discord channels and is
          subject to <a href="https://discord.com/privacy">Discord's privacy policy</a>.
          Signing in to this site also involves Discord's OAuth service.</li>
        <li><strong>News sources</strong> — the news watcher fetches public feeds (for
          example OpenAI's and Anthropic's newsrooms, or any feed a server adds). These
          are ordinary outbound requests for public pages; no personal data is sent.</li>
      </ul>
      <p>We never sell data or share it with anyone else.</p>

      <h2>3. Who can see what</h2>
      <ul>
        <li><strong>Everyone in a server</strong> can see ranks and the leaderboard, and
          public project info.</li>
        <li>
          <strong>Eboard / admins of each server</strong> can look up the verified name
          and email of that server's members (<code>/whois</code>), see that server's
          warnings, the mod-log, and the periodic <strong>backup roster</strong> — a CSV
          of the server's current verified members' names and emails, posted to the
          Eboard-only <code>#taiga-backups</code> channel so membership survives a hosting
          wipe. If you verified on one server and joined another, that server's Eboard can
          see your name and email too — verified membership is intentionally visible to
          the leadership of every server you join.
        </li>
        <li><strong>The bot's host operator</strong> (the RIT AI Club) has access to the
          underlying database, and can read every support ticket.</li>
      </ul>

      <h2>4. Retention and deletion</h2>
      <ul>
        <li>Verification, warning, XP, project and ticket records are kept until they are
          deleted by a moderator (e.g. <code>/clearwarnings</code>) or on request.</li>
        <li>Login sessions expire automatically and are deleted when you sign out.</li>
        <li>Lost your Discord account? <code>/recover</code> <em>moves</em> your
          verification to the new account and removes it from the old one — nothing is
          duplicated.</li>
        <li><strong>To have your data removed</strong>, ask your server's Eboard or
          contact us by {contact}. We'll delete your verification record and associated
          data; note you'd lose access to gated channels until you verify again.</li>
        <li>Leaving a server does not by itself delete your verification — it's
          account-wide, so rejoining or joining a sibling server still works. Ask for
          deletion if you want it gone entirely.</li>
      </ul>

      <h2>5. Security</h2>
      <p>
        Data is stored in a database on the bot's host, accessible only to the operators.
        Name/email rosters are only ever posted to Eboard-restricted channels. One-time
        verification codes are held in memory only, expire after a few minutes, and are
        never written to the database. Website login sessions are stored as a hash, so the
        database alone cannot be used to impersonate a signed-in user. That said, this is
        a volunteer student project — don't use it for data you consider highly sensitive.
      </p>

      <h2>6. Children</h2>
      <p>
        The bot is intended for university students and follows Discord's minimum age
        requirements; it is not directed at children.
      </p>

      <h2>7. Changes</h2>
      <p>
        If what we collect or share changes, this page and its "Last updated" date will be
        updated. Material changes will be announced in the servers the bot serves.
      </p>

      <h2>8. Contact</h2>
      <p>
        Privacy questions or deletion requests: {contact}, or ask any Eboard member of
        your server to pass the request on.
      </p>
    </>
  )
}
